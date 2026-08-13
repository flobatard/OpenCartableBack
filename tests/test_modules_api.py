"""Routes /courses/{id}/modules — bibliothèque de modules interactifs, pur BDD.

Même motif que tests/test_resources_api.py : fausse session FIFO (résultats
des SELECT servis dans l'ordre des ``execute`` du service, documenté dans
app/modules/service.py). Le premier ``[user]`` de la file est consommé par
``get_or_create_by_sub`` (upsert auth, 1 commit). Aucun storage : le code des
modules vit en base.
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
from app.modules.schemas import MAX_CODE_LENGTH

_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
# Sérialisation JSON de _NOW par FastAPI (suffixe « Z », pas « +00:00 »).
_NOW_JSON = "2026-07-07T12:00:00Z"


def _user_row():
    return SimpleNamespace(id=uuid.uuid4(), sub="prof-123", email=None)


def _course_row(**overrides):
    defaults = dict(id=uuid.uuid4(), owner_id=None, updated_at=_NOW)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _module_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=None,
        titre="Quiz interactif",
        html="<p>Salut</p>",
        css="p { color: red; }",
        js="console.log('ok')",
        created_at=_NOW,
        updated_at=_NOW,
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


def _deletes(session):
    return [stmt for stmt, _ in session.executed if isinstance(stmt, Delete)]


_COURSE_ID = uuid.uuid4()
_MODULE_ID = uuid.uuid4()


# --- Auth requise sur toutes les routes ---------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", f"/api/v1/courses/{_COURSE_ID}/modules", None),
        ("POST", f"/api/v1/courses/{_COURSE_ID}/modules", {"titre": "Quiz"}),
        ("GET", f"/api/v1/courses/{_COURSE_ID}/modules/{_MODULE_ID}", None),
        ("PATCH", f"/api/v1/courses/{_COURSE_ID}/modules/{_MODULE_ID}", {
            "titre": "Quiz 2",
        }),
        ("DELETE", f"/api/v1/courses/{_COURSE_ID}/modules/{_MODULE_ID}", None),
    ],
)
def test_auth_requise(method, path, body):
    response = TestClient(create_app()).request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


# --- Cours introuvable / d'autrui : 404 sur chaque route ----------------------


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    [
        ("GET", "", None),
        ("POST", "", {"titre": "Quiz"}),
        ("GET", f"/{_MODULE_ID}", None),
        ("PATCH", f"/{_MODULE_ID}", {"titre": "Quiz 2"}),
        ("DELETE", f"/{_MODULE_ID}", None),
    ],
)
def test_cours_autrui_introuvable(method, path_suffix, body):
    # Le select scopé owner_id ne retourne rien : 404, jamais 403.
    user = _user_row()
    session = _FakeSession([[user], []])
    response = _client(session).request(
        method, f"/api/v1/courses/{_COURSE_ID}/modules{path_suffix}", json=body
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"
    assert session.commits == 1  # upsert auth seulement


# --- Liste --------------------------------------------------------------------


def test_liste_modules_sans_code():
    user = _user_row()
    course = _course_row()
    m1 = _module_row(course_id=course.id)
    m2 = _module_row(course_id=course.id, titre="Simulation")
    # FIFO : cours (contrôle de propriété), puis modules (tri côté SQL).
    session = _FakeSession([[user], [course], [m1, m2]])
    response = _client(session).get(f"/api/v1/courses/{course.id}/modules")

    assert response.status_code == 200
    body = response.json()
    assert [m["id"] for m in body] == [str(m1.id), str(m2.id)]  # ordre servi
    # ModuleSummary : jamais le code dans la liste (payload léger).
    assert body[0] == {
        "id": str(m1.id),
        "titre": "Quiz interactif",
        "created_at": _NOW_JSON,
        "updated_at": _NOW_JSON,
    }
    assert session.commits == 1  # lecture seule (upsert auth seulement)


# --- Création -----------------------------------------------------------------


def test_creation_module_ok():
    user = _user_row()
    course = _course_row()
    # FIFO : cours, puis timestamps servis par l'insert RETURNING.
    session = _FakeSession([[user], [course], [(_NOW, _NOW)]])
    payload = {"titre": "Quiz", "html": "<p>Q1</p>", "css": "", "js": "let a = 1"}
    response = _client(session).post(
        f"/api/v1/courses/{course.id}/modules", json=payload
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["titre"] == "Quiz"
    assert body["html"] == "<p>Q1</p>"
    assert body["css"] == ""
    assert body["js"] == "let a = 1"

    [(stmt, _)] = _inserts(session, "modules")
    valeurs = stmt.compile().params
    assert valeurs["titre"] == "Quiz"
    assert valeurs["html"] == "<p>Q1</p>"
    assert course.updated_at != _NOW  # le cours remonte dans la liste
    assert session.commits >= 1


def test_creation_module_code_vide_par_defaut():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], [(_NOW, _NOW)]])
    response = _client(session).post(
        f"/api/v1/courses/{course.id}/modules", json={"titre": "Quiz"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["html"] == ""
    assert body["css"] == ""
    assert body["js"] == ""


@pytest.mark.parametrize(
    "payload",
    [
        {},  # titre requis
        {"titre": "   "},  # titre blanc
        {"titre": "Quiz", "entrypoint": "index.html"},  # clé en trop (extra=forbid)
        {"titre": "Quiz", "js": "x" * (MAX_CODE_LENGTH + 1)},  # code trop long
    ],
)
def test_creation_module_payload_invalide_sans_acces_bdd(payload):
    session = _FakeSession()
    response = _client(session).post(
        f"/api/v1/courses/{uuid.uuid4()}/modules", json=payload
    )
    assert response.status_code == 422
    assert session.executed == []


# --- Détail -------------------------------------------------------------------


def test_detail_module_avec_code():
    user = _user_row()
    course = _course_row()
    module = _module_row(course_id=course.id)
    # FIFO : cours, puis module scopé cours.
    session = _FakeSession([[user], [course], [module]])
    response = _client(session).get(
        f"/api/v1/courses/{course.id}/modules/{module.id}"
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(module.id),
        "titre": "Quiz interactif",
        "html": "<p>Salut</p>",
        "css": "p { color: red; }",
        "js": "console.log('ok')",
        "created_at": _NOW_JSON,
        "updated_at": _NOW_JSON,
    }
    assert session.commits == 1  # lecture seule


def test_detail_module_inconnu_ou_autre_cours():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])
    response = _client(session).get(
        f"/api/v1/courses/{course.id}/modules/{uuid.uuid4()}"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Module introuvable"


# --- Édition partielle --------------------------------------------------------


def test_patch_titre_seul():
    user = _user_row()
    course = _course_row()
    module = _module_row(course_id=course.id)
    session = _FakeSession([[user], [course], [module]])
    response = _client(session).patch(
        f"/api/v1/courses/{course.id}/modules/{module.id}",
        json={"titre": "Quiz renommé"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["titre"] == "Quiz renommé"
    assert body["html"] == "<p>Salut</p>"  # intact
    # Écriture via l'unité de travail ORM (mutation d'attribut), pas d'Update Core.
    assert module.titre == "Quiz renommé"
    assert module.html == "<p>Salut</p>"
    assert not any(isinstance(stmt, Update) for stmt, _ in session.executed)
    assert course.updated_at != _NOW
    assert session.commits >= 1


def test_patch_code_seul():
    # L'autosave de l'éditeur envoie html/css/js sans le titre.
    user = _user_row()
    course = _course_row()
    module = _module_row(course_id=course.id)
    session = _FakeSession([[user], [course], [module]])
    response = _client(session).patch(
        f"/api/v1/courses/{course.id}/modules/{module.id}",
        json={"html": "<p>V2</p>", "css": "", "js": "let b = 2"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["html"] == "<p>V2</p>"
    assert body["css"] == ""
    assert body["js"] == "let b = 2"
    assert body["titre"] == "Quiz interactif"  # intact
    assert module.html == "<p>V2</p>"
    assert module.titre == "Quiz interactif"
    # updated_at posé côté Python : la réponse du PATCH est fraîche (le
    # onupdate SQL ne tirerait qu'au flush, après construction du read).
    assert body["updated_at"] != _NOW_JSON
    assert module.updated_at != _NOW
    assert course.updated_at != _NOW


@pytest.mark.parametrize(
    "payload",
    [
        {},  # au moins un champ requis
        {"titre": None},  # un module a toujours un titre
        {"titre": "   "},  # titre blanc
        {"html": None},  # null n'efface pas un code (vider = envoyer "")
        {"js": None},  # idem pour chaque champ de code
        {"entrypoint": "index.html"},  # clé en trop (extra=forbid)
        {"html": "x" * (MAX_CODE_LENGTH + 1)},  # code trop long
    ],
)
def test_patch_payload_invalide_sans_acces_bdd(payload):
    session = _FakeSession()
    response = _client(session).patch(
        f"/api/v1/courses/{uuid.uuid4()}/modules/{uuid.uuid4()}", json=payload
    )
    assert response.status_code == 422
    assert session.executed == []


def test_patch_module_inconnu_ou_autre_cours():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])
    response = _client(session).patch(
        f"/api/v1/courses/{course.id}/modules/{uuid.uuid4()}",
        json={"titre": "Quiz"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Module introuvable"
    assert course.updated_at == _NOW


# --- Suppression --------------------------------------------------------------


def test_suppression_module():
    user = _user_row()
    course = _course_row()
    module = _module_row(course_id=course.id)
    # FIFO : cours, module (scopé cours), puis delete (non consommant).
    session = _FakeSession([[user], [course], [module]])
    response = _client(session).delete(
        f"/api/v1/courses/{course.id}/modules/{module.id}"
    )

    assert response.status_code == 204
    assert len(_deletes(session)) == 1
    # Les blocs module pointeurs partent par FK CASCADE : aucun execute
    # supplémentaire, et rien à purger côté storage (code en base).
    assert course.updated_at != _NOW
    assert session.commits >= 2  # upsert auth + delete


def test_suppression_module_inconnu():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])
    response = _client(session).delete(
        f"/api/v1/courses/{course.id}/modules/{uuid.uuid4()}"
    )

    assert response.status_code == 404
    assert _deletes(session) == []
