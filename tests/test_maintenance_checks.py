"""Tests des contrôles en lecture seule (:mod:`app.maintenance.checks`).

Ce que ces tests protègent avant tout : **un contrôle ne corrige rien**. Deux
d'entre eux ne vérifient que des absences — aucun commit, aucun DELETE, aucune
suppression S3 — et ce sont les plus importants du fichier : un « contrôle » qui
se mettrait à réparer tout seul supprimerait des données sur la foi d'un
diagnostic que personne n'a relu.

Le reste porte sur les bornes. La vérification inverse est **plafonnée par
passe** et reprend au curseur de la précédente : c'est ce qui la rend tenable
sur un Pi, et c'est aussi ce qui fait que sa couverture n'est jamais complète en
une fois.
"""

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from sqlalchemy.sql.dml import Delete, Update

from app.core.config import settings
from app.maintenance import checks
from tests.maintenance_fakes import FakeResult, FakeSession, FakeStorage, compiled_sql, s3_object


def size_rows(**sizes):
    """Le résultat de la requête catalogue : des lignes ``(name, bytes)``."""
    return FakeResult(
        rows=[SimpleNamespace(name=name, bytes=value) for name, value in sizes.items()]
    )


# ─────────────────────────────────────────────
# Inventaire de volumétrie
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_inventory_counts_every_purgeable_table():
    """Toutes les tables que la purge fait décroître sont comptées, en **une**
    requête — et les ressources ``pending`` à part, puisqu'elles ont leur job."""
    counts = {name: 1 for name, _ in checks.INVENTORIED_TABLES}
    db = FakeSession([FakeResult(rows=[counts | {"resources_pending": 3}]), size_rows()])

    outcome = await checks.storage_inventory(db, FakeStorage())

    sql = compiled_sql(db.statements[0])
    for name, _ in checks.INVENTORIED_TABLES:
        assert f"FROM {name}" in sql
    assert "resources.status = " in sql
    assert outcome.detail["rows"]["resources_pending"] == 3


@pytest.mark.anyio
async def test_inventory_reports_disk_size_not_just_row_counts():
    """La métrique qui compte pour une carte SD, c'est l'espace occupé —
    ``pg_total_relation_size`` inclut index et TOAST."""
    db = FakeSession(
        [
            FakeResult(rows=[{name: 0 for name, _ in checks.INVENTORIED_TABLES}]),
            size_rows(ai_messages=1024, courses=512),
        ]
    )

    outcome = await checks.storage_inventory(db, FakeStorage())

    assert "pg_total_relation_size" in compiled_sql(db.statements[1])
    # `courses` n'est pas une table suivie : elle compte dans le total du
    # schéma, pas dans le détail par table.
    assert outcome.detail["table_bytes"] == {"ai_messages": 1024}
    assert outcome.detail["schema_bytes"] == 1536


@pytest.mark.anyio
async def test_inventory_sums_the_bucket_page_by_page():
    """Le bucket n'est jamais tenu en mémoire : on n'accumule que deux entiers
    par préfixe, quelle que soit sa taille."""
    db = FakeSession(
        [FakeResult(rows=[{name: 0 for name, _ in checks.INVENTORIED_TABLES}]), size_rows()]
    )
    storage = FakeStorage(
        pages={
            "courses/": [
                [s3_object("courses/a", size=100), s3_object("courses/b", size=50)],
                [s3_object("courses/c", size=25)],
            ],
            "users/": [[s3_object("users/x", size=7)]],
        }
    )

    outcome = await checks.storage_inventory(db, storage)

    assert storage.listed == ["courses/", "users/"]
    assert outcome.detail["s3"]["courses/"] == {"objects": 3, "bytes": 175}
    assert outcome.detail["s3"]["users/"] == {"objects": 1, "bytes": 7}
    # Le compte du job est le nombre total d'objets : comparable d'une semaine
    # à l'autre dans `last_count`.
    assert outcome.count == 4


@pytest.mark.anyio
async def test_inventory_never_writes():
    db = FakeSession(
        [FakeResult(rows=[{name: 0 for name, _ in checks.INVENTORIED_TABLES}]), size_rows()]
    )
    storage = FakeStorage(pages={"courses/": [[s3_object("courses/a")]]})

    await checks.storage_inventory(db, storage)

    assert db.commits == 0
    assert storage.deleted == []
    assert not any(isinstance(stmt, Delete | Update) for stmt in db.statements)


# ─────────────────────────────────────────────
# Objets S3 manquants
# ─────────────────────────────────────────────


async def run_missing(db, storage, **overrides):
    kwargs = {"grace_days": 1, "max_checks": 100, "concurrency": 8, "cursor": None}
    return await checks.missing_s3_objects(db, storage, **(kwargs | overrides))


@pytest.mark.anyio
async def test_missing_objects_checks_available_rows_only():
    """Une ressource ``pending`` n'a légitimement pas encore d'objet : la
    signaler serait un faux positif systématique."""
    db = FakeSession([FakeResult(rows=[]), FakeResult(rows=["courses/a.pdf"])])

    await run_missing(db, FakeStorage())

    avatars, resources = (compiled_sql(stmt) for stmt in db.statements)
    assert "users.avatar_status = " in avatars
    assert "resources.status = " in resources
    assert "resources.created_at < " in resources  # la grâce


