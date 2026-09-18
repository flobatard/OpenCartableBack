"""Tests des tâches de purge (:mod:`app.maintenance.service`) et de la garde de
schéma — sans Postgres ni S3.

Deux motifs déjà en place dans la suite :

- **SQL compilé** (``stmt.compile(dialect=postgresql.dialect())`` + ses
  ``params``), comme ``test_search_api.py`` et ``test_student_exercises_api.py``:
  chaque tâche est vérifiée sur sa table, son prédicat et sa borne — c'est là
  que se jouent les pièges (fuseau, plancher de rétention, prédicat de
  ``share_links``), pas dans un aller-retour base.
- **Faux client S3** enregistrant ses appels (motif ``FakeStorage`` de
  ``test_resources_api.py``), enrichi de ``iter_objects``.

Les deux fakes partagent une liste d'``events`` : c'est ce qui permet
d'affirmer que la purge du bucket a bien lieu **après** le commit. Ils vivent
dans :mod:`tests.maintenance_fakes` — cinq fichiers de tests les partagent.

Ce fichier-ci ne couvre que les tâches elles-mêmes et la garde de schéma.
L'enchaînement des jobs, leur registre, leur cadence et leur état sont ailleurs :
``test_maintenance_runner.py``, ``test_maintenance_registry.py``,
``test_maintenance_scheduler.py``, ``test_maintenance_state.py`` ; les deux
contrôles en lecture seule, dans ``test_maintenance_checks.py``.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.sql.dml import Delete, Update

from app.maintenance import schema as schema_guard
from app.maintenance import service as maintenance
from tests.maintenance_fakes import (
    FakeResult,
    FakeSession,
    FakeStorage,
    RevisionSession,
    compiled_params,
    compiled_sql,
    s3_object,
)

# ─────────────────────────────────────────────
# Rétention 0 = tâche désactivée
# ─────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "task",
    [
        maintenance.purge_ai_daily_usage,
        maintenance.purge_tool_message_content,
        maintenance.purge_ai_conversations,
        maintenance.purge_exercise_submissions,
        maintenance.purge_share_links,
    ],
)
async def test_retention_zero_disables_task(task):
    """Rétention nulle : la tâche sort avant le moindre execute."""
    db = FakeSession()
    assert await task(db, 0) == 0
    assert db.statements == []
    assert db.commits == 0


@pytest.mark.anyio
async def test_retention_zero_disables_s3_tasks():
    db, storage = FakeSession(), FakeStorage()
    assert await maintenance.purge_pending_resources(db, storage, 0) == 0
    assert await maintenance.reconcile_s3_orphans(db, storage, 0, dry_run=False) == 0
    assert db.statements == []
    assert storage.listed == []
    assert storage.deleted == []


# ─────────────────────────────────────────────
# Compteurs de quota
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_ai_daily_usage_cutoff_is_a_utc_date():
    """La borne est un ``date`` UTC, pas un timestamp du fuseau serveur."""
    db = FakeSession([FakeResult(rowcount=7)])
    assert await maintenance.purge_ai_daily_usage(db, 90) == 7

    stmt = db.statements[0]
    assert isinstance(stmt, Delete)
    assert "DELETE FROM ai_daily_usage" in compiled_sql(stmt)
    cutoff = compiled_params(stmt)["day_1"]
    assert cutoff == datetime.now(UTC).date() - timedelta(days=90)
    assert not isinstance(cutoff, datetime)  # un date, pas un datetime
    assert db.commits == 1


@pytest.mark.anyio
@pytest.mark.parametrize("days", [1, 2])
async def test_ai_daily_usage_never_touches_today_nor_yesterday(days):
    """Plancher dur : le jour courant porte le quota vivant, la veille peut
    encore recevoir un remboursement à cheval sur minuit UTC."""
    db = FakeSession()
    await maintenance.purge_ai_daily_usage(db, days)

    cutoff = compiled_params(db.statements[0])["day_1"]
    today = datetime.now(UTC).date()
    assert cutoff <= today - timedelta(days=maintenance.MIN_USAGE_RETENTION_DAYS)


# ─────────────────────────────────────────────
# Contenu des tours tool
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_tool_content_is_trimmed_not_deleted():
    """UPDATE et non DELETE : la ligne doit survivre pour rester appariée à
    son tool_call_id. Seuls les tours ``tool`` sont concernés."""
    db = FakeSession([FakeResult(rowcount=3)])
    assert await maintenance.purge_tool_message_content(db, 180) == 3

    stmt = db.statements[0]
    assert isinstance(stmt, Update)
    sql = compiled_sql(stmt)
    assert sql.startswith("UPDATE ai_messages SET content=")
    assert "left(ai_messages.content" in sql
    assert "ai_messages.role =" in sql
    params = compiled_params(stmt)
    assert params["role_1"] == "tool"
    assert params["left_1"] == maintenance.TOOL_CONTENT_KEEP_CHARS
    assert params["left_2"] == maintenance.TOOL_CONTENT_MARKER


@pytest.mark.anyio
async def test_tool_content_trim_is_idempotent():
    """Le seuil de longueur vaut exactement la taille d'une ligne déjà allégée :
    une seconde passe ne la resélectionne pas."""
    db = FakeSession()
    await maintenance.purge_tool_message_content(db, 180)

    threshold = compiled_params(db.statements[0])["length_1"]
    trimmed_length = maintenance.TOOL_CONTENT_KEEP_CHARS + len(
        maintenance.TOOL_CONTENT_MARKER
    )
    assert threshold == trimmed_length
    assert not trimmed_length > threshold  # le prédicat est un `>` strict


# ─────────────────────────────────────────────
# Conversations, tentatives, liens
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_conversations_purged_on_last_activity():
    db = FakeSession([FakeResult(rowcount=2)])
    assert await maintenance.purge_ai_conversations(db, 365) == 2

    sql = compiled_sql(db.statements[0])
    assert "DELETE FROM ai_conversations" in sql
    assert "updated_at <" in sql  # dernière activité, pas la création


@pytest.mark.anyio
async def test_exercise_submissions_purged_on_created_at():
    db = FakeSession([FakeResult(rowcount=5)])
    assert await maintenance.purge_exercise_submissions(db, 365) == 5

    sql = compiled_sql(db.statements[0])
    assert "DELETE FROM exercise_submissions" in sql
    assert "created_at <" in sql


@pytest.mark.anyio
async def test_share_links_purged_on_expiry_only():
    """Un lien révoqué mais non expiré est délibérément conservé (audit) :
    le prédicat ne doit mentionner que ``expires_at``."""
    db = FakeSession([FakeResult(rowcount=4)])
    assert await maintenance.purge_share_links(db, 365) == 4

    sql = compiled_sql(db.statements[0])
    assert "DELETE FROM share_links WHERE share_links.expires_at <" in sql
    assert "revoked" not in sql


# ─────────────────────────────────────────────
# Ressources pending
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_pending_resources_purge_s3_after_commit():
    """Motif ``delete_course`` : clés relevées, DELETE, commit, PUIS bucket."""
    events: list[str] = []
    db = FakeSession(
        [FakeResult(rows=["courses/c/resources/r/a.pdf"]), FakeResult(rowcount=1)],
        events=events,
    )
    storage = FakeStorage(events=events)

    assert await maintenance.purge_pending_resources(db, storage, 30) == 1

    assert storage.deleted == ["courses/c/resources/r/a.pdf"]
    assert events == ["execute", "execute", "commit", "delete_many"]
    assert "resources.status =" in compiled_sql(db.statements[0])
    assert compiled_params(db.statements[0])["status_1"] == "pending"


@pytest.mark.anyio
async def test_pending_resources_noop_without_candidates():
    """Aucune ressource à purger : pas de DELETE, pas d'appel S3."""
    events: list[str] = []
    db = FakeSession([FakeResult(rows=[])], events=events)
    storage = FakeStorage(events=events)

    assert await maintenance.purge_pending_resources(db, storage, 30) == 0
    assert len(db.statements) == 1  # le select seul
    assert db.commits == 0
    assert storage.deleted == []


