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
from sqlalchemy.sql.dml import Update

from app.main import create_app
from app.modules.schemas import MAX_CODE_LENGTH
from tests.fakes import FakeSession, deletes, inserts, make_client

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
        title="Quiz interactif",
        html="<p>Salut</p>",
        css="p { color: red; }",
        js="console.log('ok')",
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


_COURSE_ID = uuid.uuid4()
_MODULE_ID = uuid.uuid4()


# --- Auth requise sur toutes les routes ---------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", f"/api/v1/courses/{_COURSE_ID}/modules", None),
        ("POST", f"/api/v1/courses/{_COURSE_ID}/modules", {"title": "Quiz"}),
        ("GET", f"/api/v1/courses/{_COURSE_ID}/modules/{_MODULE_ID}", None),
        ("PATCH", f"/api/v1/courses/{_COURSE_ID}/modules/{_MODULE_ID}", {
            "title": "Quiz 2",
        }),
        ("DELETE", f"/api/v1/courses/{_COURSE_ID}/modules/{_MODULE_ID}", None),
    ],
)
def test_auth_required(method, path, body):
    response = TestClient(create_app()).request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


# --- Cours introuvable / d'autrui : 404 sur chaque route ----------------------


@pytest.mark.parametrize(
    ("method", "path_suffix", "body"),
    [
        ("GET", "", None),
        ("POST", "", {"title": "Quiz"}),
        ("GET", f"/{_MODULE_ID}", None),
        ("PATCH", f"/{_MODULE_ID}", {"title": "Quiz 2"}),
        ("DELETE", f"/{_MODULE_ID}", None),
    ],
)
def test_foreign_course_not_found(method, path_suffix, body):
    # Le select scopé owner_id ne retourne rien : 404, jamais 403.
    user = _user_row()
    session = FakeSession([[user], []])
    response = make_client(session).request(
        method, f"/api/v1/courses/{_COURSE_ID}/modules{path_suffix}", json=body
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"
    assert session.commits == 1  # upsert auth seulement


# --- Liste --------------------------------------------------------------------


def test_list_modules_without_code():
    user = _user_row()
    course = _course_row()
    m1 = _module_row(course_id=course.id)
    m2 = _module_row(course_id=course.id, title="Simulation")
    # FIFO : cours (contrôle de propriété), puis modules (tri côté SQL).
    session = FakeSession([[user], [course], [m1, m2]])
    response = make_client(session).get(f"/api/v1/courses/{course.id}/modules")

    assert response.status_code == 200
    body = response.json()
    assert [m["id"] for m in body] == [str(m1.id), str(m2.id)]  # ordre servi
    # ModuleSummary : jamais le code dans la liste (payload léger).
    assert body[0] == {
        "id": str(m1.id),
        "title": "Quiz interactif",
        "created_at": _NOW_JSON,
        "updated_at": _NOW_JSON,
    }
    assert session.commits == 1  # lecture seule (upsert auth seulement)


# --- Création -----------------------------------------------------------------


def test_create_module_ok():
    user = _user_row()
    course = _course_row()
    # FIFO : cours, puis timestamps servis par l'insert RETURNING.
    session = FakeSession([[user], [course], [(_NOW, _NOW)]])
    payload = {"title": "Quiz", "html": "<p>Q1</p>", "css": "", "js": "let a = 1"}
    response = make_client(session).post(
        f"/api/v1/courses/{course.id}/modules", json=payload
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["title"] == "Quiz"
    assert body["html"] == "<p>Q1</p>"
    assert body["css"] == ""
    assert body["js"] == "let a = 1"

    [(stmt, _)] = inserts(session, "modules")
    values = stmt.compile().params
    assert values["title"] == "Quiz"
    assert values["html"] == "<p>Q1</p>"
    assert course.updated_at != _NOW  # le cours remonte dans la liste
    assert session.commits >= 1


def test_create_module_empty_code_by_default():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], [(_NOW, _NOW)]])
    response = make_client(session).post(
        f"/api/v1/courses/{course.id}/modules", json={"title": "Quiz"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["html"] == ""
    assert body["css"] == ""
    assert body["js"] == ""


@pytest.mark.parametrize(
    "payload",
    [
        {},  # title requis
        {"title": "   "},  # titre blanc
        {"title": "Quiz", "entrypoint": "index.html"},  # clé en trop (extra=forbid)
        {"title": "Quiz", "js": "x" * (MAX_CODE_LENGTH + 1)},  # code trop long
    ],
)
def test_create_module_invalid_payload_without_db_access(payload):
    session = FakeSession()
    response = make_client(session).post(
        f"/api/v1/courses/{uuid.uuid4()}/modules", json=payload
    )
    assert response.status_code == 422
    assert session.executed == []


# --- Détail -------------------------------------------------------------------


def test_module_detail_with_code():
    user = _user_row()
    course = _course_row()
    module = _module_row(course_id=course.id)
    # FIFO : cours, puis module scopé cours.
    session = FakeSession([[user], [course], [module]])
    response = make_client(session).get(
        f"/api/v1/courses/{course.id}/modules/{module.id}"
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": str(module.id),
        "title": "Quiz interactif",
        "html": "<p>Salut</p>",
        "css": "p { color: red; }",
        "js": "console.log('ok')",
        "created_at": _NOW_JSON,
        "updated_at": _NOW_JSON,
    }
    assert session.commits == 1  # lecture seule


def test_module_detail_unknown_or_other_course():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], []])
    response = make_client(session).get(
        f"/api/v1/courses/{course.id}/modules/{uuid.uuid4()}"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Module introuvable"


# --- Édition partielle --------------------------------------------------------


def test_patch_title_only():
    user = _user_row()
    course = _course_row()
    module = _module_row(course_id=course.id)
    session = FakeSession([[user], [course], [module]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/modules/{module.id}",
        json={"title": "Quiz renommé"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Quiz renommé"
    assert body["html"] == "<p>Salut</p>"  # intact
    # Écriture via l'unité de travail ORM (mutation d'attribut), pas d'Update Core.
    assert module.title == "Quiz renommé"
    assert module.html == "<p>Salut</p>"
    assert not any(isinstance(stmt, Update) for stmt, _ in session.executed)
    assert course.updated_at != _NOW
    assert session.commits >= 1


def test_patch_code_only():
    # L'autosave de l'éditeur envoie html/css/js sans le titre.
    user = _user_row()
    course = _course_row()
    module = _module_row(course_id=course.id)
    session = FakeSession([[user], [course], [module]])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/modules/{module.id}",
        json={"html": "<p>V2</p>", "css": "", "js": "let b = 2"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["html"] == "<p>V2</p>"
    assert body["css"] == ""
    assert body["js"] == "let b = 2"
    assert body["title"] == "Quiz interactif"  # intact
    assert module.html == "<p>V2</p>"
    assert module.title == "Quiz interactif"
    # updated_at posé côté Python : la réponse du PATCH est fraîche (le
    # onupdate SQL ne tirerait qu'au flush, après construction du read).
    assert body["updated_at"] != _NOW_JSON
    assert module.updated_at != _NOW
    assert course.updated_at != _NOW


@pytest.mark.parametrize(
    "payload",
    [
        {},  # au moins un champ requis
        {"title": None},  # un module a toujours un titre
        {"title": "   "},  # titre blanc
        {"html": None},  # null n'efface pas un code (vider = envoyer "")
        {"js": None},  # idem pour chaque champ de code
        {"entrypoint": "index.html"},  # clé en trop (extra=forbid)
        {"html": "x" * (MAX_CODE_LENGTH + 1)},  # code trop long
    ],
)
def test_patch_invalid_payload_without_db_access(payload):
    session = FakeSession()
    response = make_client(session).patch(
        f"/api/v1/courses/{uuid.uuid4()}/modules/{uuid.uuid4()}", json=payload
    )
    assert response.status_code == 422
    assert session.executed == []


def test_patch_unknown_or_other_course_module():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], []])
    response = make_client(session).patch(
        f"/api/v1/courses/{course.id}/modules/{uuid.uuid4()}",
        json={"title": "Quiz"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Module introuvable"
    assert course.updated_at == _NOW


# --- Suppression --------------------------------------------------------------


def test_delete_module():
    user = _user_row()
    course = _course_row()
    module = _module_row(course_id=course.id)
    # FIFO : cours, module (scopé cours), puis delete (non consommant).
    session = FakeSession([[user], [course], [module]])
    response = make_client(session).delete(
        f"/api/v1/courses/{course.id}/modules/{module.id}"
    )

    assert response.status_code == 204
    assert len(deletes(session)) == 1
    # Les blocs module pointeurs partent par FK CASCADE : aucun execute
    # supplémentaire, et rien à purger côté storage (code en base).
    assert course.updated_at != _NOW
    assert session.commits >= 2  # upsert auth + delete


def test_delete_unknown_module():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], []])
    response = make_client(session).delete(
        f"/api/v1/courses/{course.id}/modules/{uuid.uuid4()}"
    )

    assert response.status_code == 404
    assert deletes(session) == []
