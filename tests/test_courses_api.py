"""Routes /courses — aucun Postgres requis.

La fausse session sert les résultats des SELECT dans l'ordre des ``execute``
du service (FIFO, ordre documenté dans app/courses/service.py) ; les
INSERT/UPDATE/DELETE sont tracés dans ``executed`` sans consommer la file,
à une exception près : un INSERT porteur de RETURNING (celui de ``courses``)
consomme aussi la file pour servir les timestamps ``server_default``.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete, Insert, Update

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.main import create_app

_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def _user_row():
    return SimpleNamespace(id=uuid.uuid4(), sub="prof-123", email=None)


def _course_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        owner_id=None,
        titre="Suites numériques",
        description=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _block_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        position=0,
        type="texte",
        content={"markdown": ""},
        resource_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def one(self):
        [row] = self._rows
        return row

    def one_or_none(self):
        if not self._rows:
            return None
        [row] = self._rows
        return row


class _FakeSession:
    """FIFO des résultats de SELECT ; écritures tracées sans consommer."""

    def __init__(self, select_results=()):
        self._select_results = list(select_results)
        self.executed = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        if isinstance(stmt, Insert) and stmt._returning:
            return _FakeResult(self._select_results.pop(0))
        if isinstance(stmt, (Insert, Update, Delete)):
            return _FakeResult([])
        return _FakeResult(self._select_results.pop(0))

    async def commit(self):
        self.commits += 1


def _client(session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="prof-123", email=None, roles=frozenset(), claims={}
    )
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


def _inserts(session, table_name):
    return [
        (stmt, params)
        for stmt, params in session.executed
        if isinstance(stmt, Insert) and stmt.table.name == table_name
    ]


def _updates(session):
    return [(stmt, params) for stmt, params in session.executed if isinstance(stmt, Update)]


def _deletes(session):
    return [(stmt, params) for stmt, params in session.executed if isinstance(stmt, Delete)]


_COURSE_ID = uuid.uuid4()
_BLOCK_ID = uuid.uuid4()


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/courses", None),
        ("POST", "/api/v1/courses", {"titre": "x"}),
        ("GET", f"/api/v1/courses/{_COURSE_ID}", None),
        ("POST", f"/api/v1/courses/{_COURSE_ID}/blocks", {"type": "texte"}),
        ("PUT", f"/api/v1/courses/{_COURSE_ID}/blocks/order", {"block_ids": []}),
        (
            "PATCH",
            f"/api/v1/courses/{_COURSE_ID}/blocks/{_BLOCK_ID}",
            {"content": {"markdown": "x"}},
        ),
        ("DELETE", f"/api/v1/courses/{_COURSE_ID}/blocks/{_BLOCK_ID}", None),
    ],
)
def test_routes_requierent_auth(client: TestClient, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_liste_vide_court_circuite():
    user = _user_row()
    session = _FakeSession([[user], []])
    response = _client(session).get("/api/v1/courses")

    assert response.status_code == 200
    assert response.json() == []
    # 3 executes : upsert users, select user, select cours — pas de M2M/blocs.
    assert len(session.executed) == 3


def test_liste_ventile_classement_et_comptes():
    user = _user_row()
    c1, c2 = _course_row(), _course_row(description="Avec description")
    s1, s2, l1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = _FakeSession(
        [
            [user],
            [c1, c2],
            [(c1.id, s1), (c2.id, s2)],
            [(c1.id, l1)],
            [(c1.id, 3)],
        ]
    )
    response = _client(session).get("/api/v1/courses")

    assert response.status_code == 200
    body = response.json()
    assert [c["id"] for c in body] == [str(c1.id), str(c2.id)]  # ordre servi respecté
    assert body[0]["subject_ids"] == [str(s1)]
    assert body[1]["subject_ids"] == [str(s2)]
    assert body[0]["education_level_ids"] == [str(l1)]
    assert body[1]["education_level_ids"] == []
    assert body[0]["block_count"] == 3
    assert body[1]["block_count"] == 0  # absent du GROUP BY → 0


def test_creation_happy_path():
    user = _user_row()
    s1, s2, l1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], [s1, s2], [l1], [(_NOW, _NOW)]])
    payload = {
        "titre": "  Suites numériques  ",
        "description": "Premier chapitre",
        "subject_ids": [str(s1), str(s2)],
        "education_level_ids": [str(l1)],
    }
    response = _client(session).post("/api/v1/courses", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["titre"] == "Suites numériques"  # trimé par le schéma
    assert body["description"] == "Premier chapitre"
    assert body["subject_ids"] == [str(s1), str(s2)]
    assert body["education_level_ids"] == [str(l1)]
    assert body["block_count"] == 0
    assert body["created_at"] and body["updated_at"]

    [(stmt_course, _)] = _inserts(session, "courses")
    assert stmt_course._returning  # timestamps server_default relus en RETURNING
    [(_, params_matieres)] = _inserts(session, "course_subjects")
    assert [p["subject_id"] for p in params_matieres] == [s1, s2]
    [(_, params_niveaux)] = _inserts(session, "course_education_levels")
    assert [p["education_level_id"] for p in params_niveaux] == [l1]
    assert session.commits >= 1


def test_creation_sans_classement():
    user = _user_row()
    session = _FakeSession([[user], [], [], [(_NOW, _NOW)]])
    response = _client(session).post("/api/v1/courses", json={"titre": "Sans classement"})

    assert response.status_code == 201
    body = response.json()
    assert body["subject_ids"] == []
    assert body["education_level_ids"] == []
    # Aucun executemany sur liste vide (erreur SQLAlchemy sinon).
    assert _inserts(session, "course_subjects") == []
    assert _inserts(session, "course_education_levels") == []


def test_creation_matiere_inconnue():
    user = _user_row()
    session = _FakeSession([[user], []])  # lookup matières vide
    payload = {"titre": "x", "subject_ids": [str(uuid.uuid4())]}
    response = _client(session).post("/api/v1/courses", json=payload)

    assert response.status_code == 422
    assert "Matières inconnues" in response.json()["detail"]
    assert _inserts(session, "courses") == []


def test_creation_niveau_inconnu():
    user = _user_row()
    s1 = uuid.uuid4()
    session = _FakeSession([[user], [s1], []])  # lookup niveaux vide
    payload = {
        "titre": "x",
        "subject_ids": [str(s1)],
        "education_level_ids": [str(uuid.uuid4())],
    }
    response = _client(session).post("/api/v1/courses", json=payload)

    assert response.status_code == 422
    assert "Niveaux d'étude inconnus" in response.json()["detail"]
    assert _inserts(session, "courses") == []


@pytest.mark.parametrize(
    "payload",
    [
        {},  # titre manquant
        {"titre": ""},
        {"titre": "   "},  # blanc : rejeté après trim
        {"titre": "x" * 301},
        {"titre": "ok", "description": "d" * 2001},
    ],
)
def test_creation_payload_invalide_sans_acces_bdd(payload):
    session = _FakeSession()
    response = _client(session).post("/api/v1/courses", json=payload)
    assert response.status_code == 422
    assert session.executed == []


def test_creation_dedoublonne_les_ids():
    user = _user_row()
    s1 = uuid.uuid4()
    session = _FakeSession([[user], [s1], [], [(_NOW, _NOW)]])
    payload = {"titre": "x", "subject_ids": [str(s1), str(s1)]}
    response = _client(session).post("/api/v1/courses", json=payload)

    assert response.status_code == 201
    assert response.json()["subject_ids"] == [str(s1)]
    [(_, params)] = _inserts(session, "course_subjects")
    assert len(params) == 1


def test_detail_cours_non_possede():
    user = _user_row()
    session = _FakeSession([[user], []])  # select cours scopé owner → vide
    response = _client(session).get(f"/api/v1/courses/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_detail_avec_blocs_ordonnes():
    user = _user_row()
    course = _course_row()
    s1, l1 = uuid.uuid4(), uuid.uuid4()
    b1 = _block_row()
    b2 = _block_row(
        position=1, type="lien", content={"url": "https://ex.org", "titre": "", "fournisseur": None}
    )
    session = _FakeSession([[user], [course], [s1], [l1], [b1, b2]])
    response = _client(session).get(f"/api/v1/courses/{course.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["titre"] == course.titre
    assert body["subject_ids"] == [str(s1)]
    assert body["education_level_ids"] == [str(l1)]
    assert body["block_count"] == 2
    assert [b["id"] for b in body["blocks"]] == [str(b1.id), str(b2.id)]  # ordre servi
    assert body["blocks"][0] == {
        "id": str(b1.id),
        "position": 0,
        "type": "texte",
        "content": {"markdown": ""},
        "resource_id": None,
    }


@pytest.mark.parametrize(
    ("type_bloc", "contenu"),
    [
        ("texte", {"markdown": ""}),
        ("exercice", {"enonce": "", "questions": []}),
        ("lien", {"url": "", "titre": "", "fournisseur": None}),
    ],
)
def test_ajout_bloc_contenu_par_defaut(type_bloc, contenu):
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], [3]])  # position suivante servie : 3
    response = _client(session).post(
        f"/api/v1/courses/{course.id}/blocks", json={"type": type_bloc}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["type"] == type_bloc
    assert body["content"] == contenu
    assert body["position"] == 3
    assert body["resource_id"] is None
    assert len(_inserts(session, "blocks")) == 1
    assert course.updated_at != _NOW  # le cours remonte dans la liste
    assert session.commits >= 1


def test_ajout_premier_bloc_position_zero():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], [0]])  # coalesce(max+1, 0) sur cours vide
    response = _client(session).post(f"/api/v1/courses/{course.id}/blocks", json={"type": "texte"})

    assert response.status_code == 201
    assert response.json()["position"] == 0


@pytest.mark.parametrize("type_bloc", ["ressource", "inconnu"])
def test_ajout_bloc_type_refuse_sans_acces_bdd(type_bloc):
    # « ressource » exige un resource_id (upload S3, pas encore livré) : le
    # schéma ne l'accepte pas.
    session = _FakeSession()
    response = _client(session).post(
        f"/api/v1/courses/{uuid.uuid4()}/blocks", json={"type": type_bloc}
    )
    assert response.status_code == 422
    assert session.executed == []


def test_edition_contenu_bloc_texte():
    user = _user_row()
    course = _course_row()
    block = _block_row()
    session = _FakeSession([[user], [course], [block]])
    payload = {"content": {"markdown": "## Suites\nDéfinition d'une suite."}}
    response = _client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}", json=payload
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(block.id),
        "position": 0,
        "type": "texte",
        "content": {"markdown": "## Suites\nDéfinition d'une suite."},
        "resource_id": None,
    }
    # Écriture via l'unité de travail ORM (mutation d'attribut), pas d'Update Core.
    assert block.content == {"markdown": "## Suites\nDéfinition d'une suite."}
    assert _updates(session) == []
    assert course.updated_at != _NOW
    assert session.commits >= 1


def test_edition_contenu_cours_non_possede():
    user = _user_row()
    session = _FakeSession([[user], []])
    response = _client(session).patch(
        f"/api/v1/courses/{uuid.uuid4()}/blocks/{uuid.uuid4()}",
        json={"content": {"markdown": "x"}},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_edition_contenu_bloc_introuvable():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])
    response = _client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{uuid.uuid4()}",
        json={"content": {"markdown": "x"}},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Bloc introuvable"


def test_edition_contenu_refuse_les_types_non_texte():
    user = _user_row()
    course = _course_row()
    contenu_initial = {"url": "https://ex.org", "titre": "", "fournisseur": None}
    block = _block_row(type="lien", content=contenu_initial)
    session = _FakeSession([[user], [course], [block]])
    response = _client(session).patch(
        f"/api/v1/courses/{course.id}/blocks/{block.id}",
        json={"content": {"markdown": "x"}},
    )

    assert response.status_code == 422
    assert "Seuls les blocs" in response.json()["detail"]
    assert block.content == contenu_initial
    assert course.updated_at == _NOW
    # Seul commit : celui de get_or_create_by_sub (upsert auth) — pas d'écriture cours.
    assert session.commits == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},  # content manquant
        {"content": {}},  # markdown manquant
        {"content": {"markdown": None}},
        {"content": {"markdown": "x" * 100_001}},  # trop long
        {"content": {"markdown": "x", "html": "<b>"}},  # clé en trop (extra=forbid)
    ],
)
def test_edition_contenu_payload_invalide_sans_acces_bdd(payload):
    session = _FakeSession()
    response = _client(session).patch(
        f"/api/v1/courses/{uuid.uuid4()}/blocks/{uuid.uuid4()}", json=payload
    )
    assert response.status_code == 422
    assert session.executed == []


def test_suppression_bloc():
    user = _user_row()
    course = _course_row()
    block_id = uuid.uuid4()
    session = _FakeSession([[user], [course], [block_id]])
    response = _client(session).delete(f"/api/v1/courses/{course.id}/blocks/{block_id}")

    assert response.status_code == 204
    [(stmt, _)] = _deletes(session)
    assert stmt.table.name == "blocks"
    assert course.updated_at != _NOW


def test_suppression_bloc_inexistant():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])  # bloc absent du cours
    response = _client(session).delete(f"/api/v1/courses/{course.id}/blocks/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Bloc introuvable"
    assert _deletes(session) == []


def test_suppression_cours_non_possede():
    user = _user_row()
    session = _FakeSession([[user], []])
    response = _client(session).delete(f"/api/v1/courses/{uuid.uuid4()}/blocks/{uuid.uuid4()}")

    assert response.status_code == 404
    assert _deletes(session) == []


def test_reordonnancement_reecrit_les_positions():
    user = _user_row()
    course = _course_row()
    b1, b2, b3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], [course], [b1, b2, b3]])
    payload = {"block_ids": [str(b3), str(b1), str(b2)]}
    response = _client(session).put(f"/api/v1/courses/{course.id}/blocks/order", json=payload)

    assert response.status_code == 204
    [(stmt, params)] = _updates(session)  # un seul executemany
    assert stmt.table.name == "blocks"
    assert params == [
        {"b_id": b3, "b_position": 0},
        {"b_id": b1, "b_position": 1},
        {"b_id": b2, "b_position": 2},
    ]
    assert course.updated_at != _NOW


def test_reordonnancement_liste_incomplete_ou_etrangere():
    user = _user_row()
    course = _course_row()
    b1, b2 = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], [course], [b1, b2]])
    payload = {"block_ids": [str(b1), str(uuid.uuid4())]}  # b2 manquant + id étranger
    response = _client(session).put(f"/api/v1/courses/{course.id}/blocks/order", json=payload)

    assert response.status_code == 422
    assert "exactement les blocs" in response.json()["detail"]
    assert _updates(session) == []


def test_reordonnancement_doublons_sans_acces_bdd():
    b1 = uuid.uuid4()
    session = _FakeSession()
    payload = {"block_ids": [str(b1), str(b1)]}
    response = _client(session).put(f"/api/v1/courses/{uuid.uuid4()}/blocks/order", json=payload)

    assert response.status_code == 422
    assert session.executed == []


def test_reordonnancement_cours_vide():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])
    response = _client(session).put(
        f"/api/v1/courses/{course.id}/blocks/order", json={"block_ids": []}
    )

    assert response.status_code == 204
    assert _updates(session) == []
