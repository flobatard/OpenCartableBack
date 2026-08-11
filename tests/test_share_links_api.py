"""Routes /courses/{id}/share-links — gestion prof des liens de partage (J2).

Même motif que tests/test_modules_api.py : fausse session FIFO (résultats
des SELECT servis dans l'ordre des ``execute`` du service, documenté dans
app/share_links/service.py). Le premier ``[user]`` de la file est consommé
par ``get_or_create_by_sub`` (upsert auth, 1 commit). Pur BDD, pas de storage.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete, Insert, Update

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.main import create_app

_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
_NOW_JSON = "2026-07-07T12:00:00Z"


def _user_row():
    return SimpleNamespace(id=uuid.uuid4(), sub="prof-123", email=None)


def _course_row(**overrides):
    defaults = dict(id=uuid.uuid4(), owner_id=None, updated_at=_NOW)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _link_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=None,
        token="tok-" + "a" * 39,
        libelle="6eB 2026",
        expires_at=_NOW + timedelta(days=270),
        revoked=False,
        created_at=_NOW,
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


_COURSE_ID = uuid.uuid4()
_LINK_ID = uuid.uuid4()


# --- Auth requise sur toutes les routes ---------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", f"/api/v1/courses/{_COURSE_ID}/share-links", None),
        ("POST", f"/api/v1/courses/{_COURSE_ID}/share-links", {}),
        ("DELETE", f"/api/v1/courses/{_COURSE_ID}/share-links/{_LINK_ID}", None),
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
        ("POST", "", {}),
        ("DELETE", f"/{_LINK_ID}", None),
    ],
)
def test_cours_autrui_introuvable(method, path_suffix, body):
    user = _user_row()
    session = _FakeSession([[user], []])
    response = _client(session).request(
        method, f"/api/v1/courses/{_COURSE_ID}/share-links{path_suffix}", json=body
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"
    assert session.commits == 1  # upsert auth seulement


# --- Création -----------------------------------------------------------------


def test_creation_lien_token_et_expiration():
    user = _user_row()
    course = _course_row()
    # FIFO : cours, puis created_at servi par l'insert RETURNING.
    session = _FakeSession([[user], [course], [_NOW]])
    avant = datetime.now(UTC)
    response = _client(session).post(
        f"/api/v1/courses/{course.id}/share-links", json={"libelle": "6eB 2026"}
    )
    apres = datetime.now(UTC)

    assert response.status_code == 201
    body = response.json()
    # token_urlsafe(32) = 43 caractères URL-safe (256 bits d'entropie).
    assert len(body["token"]) >= 43
    assert body["libelle"] == "6eB 2026"
    assert body["revoked"] is False
    assert body["created_at"] == _NOW_JSON
    expires_at = datetime.fromisoformat(body["expires_at"])
    ttl = timedelta(days=settings.SHARE_LINK_TTL_DAYS)
    assert avant + ttl <= expires_at <= apres + ttl

    # Le token inséré est bien celui renvoyé (capability URL recopiable).
    [(stmt, _)] = [
        (stmt, params)
        for stmt, params in session.executed
        if isinstance(stmt, Insert) and stmt.table.name == "share_links"
    ]
    valeurs = stmt.compile().params
    assert valeurs["token"] == body["token"]
    assert valeurs["revoked"] is False
    # Créer un lien ne bump pas updated_at (le contenu du cours ne change pas).
    assert course.updated_at == _NOW


def test_creation_lien_sans_libelle_et_libelle_blanc():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], [_NOW]])
    response = _client(session).post(
        f"/api/v1/courses/{course.id}/share-links", json={"libelle": "   "}
    )
    assert response.status_code == 201
    assert response.json()["libelle"] is None


def test_creation_deux_liens_tokens_distincts():
    user = _user_row()
    course = _course_row()
    session1 = _FakeSession([[user], [course], [_NOW]])
    session2 = _FakeSession([[_user_row()], [course], [_NOW]])
    t1 = (
        _client(session1)
        .post(f"/api/v1/courses/{course.id}/share-links", json={})
        .json()["token"]
    )
    t2 = (
        _client(session2)
        .post(f"/api/v1/courses/{course.id}/share-links", json={})
        .json()["token"]
    )
    assert t1 != t2


# --- Liste --------------------------------------------------------------------


def test_liste_liens_revoques_inclus():
    user = _user_row()
    course = _course_row()
    actif = _link_row(course_id=course.id)
    revoque = _link_row(course_id=course.id, libelle=None, revoked=True)
    # FIFO : cours (contrôle de propriété), puis liens (tri côté SQL).
    session = _FakeSession([[user], [course], [actif, revoque]])
    response = _client(session).get(f"/api/v1/courses/{course.id}/share-links")

    assert response.status_code == 200
    body = response.json()
    assert [link["id"] for link in body] == [str(actif.id), str(revoque.id)]
    assert body[0] == {
        "id": str(actif.id),
        "token": actif.token,
        "libelle": "6eB 2026",
        "expires_at": "2027-04-03T12:00:00Z",
        "revoked": False,
        "created_at": _NOW_JSON,
    }
    assert body[1]["revoked"] is True  # les révoqués restent listés (audit)
    assert session.commits == 1  # lecture seule


# --- Révocation ---------------------------------------------------------------


def test_revocation_soft():
    user = _user_row()
    course = _course_row()
    link = _link_row(course_id=course.id)
    # FIFO : cours, puis lien scopé cours.
    session = _FakeSession([[user], [course], [link]])
    response = _client(session).delete(
        f"/api/v1/courses/{course.id}/share-links/{link.id}"
    )

    assert response.status_code == 204
    # Soft delete : mutation d'attribut, aucun Delete Core.
    assert link.revoked is True
    assert not any(isinstance(stmt, Delete) for stmt, _ in session.executed)
    assert course.updated_at == _NOW  # pas de bump
    assert session.commits >= 2  # upsert auth + révocation


def test_revocation_idempotente():
    user = _user_row()
    course = _course_row()
    link = _link_row(course_id=course.id, revoked=True)
    session = _FakeSession([[user], [course], [link]])
    response = _client(session).delete(
        f"/api/v1/courses/{course.id}/share-links/{link.id}"
    )
    assert response.status_code == 204
    assert link.revoked is True


def test_revocation_lien_inconnu_ou_autre_cours():
    user = _user_row()
    course = _course_row()
    session = _FakeSession([[user], [course], []])
    response = _client(session).delete(
        f"/api/v1/courses/{course.id}/share-links/{uuid.uuid4()}"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Lien de partage introuvable"