# ─────────────────────────────────────────────
# Réconciliation des orphelins S3
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_orphans_spare_referenced_and_recent_keys():
    """Une clé référencée en base est épargnée ; une clé plus jeune que la
    grâce aussi (fenêtre d'un import ou d'un upload en cours)."""
    known = "courses/c/resources/known/a.pdf"
    orphan = "courses/c/resources/gone/b.pdf"
    fresh = "courses/c/resources/new/c.pdf"
    storage = FakeStorage(
        {"courses/": [[s3_object(known, 200), s3_object(orphan, 200), s3_object(fresh, 1)]]}
    )
    db = FakeSession([FakeResult(rows=[known]), FakeResult(rows=[])])

    found = await maintenance.reconcile_s3_orphans(db, storage, 90, dry_run=False)

    assert found == 1
    assert storage.deleted == [orphan]
    # `fresh` n'a même pas été soumis à l'anti-jointure : trop récent.
    assert fresh not in compiled_params(db.statements[0]).values()


@pytest.mark.anyio
async def test_orphans_dry_run_deletes_nothing():
    orphan = "courses/c/resources/gone/b.pdf"
    storage = FakeStorage({"courses/": [[s3_object(orphan, 200)]]})
    db = FakeSession([FakeResult(rows=[]), FakeResult(rows=[])])

    assert await maintenance.reconcile_s3_orphans(db, storage, 90, dry_run=True) == 1
    assert storage.deleted == []


