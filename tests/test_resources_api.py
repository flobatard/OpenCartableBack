"""Routes /courses/{id}/resources — bibliothèque + flow presigned, aucun réseau.

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
        original_name="schema.pdf",
        size=1024,
        mime="application/pdf",
        status="pending",
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


class _FakeStorage:
    """Faux client S3 : URLs déterministes, HEAD configurable, pas de réseau."""

    def __init__(self, head_result=None):
        self._head_result = head_result
        self.put_calls: list[tuple[str, str]] = []
        self.get_calls: list[tuple[str, str]] = []
        self.inline_calls: list[bool] = []
        self.head_calls: list[str] = []
        self.deleted: list[str] = []

    def presign_put(self, s3_key, content_type):
        self.put_calls.append((s3_key, content_type))
        return f"https://s3.test/put/{s3_key}"

    def presign_get(self, s3_key, original_name, inline=False):
        self.get_calls.append((s3_key, original_name))
        self.inline_calls.append(inline)
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
        ("GET", f"/api/v1/courses/{_COURSE_ID}/resources", None),
        ("POST", f"/api/v1/courses/{_COURSE_ID}/resources", {
            "original_name": "x.pdf", "mime": "application/pdf", "size": 10,
            "type": "document",
        }),
        ("POST", f"/api/v1/courses/{_COURSE_ID}/resources/{_RESOURCE_ID}/confirm", None),
        ("PATCH", f"/api/v1/courses/{_COURSE_ID}/resources/{_RESOURCE_ID}", {
            "original_name": "y.pdf",
        }),
        ("DELETE", f"/api/v1/courses/{_COURSE_ID}/resources/{_RESOURCE_ID}", None),
        ("GET", f"/api/v1/courses/{_COURSE_ID}/resources/{_RESOURCE_ID}/download", None),
        (
            "GET",
            f"/api/v1/courses/{_COURSE_ID}/resources/{_RESOURCE_ID}/download"
            "?disposition=inline",
            None,
        ),
    ],
)
def test_auth_required(method, path, body):
    # Pas d'override d'auth : 401 + WWW-Authenticate. Toutes les routes
    # ressources exigent l'auth — S3 n'est jamais exposé sans Bearer.
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
        "original_name": "schema.pdf",
        "mime": "application/pdf",
        "size": 2048,
        "type": "document",
    }
    response = _client(session, storage).post(
        f"/api/v1/courses/{course.id}/resources", json=payload
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["expires_in"] == settings.S3_PRESIGN_PUT_TTL
    # Clé S3 préfixée cours « courses/<course_id>/resources/<resource_id>/<nom-sanitizé> »
    # (nettoyage par préfixe quand un cours disparaît).
    assert body["s3_key"] == f"courses/{course.id}/resources/{body['resource_id']}/schema.pdf"
    assert body["upload_url"] == f"https://s3.test/put/{body['s3_key']}"
    assert storage.put_calls == [(body["s3_key"], "application/pdf")]

    [(stmt, _)] = _inserts(session, "resources")
    values = stmt.compile().params
    assert values["status"] == "pending"
    assert values["original_name"] == "schema.pdf"
    assert values["size"] == 2048
    assert session.commits >= 1


def test_presign_upload_sanitizes_name():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course]])
    payload = {
        "original_name": "../etc/mon cours (final).pdf",
        "mime": "application/pdf",
        "size": 10,
        "type": "document",
    }
    response = _client(session).post(
        f"/api/v1/courses/{course.id}/resources", json=payload
    )

    assert response.status_code == 201
    s3_key = response.json()["s3_key"]
    # Traversée neutralisée, suites de chars interdits → un seul « _ », basename seul.
    assert s3_key.startswith(f"courses/{course.id}/resources/")
    assert s3_key.endswith("/mon_cours_final_.pdf")
    assert ".." not in s3_key


@pytest.mark.parametrize(
    "payload",
    [
        {"original_name": "x.pdf", "mime": "application/pdf", "size": -1, "type": "document"},
        {"original_name": "x.pdf", "mime": "", "size": 10, "type": "document"},
        {"original_name": "  ", "mime": "application/pdf", "size": 10, "type": "document"},
        {"original_name": "x.zip", "mime": "application/zip", "size": 10, "type": "module"},
        {"original_name": "x.pdf", "mime": "application/pdf",
         "size": settings.S3_MAX_UPLOAD_BYTES + 1, "type": "document"},
    ],
)
def test_presign_invalid_payload_without_db_access(payload):
    session = _FakeSession()
    response = _client(session).post(
        f"/api/v1/courses/{uuid.uuid4()}/resources", json=payload
    )
    assert response.status_code == 422
    assert session.executed == []


def test_presign_foreign_course_404():
    user = _user_row()
    session = _FakeSession([[user], []])  # select cours scopé owner → vide
    payload = {
        "original_name": "x.pdf", "mime": "application/pdf", "size": 10, "type": "document",
    }
    response = _client(session).post(
        f"/api/v1/courses/{uuid.uuid4()}/resources", json=payload
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"
    assert _inserts(session, "resources") == []


# --- Liste (bibliothèque du cours) ---------------------------------------------


def test_list_course_resources():
    user = _user_row()
    course = _course_row()
    r1 = _resource_row(course_id=course.id, status="available")
    r2 = _resource_row(
        course_id=course.id, s3_key="uuid/photo.png", original_name="photo.png",
        type="image", mime="image/png",
    )
    # L'ordre servi (created_at desc, id) est restitué tel quel ; les
    # « pending » sont incluses (uploads à confirmer/purger).
    session = _FakeSession([[user], [course], [r1, r2]])
    response = _client(session).get(f"/api/v1/courses/{course.id}/resources")

    assert response.status_code == 200
    body = response.json()
    assert [r["id"] for r in body] == [str(r1.id), str(r2.id)]
    assert body[0] == {
        "id": str(r1.id),
        "type": "document",
        "original_name": "schema.pdf",
        "size": 1024,
        "mime": "application/pdf",
        "status": "available",
        "created_at": r1.created_at.isoformat().replace("+00:00", "Z"),
        "updated_at": r1.updated_at.isoformat().replace("+00:00", "Z"),
    }
    assert "s3_key" not in body[0]  # détail interne de stockage, jamais servi
    assert body[1]["status"] == "pending"
    # Lecture seule : seul commit, l'upsert auth.
    assert session.commits == 1


def test_list_resources_foreign_course_404():
    user = _user_row()
    session = _FakeSession([[user], []])
    response = _client(session).get(f"/api/v1/courses/{uuid.uuid4()}/resources")

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


# --- Confirmation d'upload ----------------------------------------------------


def test_confirm_ok_available_without_block():
    # La confirmation ne matérialise plus AUCUN bloc : la ressource rejoint la
    # bibliothèque, les blocs document la pointeront via PATCH resource_id.
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, size=2048)
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage(head_result={"ContentLength": 2048})
    response = _client(session, storage).post(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/confirm"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(resource.id)
    assert body["status"] == "available"
    assert body["original_name"] == "schema.pdf"

    assert storage.head_calls == [resource.s3_key]
    # Aucun insert métier (seul l'upsert auth sur users passe par un Insert).
    assert _inserts(session, "blocks") == []
    assert _inserts(session, "resources") == []
    # La ressource passe à available (mutation ORM, flush au commit).
    assert resource.status == "available"
    assert course.updated_at != _NOW
    assert session.commits >= 1


def test_confirm_missing_object_409():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id)
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage(head_result=None)  # objet jamais uploadé
    response = _client(session, storage).post(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/confirm"
    )

    assert response.status_code == 409
    assert resource.status == "pending"


def test_confirm_inconsistent_size_409():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, size=2048)
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage(head_result={"ContentLength": 999})
    response = _client(session, storage).post(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/confirm"
    )

    assert response.status_code == 409
    assert resource.status == "pending"


def test_confirm_already_confirmed_409():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, status="available")
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage(head_result={"ContentLength": 1024})
    response = _client(session, storage).post(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/confirm"
    )

    assert response.status_code == 409
    assert storage.head_calls == []  # court-circuit avant HEAD S3


def test_confirm_resource_not_found_404():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])  # ressource absente du cours
    response = _client(session).post(
        f"/api/v1/courses/{course.id}/resources/{uuid.uuid4()}/confirm"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Ressource introuvable"


def test_confirm_foreign_course_404():
    user = _user_row()
    session = _FakeSession([[user], []])
    response = _client(session).post(
        f"/api/v1/courses/{uuid.uuid4()}/resources/{uuid.uuid4()}/confirm"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


# --- Renommage (PATCH) ----------------------------------------------------------


def test_rename_ok_s3_key_frozen():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, status="available")
    session = _FakeSession([[user], [course], [resource]])
    response = _client(session).patch(
        f"/api/v1/courses/{course.id}/resources/{resource.id}",
        json={"original_name": "schéma final.pdf"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["original_name"] == "schéma final.pdf"
    # Mutation ORM, pas d'Update Core ; la clé S3 ne bouge JAMAIS au renommage.
    assert resource.original_name == "schéma final.pdf"
    assert resource.s3_key == "uuid/schema.pdf"
    assert not any(isinstance(stmt, Update) for stmt, _ in session.executed)
    assert course.updated_at != _NOW
    assert session.commits >= 1


@pytest.mark.parametrize(
    "payload",
    [
        {},  # original_name requis
        {"original_name": ""},
        {"original_name": "   "},  # blanc : rejeté après trim
        {"original_name": "x" * 256},
        {"original_name": "x.pdf", "type": "image"},  # clé en trop (extra=forbid)
    ],
)
def test_rename_invalid_payload_without_db_access(payload):
    session = _FakeSession()
    response = _client(session).patch(
        f"/api/v1/courses/{uuid.uuid4()}/resources/{uuid.uuid4()}", json=payload
    )
    assert response.status_code == 422
    assert session.executed == []


def test_rename_resource_other_course_404():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])  # select scopé course → vide
    response = _client(session).patch(
        f"/api/v1/courses/{course.id}/resources/{uuid.uuid4()}",
        json={"original_name": "y.pdf"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Ressource introuvable"


# --- Suppression -----------------------------------------------------------------


def test_delete_resource_purges_s3_after_commit():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, status="available")
    session = _FakeSession([[user], [course], [resource]])

    class _StorageAfterCommit(_FakeStorage):
        # La purge S3 doit intervenir APRÈS le commit (motif delete_course) :
        # jamais de réf DB pointant un objet absent.
        async def delete_many(self, s3_keys):
            assert session.commits >= 2  # upsert auth + delete ressource
            await super().delete_many(s3_keys)

    storage = _StorageAfterCommit()
    response = _client(session, storage).delete(
        f"/api/v1/courses/{course.id}/resources/{resource.id}"
    )

    assert response.status_code == 204
    deletes = [stmt for stmt, _ in session.executed if isinstance(stmt, Delete)]
    assert [d.table.name for d in deletes] == ["resources"]
    # Les blocs document pointeurs partent avec elle par la FK CASCADE :
    # aucun execute supplémentaire côté service.
    assert storage.deleted == [resource.s3_key]
    assert course.updated_at != _NOW


def test_delete_resource_not_found_404():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])
    storage = _FakeStorage()
    response = _client(session, storage).delete(
        f"/api/v1/courses/{course.id}/resources/{uuid.uuid4()}"
    )

    assert response.status_code == 404
    assert storage.deleted == []


def test_delete_resource_foreign_course_404():
    user = _user_row()
    session = _FakeSession([[user], []])
    storage = _FakeStorage()
    response = _client(session, storage).delete(
        f"/api/v1/courses/{uuid.uuid4()}/resources/{uuid.uuid4()}"
    )

    assert response.status_code == 404
    assert storage.deleted == []


# --- Lecture (presign GET) ----------------------------------------------------


def test_download_ok():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, status="available")
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage()
    response = _client(session, storage).get(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/download"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["download_url"] == f"https://s3.test/get/{resource.s3_key}"
    assert body["expires_in"] == settings.S3_PRESIGN_GET_TTL
    assert storage.get_calls == [(resource.s3_key, resource.original_name)]
    # Sans query param, la disposition reste attachment (téléchargement).
    assert storage.inline_calls == [False]


def test_download_inline_ok():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, status="available")
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage()
    response = _client(session, storage).get(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/download"
        "?disposition=inline"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["download_url"] == f"https://s3.test/get/{resource.s3_key}"
    # Disposition inline demandée à S3 (le navigateur affiche, pas de download).
    assert storage.get_calls == [(resource.s3_key, resource.original_name)]
    assert storage.inline_calls == [True]


def test_download_invalid_disposition_422():
    session = _FakeSession([])
    response = _client(session).get(
        f"/api/v1/courses/{uuid.uuid4()}/resources/{uuid.uuid4()}/download"
        "?disposition=autre"
    )

    assert response.status_code == 422
    assert session.executed == []  # validé avant tout accès BDD


def test_download_not_available_409():
    user = _user_row()
    course = _course_row()
    resource = _resource_row(course_id=course.id, status="pending")
    session = _FakeSession([[user], [course], [resource]])
    storage = _FakeStorage()
    response = _client(session, storage).get(
        f"/api/v1/courses/{course.id}/resources/{resource.id}/download"
    )

    assert response.status_code == 409
    assert storage.get_calls == []


def test_download_resource_not_found_404():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])
    response = _client(session).get(
        f"/api/v1/courses/{course.id}/resources/{uuid.uuid4()}/download"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Ressource introuvable"
