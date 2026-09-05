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
from sqlalchemy.sql.dml import Delete, Insert

from app.core.config import settings
from app.main import create_app
from tests.fakes import FakeSession, make_client

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
        label="6eB 2026",
        expires_at=_NOW + timedelta(days=270),
        revoked=False,
        created_at=_NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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
def test_auth_required(method, path, body):
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
def test_other_users_course_not_found(method, path_suffix, body):
    user = _user_row()
    session = FakeSession([[user], []])
    response = make_client(session).request(
        method, f"/api/v1/courses/{_COURSE_ID}/share-links{path_suffix}", json=body
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"
    assert session.commits == 1  # upsert auth seulement


# --- Création -----------------------------------------------------------------


def test_link_creation_token_and_expiration():
    user = _user_row()
    course = _course_row()
    # FIFO : cours, puis created_at servi par l'insert RETURNING.
    session = FakeSession([[user], [course], [_NOW]])
    before = datetime.now(UTC)
    response = make_client(session).post(
        f"/api/v1/courses/{course.id}/share-links", json={"label": "6eB 2026"}
    )
    after = datetime.now(UTC)

    assert response.status_code == 201
    body = response.json()
    # token_urlsafe(32) = 43 caractères URL-safe (256 bits d'entropie).
    assert len(body["token"]) >= 43
    assert body["label"] == "6eB 2026"
    assert body["revoked"] is False
    assert body["created_at"] == _NOW_JSON
    expires_at = datetime.fromisoformat(body["expires_at"])
    ttl = timedelta(days=settings.SHARE_LINK_TTL_DAYS)
    assert before + ttl <= expires_at <= after + ttl

    # Le token inséré est bien celui renvoyé (capability URL recopiable).
    [(stmt, _)] = [
        (stmt, params)
        for stmt, params in session.executed
        if isinstance(stmt, Insert) and stmt.table.name == "share_links"
    ]
    values = stmt.compile().params
    assert values["token"] == body["token"]
    assert values["revoked"] is False
    # Créer un lien ne bump pas updated_at (le contenu du cours ne change pas).
    assert course.updated_at == _NOW


def test_link_creation_without_label_and_blank_label():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], [_NOW]])
    response = make_client(session).post(
        f"/api/v1/courses/{course.id}/share-links", json={"label": "   "}
    )
    assert response.status_code == 201
    assert response.json()["label"] is None


def test_two_link_creations_distinct_tokens():
    user = _user_row()
    course = _course_row()
    session1 = FakeSession([[user], [course], [_NOW]])
    session2 = FakeSession([[_user_row()], [course], [_NOW]])
    t1 = (
        make_client(session1)
        .post(f"/api/v1/courses/{course.id}/share-links", json={})
        .json()["token"]
    )
    t2 = (
        make_client(session2)
        .post(f"/api/v1/courses/{course.id}/share-links", json={})
        .json()["token"]
    )
    assert t1 != t2


# --- Liste --------------------------------------------------------------------


def test_list_includes_revoked_links():
    user = _user_row()
    course = _course_row()
    active = _link_row(course_id=course.id)
    revoked_link = _link_row(course_id=course.id, label=None, revoked=True)
    # FIFO : cours (contrôle de propriété), puis liens (tri côté SQL).
    session = FakeSession([[user], [course], [active, revoked_link]])
    response = make_client(session).get(f"/api/v1/courses/{course.id}/share-links")

    assert response.status_code == 200
    body = response.json()
    assert [link["id"] for link in body] == [str(active.id), str(revoked_link.id)]
    assert body[0] == {
        "id": str(active.id),
        "token": active.token,
        "label": "6eB 2026",
        "expires_at": "2027-04-03T12:00:00Z",
        "revoked": False,
        "created_at": _NOW_JSON,
    }
    assert body[1]["revoked"] is True  # les révoqués restent listés (audit)
    assert session.commits == 1  # lecture seule


# --- Révocation ---------------------------------------------------------------


def test_soft_revocation():
    user = _user_row()
    course = _course_row()
    link = _link_row(course_id=course.id)
    # FIFO : cours, puis lien scopé cours.
    session = FakeSession([[user], [course], [link]])
    response = make_client(session).delete(
        f"/api/v1/courses/{course.id}/share-links/{link.id}"
    )

    assert response.status_code == 204
    # Soft delete : mutation d'attribut, aucun Delete Core.
    assert link.revoked is True
    assert not any(isinstance(stmt, Delete) for stmt, _ in session.executed)
    assert course.updated_at == _NOW  # pas de bump
    assert session.commits >= 2  # upsert auth + révocation


def test_revocation_idempotent():
    user = _user_row()
    course = _course_row()
    link = _link_row(course_id=course.id, revoked=True)
    session = FakeSession([[user], [course], [link]])
    response = make_client(session).delete(
        f"/api/v1/courses/{course.id}/share-links/{link.id}"
    )
    assert response.status_code == 204
    assert link.revoked is True


def test_revocation_unknown_link_or_other_course():
    user = _user_row()
    course = _course_row()
    session = FakeSession([[user], [course], []])
    response = make_client(session).delete(
        f"/api/v1/courses/{course.id}/share-links/{uuid.uuid4()}"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Lien de partage introuvable"