@pytest.mark.anyio
async def test_missing_objects_reports_only_absent_keys():
    db = FakeSession(
        [FakeResult(rows=["users/avatar.png"]), FakeResult(rows=["courses/a.pdf", "courses/b.pdf"])]
    )
    storage = FakeStorage(missing={"courses/b.pdf", "users/avatar.png"})

    outcome = await run_missing(db, storage)

    assert outcome.count == 2
    assert set(outcome.detail["missing_keys"]) == {"courses/b.pdf", "users/avatar.png"}
    assert outcome.detail["checked"] == 3


@pytest.mark.anyio
async def test_missing_objects_respects_the_check_budget():
    """Le plafond est ce qui rend le contrôle tenable sur un Pi : il est tenu,
    même si la base a des milliers de lignes de plus."""
    db = FakeSession([FakeResult(rows=[]), FakeResult(rows=[f"courses/{i}" for i in range(10)])])
    storage = FakeStorage()

    await run_missing(db, storage, max_checks=10)

    assert len(storage.headed) == 10
    assert compiled_sql(db.statements[1]).endswith("LIMIT %(param_1)s")


@pytest.mark.anyio
async def test_avatars_are_counted_against_the_budget():
    """Les avatars passent en premier et entament le budget : sinon une
    instance à 2 000 avatars ne vérifierait jamais une seule ressource."""
    db = FakeSession([FakeResult(rows=["users/a.png", "users/b.png"]), FakeResult(rows=[])])
    storage = FakeStorage()

    await run_missing(db, storage, max_checks=3)

    assert storage.headed[:2] == ["users/a.png", "users/b.png"]
    assert db.statements[1].compile().params["param_1"] == 1  # le budget restant


@pytest.mark.anyio
async def test_missing_objects_resumes_from_the_stored_cursor():
    db = FakeSession([FakeResult(rows=[]), FakeResult(rows=["courses/z.pdf"])])

    outcome = await run_missing(db, FakeStorage(), cursor="courses/m.pdf", max_checks=5)

    assert "resources.s3_key > " in compiled_sql(db.statements[1])
    # Tranche plus courte que le budget : la table est épuisée, on enroule.
    assert outcome.detail["wrapped"] is True
    assert outcome.detail["cursor"] is None


@pytest.mark.anyio
async def test_a_full_slice_advances_the_cursor():
    """Tranche pleine : la passe suivante doit reprendre après la dernière clé,
    sinon le contrôle revérifierait éternellement les mêmes objets."""
    keys = [f"courses/{i}.pdf" for i in range(3)]
    db = FakeSession([FakeResult(rows=[]), FakeResult(rows=keys)])

    outcome = await run_missing(db, FakeStorage(), max_checks=3)

    assert outcome.detail["wrapped"] is False
    assert outcome.detail["cursor"] == "courses/2.pdf"


@pytest.mark.anyio
async def test_missing_objects_limits_concurrency():
    db = FakeSession([FakeResult(rows=[]), FakeResult(rows=[f"courses/{i}" for i in range(50)])])
    storage = FakeStorage()

    await run_missing(db, storage, concurrency=4)

    assert storage.max_in_flight <= 4


@pytest.mark.anyio
async def test_missing_objects_propagates_a_real_s3_error():
    """Un bucket momentanément indisponible ne doit jamais se déguiser en
    « tout est manquant » : le job échoue, aucun faux rapport n'est écrit."""

    class BrokenStorage(FakeStorage):
        async def head(self, s3_key):
            raise ClientError({"Error": {"Code": "500"}}, "HeadObject")

    db = FakeSession([FakeResult(rows=[]), FakeResult(rows=["courses/a.pdf"])])

    with pytest.raises(ClientError):
        await run_missing(db, BrokenStorage())


@pytest.mark.anyio
async def test_missing_objects_never_deletes_anything():
    db = FakeSession([FakeResult(rows=[]), FakeResult(rows=["courses/a.pdf"])])
    storage = FakeStorage(missing={"courses/a.pdf"})

    await run_missing(db, storage)

    assert storage.deleted == []
    assert db.commits == 0
    assert not any(isinstance(stmt, Delete | Update) for stmt in db.statements)


@pytest.mark.anyio
async def test_a_zero_budget_does_nothing(monkeypatch):
    db, storage = FakeSession(), FakeStorage()

    outcome = await run_missing(db, storage, max_checks=0)

    assert outcome.count == 0
    assert db.statements == []
    assert storage.headed == []


@pytest.mark.anyio
async def test_missing_keys_are_bounded_in_the_state(monkeypatch):
    """Le détail part en JSONB dans une table qui doit rester à quelques
    kilo-octets ; les clés complètes, elles, sont toutes journalisées."""
    monkeypatch.setattr(settings, "MAINTENANCE_DETAIL_MAX_ITEMS", 2)
    keys = [f"courses/{i}.pdf" for i in range(5)]
    db = FakeSession([FakeResult(rows=[]), FakeResult(rows=keys)])

    outcome = await run_missing(db, FakeStorage(missing=set(keys)), max_checks=5)

    from app.maintenance.state import bounded

    assert outcome.count == 5  # le compte, lui, est exact
    assert len(bounded(outcome.detail)["missing_keys"]) == 2