@pytest.mark.anyio
async def test_orphans_spare_avatars_still_referenced():
    """L'anti-jointure interroge aussi ``users.avatar_s3_key``."""
    avatar = "users/u/avatar/x/avatar.png"
    storage = FakeStorage({"users/": [[s3_object(avatar, 200)]]})
    db = FakeSession([FakeResult(rows=[]), FakeResult(rows=[avatar])])

    assert await maintenance.reconcile_s3_orphans(db, storage, 90, dry_run=False) == 0
    assert storage.deleted == []


@pytest.mark.anyio
async def test_orphans_sweep_only_known_prefixes():
    """Le bucket peut contenir autre chose : on ne balaye que nos deux préfixes."""
    storage = FakeStorage()
    await maintenance.reconcile_s3_orphans(FakeSession(), storage, 90, dry_run=True)
    assert storage.listed == ["courses/", "users/"]


@pytest.mark.anyio
async def test_orphans_process_pages_independently():
    """Deux pages ⇒ deux anti-jointures : le bucket n'est jamais tenu en RAM."""
    page_one, page_two = "courses/a/1", "courses/a/2"
    storage = FakeStorage({"courses/": [[s3_object(page_one, 200)], [s3_object(page_two, 200)]]})
    db = FakeSession([FakeResult(rows=[]) for _ in range(4)])

    assert await maintenance.reconcile_s3_orphans(db, storage, 90, dry_run=False) == 2
    assert len(db.statements) == 4  # 2 pages × (resources + users)
    assert storage.deleted == [page_one, page_two]
    assert db.commits == 0  # la réconciliation ne touche jamais la base


# ─────────────────────────────────────────────
# Garde de schéma
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_expected_head_is_the_image_alembic_head():
    """La tête est lue dans le dossier alembic/ embarqué, sans dépendre du cwd."""
    assert schema_guard.ALEMBIC_DIR.is_dir()
    assert schema_guard.expected_head()  # une chaîne de révision non vide


@pytest.mark.anyio
async def test_schema_guard_passes_when_revisions_match(monkeypatch):
    monkeypatch.setattr(schema_guard, "expected_head", lambda: "abc123")
    db = RevisionSession(["abc123"])

    assert await schema_guard.wait_until_current(db, timeout=0, poll=0) is True


@pytest.mark.anyio
async def test_schema_guard_waits_then_passes(monkeypatch):
    """Migration en cours côté api : la garde repasse dès que la base rattrape."""
    monkeypatch.setattr(schema_guard, "expected_head", lambda: "neuve")
    db = RevisionSession(["ancienne", "ancienne", "neuve"])

    assert await schema_guard.wait_until_current(db, timeout=10, poll=0) is True
    assert len(db.statements) == 3


@pytest.mark.anyio
async def test_schema_guard_gives_up_on_stale_database(monkeypatch):
    """Au-delà du délai, on renonce : ne rien purger est toujours sûr."""
    monkeypatch.setattr(schema_guard, "expected_head", lambda: "neuve")
    db = RevisionSession(["ancienne"] * 5)

    assert await schema_guard.wait_until_current(db, timeout=0, poll=0) is False


@pytest.mark.anyio
async def test_schema_guard_handles_never_migrated_database(monkeypatch):
    """Pas de table alembic_version : pas de crash, un rollback, et un refus."""
    monkeypatch.setattr(schema_guard, "expected_head", lambda: "neuve")
    db = RevisionSession([None])

    assert await schema_guard.wait_until_current(db, timeout=0, poll=0) is False
    assert db.rollbacks == 1


@pytest.mark.anyio
async def test_schema_guard_refuses_when_head_is_undeterminable(monkeypatch):
    """Tête introuvable (dossier alembic absent) : on ne touche même pas la base."""
    monkeypatch.setattr(schema_guard, "expected_head", lambda: None)
    db = RevisionSession([])

    assert await schema_guard.wait_until_current(db, timeout=0, poll=0) is False
    assert db.statements == []


@pytest.mark.anyio
async def test_is_current_checks_once_and_never_waits(monkeypatch):
    """La garde de régime : un seul SELECT, aucune attente.

    C'est ce qui permet de la rejouer avant CHAQUE job du scheduler, qui vit des
    semaines pendant que l'api déploie de nouvelles migrations.
    """
    monkeypatch.setattr(schema_guard, "expected_head", lambda: "neuve")
    db = RevisionSession(["ancienne", "neuve"])

    assert await schema_guard.is_current(db) is False
    assert len(db.statements) == 1  # elle n'a pas bouclé jusqu'à "neuve"
    assert await schema_guard.is_current(db) is True
    assert len(db.statements) == 2


@pytest.mark.anyio
async def test_is_current_refuses_when_head_is_undeterminable(monkeypatch):
    monkeypatch.setattr(schema_guard, "expected_head", lambda: None)
    db = RevisionSession([])

    assert await schema_guard.is_current(db) is False
    assert db.statements == []
