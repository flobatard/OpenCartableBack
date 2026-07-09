"""Routes /courses/{id}/resources — flow d'upload S3 presigned, aucun réseau.

Même motif que tests/test_courses_api.py : fausse session FIFO (résultats des
SELECT servis dans l'ordre des ``execute`` du service) + faux client S3 injecté
via ``get_storage`` (aucun appel boto3 réel). Le premier ``[user]`` de la file
est consommé par ``get_or_create_by_sub`` (upsert auth, 1 commit).
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete, Insert, Update

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.storage import get_storage
from app.main import create_app

_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def _user_row():
    return SimpleNamespace(id=uuid.uuid4(), sub="prof-123", email=None)


def _course_row(**overrides):
    defaults = dict(id=uuid.uuid4(), owner_id=None, updated_at=_NOW)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _resource_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=None,
        type="document",
        s3_key="uuid/schema.pdf",
        nom_original="schema.pdf",
        taille=1024,
        mime="application/pdf",
        statut="en_attente",
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


class _FakeStorage:
    """Faux client S3 : URLs déterministes, HEAD configurable, pas de réseau."""

    def __init__(self, head_result=None):
        self._head_result = head_result
        self.put_calls: list[tuple[str, str]] = []
        self.get_calls: list[tuple[str, str]] = []
        self.head_calls: list[str] = []
        self.deleted: list[str] = []

    def presign_put(self, s3_key, content_type):
        self.put_calls.append((s3_key, content_type))
        return f"https://s3.test/put/{s3_key}"

    def presign_get(self, s3_key, nom_original):
        self.get_calls.append((s3_key, nom_original))
        return f"https://s3.test/get/{s3_key}"

    async def head(self, s3_key):
        self.head_calls.append(s3_key)
        return self._head_result

    async def delete_many(self, s3_keys):
        self.deleted.extend(s3_keys)


def _client(session, storage=None) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="prof-123", email=None, roles=frozenset(), claims={}
    )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_storage] = lambda: storage or _FakeStorage()
    return TestClient(app)


def _inserts(session, table_name):
    return [
        (stmt, params)
        for stmt, params in session.executed
        if isinstance(stmt, Insert) and stmt.table.name == table_name
    ]


_COURSE_ID = uuid.uuid4()
_RESOURCE_ID = uuid.uuid4()


# --- Auth requise sur toutes les routes ---------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", f"/api/v1/courses/{_COURSE_ID}/resources", {
            "nom_original": "x.pdf", "mime": "application/pdf", "taille": 10,
            "type": "document",
        }),
        ("POST", f"/api/v1/courses/{_COURSE_ID}/resources/{_RESOURCE_ID}/confirm", {}),
        ("GET", f"/api/v1/courses/{_COURSE_ID}/resources/{_RESOURCE_ID}/download", None),
    ],
)
def test_auth_requise(method, path, body):
    # Pas d'override d'auth : 401 + WWW-Authenticate, aucune route S3 publique.
    response = TestClient(create_app()).request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


# --- Presign upload -----------------------------------------------------------


def test_presign_upload_ok():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course]])
    storage = _FakeStorage()
    payload = {
        "nom_original": "schema.pdf",
        "mime": "application/pdf",
        "taille": 2048,
        "type": "document",
    }
    response = _client(session, storage).post(
        f"/api/v1/courses/{course.id}/resources", json=payload
    )

    assert response.status_code == 201
    body = response.json()
    assert body["statut"] == "en_attente"
    assert body["expires_in"] == settings.S3_PRESIGN_PUT_TTL
    # Clé S3 plate « <resource_id>/<nom-sanitizé> ».
    assert body["s3_key"] == f"{body['resource_id']}/schema.pdf"
    assert body["upload_url"] == f"https://s3.test/put/{body['s3_key']}"
    assert storage.put_calls == [(body["s3_key"], "application/pdf")]

    [(stmt, _)] = _inserts(session, "resources")
    valeurs = stmt.compile().params
    assert valeurs["statut"] == "en_attente"
    assert valeurs["nom_original"] == "schema.pdf"
    assert valeurs["taille"] == 2048
    assert session.commits >= 1


def test_presign_upload_sanitize_nom():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course]])
    payload = {
        "nom_original": "../etc/mon cours (final).pdf",
        "mime": "application/pdf",
        "taille": 10,
        "type": "document",
    }
    response = _client(session).post(
        f"/api/v1/courses/{course.id}/resources", json=payload
    )

    assert response.status_code == 201
    s3_key = response.json()["s3_key"]
    # Traversée neutralisée, suites de chars interdits → un seul « _ », basename seul.
    assert s3_key.endswith("/mon_cours_final_.pdf")
    assert ".." not in s3_key


@pytest.mark.parametrize(
    "payload",
    [
        {"nom_original": "x.pdf", "mime": "application/pdf", "taille": -1, "type": "document"},
        {"nom_original": "x.pdf", "mime": "", "taille": 10, "type": "document"},
        {"nom_original": "  ", "mime": "application/pdf", "taille": 10, "type": "document"},
        {"nom_original": "x.zip", "mime": "application/zip", "taille": 10, "type": "module"},
        {"nom_original": "x.pdf", "mime": "application/pdf",
         "taille": settings.S3_MAX_UPLOAD_BYTES + 1, "type": "document"},
    ],
)
def test_presign_payload_invalide_sans_acces_bdd(payload):
    session = _FakeSession()
    response = _client(session).post(
        f"/api/v1/courses/{uuid.uuid4()}/resources", json=payload
    )
    assert response.status_code == 422
    assert session.executed == []


def test_presign_cours_autrui_404():
    user = _user_row()
    session = _FakeSession([[user], []])  # select cours scopé owner → vide
    payload = {
        "nom_original": "x.pdf", "mime": "application/pdf", "taille": 10, "type": "document",
    }
    response = _client(session).post(
        f"/api/v1/courses/{uuid.uuid4()}/resources", json=payload
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"
    assert _inserts(session, "resources") == []


# --- Confirmation d'upload ----------------------------------------------------


def test_confirm_ok_cree_bloc_ressource():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, taille=2048)
    session = _FakeSession([[user], [course], [resource], [0]])
    storage = _FakeStorage(head_result={"ContentLength": 2048})
    payload = {"titre": "Le schéma", "legende": "Figure 1", "affichage": "inline"}
    response = _client(session, storage).post(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/confirm", json=payload
    )

    assert response.status_code == 201
    body = response.json()
    assert body["type"] == "ressource"
    assert body["resource_id"] == str(resource.id)
    assert body["position"] == 0
    assert body["titre"] == "Le schéma"
    assert body["content"] == {"legende": "Figure 1", "affichage": "inline"}

    assert storage.head_calls == [resource.s3_key]
    [(stmt, _)] = _inserts(session, "blocks")
    valeurs = stmt.compile().params
    assert valeurs["type"] == "ressource"
    assert valeurs["resource_id"] == resource.id
    # La ressource passe à disponible (mutation ORM, flush au commit).
    assert resource.statut == "disponible"
    assert course.updated_at != _NOW
    assert session.commits >= 1


def test_confirm_objet_absent_409():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id)
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage(head_result=None)  # objet jamais uploadé
    response = _client(session, storage).post(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/confirm", json={}
    )

    assert response.status_code == 409
    assert _inserts(session, "blocks") == []
    assert resource.statut == "en_attente"


def test_confirm_taille_incoherente_409():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, taille=2048)
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage(head_result={"ContentLength": 999})
    response = _client(session, storage).post(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/confirm", json={}
    )

    assert response.status_code == 409
    assert _inserts(session, "blocks") == []


def test_confirm_deja_confirmee_409():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, statut="disponible")
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage(head_result={"ContentLength": 1024})
    response = _client(session, storage).post(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/confirm", json={}
    )

    assert response.status_code == 409
    assert storage.head_calls == []  # court-circuit avant HEAD S3
    assert _inserts(session, "blocks") == []


def test_confirm_ressource_introuvable_404():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])  # ressource absente du cours
    response = _client(session).post(
        f"/api/v1/courses/{course.id}/resources/{uuid.uuid4()}/confirm", json={}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Ressource introuvable"


def test_confirm_cours_autrui_404():
    user = _user_row()
    session = _FakeSession([[user], []])
    response = _client(session).post(
        f"/api/v1/courses/{uuid.uuid4()}/resources/{uuid.uuid4()}/confirm", json={}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


# --- Lecture (presign GET) ----------------------------------------------------


def test_download_ok():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, statut="disponible")
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage()
    response = _client(session, storage).get(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/download"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["download_url"] == f"https://s3.test/get/{resource.s3_key}"
    assert body["expires_in"] == settings.S3_PRESIGN_GET_TTL
    assert storage.get_calls == [(resource.s3_key, resource.nom_original)]


def test_download_non_disponible_409():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, statut="en_attente")
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage()
    response = _client(session, storage).get(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/download"
    )

    assert response.status_code == 409
    assert storage.get_calls == []


def test_download_ressource_introuvable_404():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])
    response = _client(session).get(
        f"/api/v1/courses/{course.id}/resources/{uuid.uuid4()}/download"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Ressource introuvable"
