"""Routes /users/me et /users/me/profile — aucun Postgres requis.

La fausse session sert les résultats des SELECT dans l'ordre des ``execute``
du service (FIFO, ordre documenté dans app/users/service.py) ; les
INSERT/DELETE sont tracés dans ``executed`` sans consommer la file.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Delete, Insert

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.storage import get_storage
from app.main import create_app


def _user_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        sub="prof-123",
        email=None,
        is_teacher=False,
        is_student=False,
        school_system=None,
        public_name=None,
        searchable=False,
        avatar_s3_key=None,
        avatar_mime=None,
        avatar_status=None,
        onboarded_at=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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


class _FakeSession:
    """FIFO des résultats de SELECT ; INSERT/DELETE tracés sans consommer."""

    def __init__(self, select_results=()):
        self._select_results = list(select_results)
        self.executed = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        if isinstance(stmt, (Insert, Delete)):
            return _FakeResult([])
        return _FakeResult(self._select_results.pop(0))

    async def commit(self):
        self.commits += 1


def _client(session, email=None, storage=None) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="prof-123", email=email, roles=frozenset(), claims={}
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


def test_me_requires_auth(client: TestClient):
    response = client.get("/api/v1/users/me")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_onboarding_requires_auth(client: TestClient):
    response = client.put("/api/v1/users/me/profile", json={})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_me_first_login_auto_provisions():
    user = _user_row()
    # SELECTs : ligne user, associations niveaux (vides), matières (vides)
    session = _FakeSession([[user], [], []])
    response = _client(session).get("/api/v1/users/me")

    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "prof-123"
    assert body["onboarding_complete"] is False
    assert body["is_teacher"] is False and body["is_student"] is False
    assert body["teaching"] is None and body["learning"] is None

    # Le premier statement est bien l'upsert ON CONFLICT sur users.
    stmt, _ = session.executed[0]
    assert isinstance(stmt, Insert) and stmt.table.name == "users"
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql
    assert session.commits >= 1


def test_me_refreshes_email_from_claim():
    user = _user_row(email="ancien@example.org")
    session = _FakeSession([[user], [], []])
    response = _client(session, email="nouveau@example.org").get("/api/v1/users/me")
    assert response.status_code == 200
    assert user.email == "nouveau@example.org"
    assert response.json()["email"] == "nouveau@example.org"


def test_me_onboarded_user_dual_role():
    user = _user_row(
        is_teacher=True,
        is_student=True,
        school_system="fr",
        onboarded_at=datetime.now(UTC),
    )
    taught_level, learned_level = uuid.uuid4(), uuid.uuid4()
    taught_subject, learned_subject = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession(
        [
            [user],
            [(learned_level, "learning"), (taught_level, "teaching")],
            [(learned_subject, "learning"), (taught_subject, "teaching")],
        ]
    )
    response = _client(session).get("/api/v1/users/me")

    assert response.status_code == 200
    body = response.json()
    assert body["onboarding_complete"] is True
    assert body["teaching"]["education_level_ids"] == [str(taught_level)]
    assert body["teaching"]["subject_ids"] == [str(taught_subject)]
    assert body["learning"]["education_level_ids"] == [str(learned_level)]
    assert body["learning"]["subject_ids"] == [str(learned_subject)]


def _block(levels=None, subjects=None):
    return {
        "education_level_ids": [str(i) for i in (levels or [uuid.uuid4()])],
        "subject_ids": [str(i) for i in (subjects or [uuid.uuid4()])],
    }


@pytest.mark.parametrize(
    "payload",
    [
        # Aucun rôle coché
        {"is_teacher": False, "is_student": False, "school_system": "fr",
         "teaching": None, "learning": None},
        # Rôle coché sans son bloc
        {"is_teacher": True, "is_student": False, "school_system": "fr"},
        # Bloc fourni sans le rôle correspondant
        {"is_teacher": True, "is_student": False, "school_system": "fr",
         "teaching": _block(), "learning": _block()},
        # Listes vides
        {"is_teacher": True, "is_student": False, "school_system": "fr",
         "teaching": {"education_level_ids": [], "subject_ids": []}},
    ],
)
def test_onboarding_invalid_payload_without_db_access(payload):
    session = _FakeSession()
    response = _client(session).put("/api/v1/users/me/profile", json=payload)
    assert response.status_code == 422
    assert session.executed == []


def test_onboarding_unknown_system():
    user = _user_row()
    session = _FakeSession([[user], ["fr", "uk"]])
    payload = {"is_teacher": True, "is_student": False, "school_system": "xx",
               "teaching": _block()}
    response = _client(session).put("/api/v1/users/me/profile", json=payload)
    assert response.status_code == 422
    assert "Système scolaire inconnu" in response.json()["detail"]


def test_onboarding_unknown_level():
    user = _user_row()
    session = _FakeSession([[user], ["fr"], []])  # lookup niveaux vide
    payload = {"is_teacher": True, "is_student": False, "school_system": "fr",
               "teaching": _block()}
    response = _client(session).put("/api/v1/users/me/profile", json=payload)
    assert response.status_code == 422
    assert "Niveaux d'étude inconnus" in response.json()["detail"]


def test_onboarding_level_outside_system():
    user = _user_row()
    uk_level = uuid.uuid4()
    session = _FakeSession([[user], ["fr", "uk"], [(uk_level, "uk")]])
    payload = {"is_teacher": True, "is_student": False, "school_system": "fr",
               "teaching": _block(levels=[uk_level])}
    response = _client(session).put("/api/v1/users/me/profile", json=payload)
    assert response.status_code == 422
    assert "hors du système scolaire 'fr'" in response.json()["detail"]


def test_onboarding_unknown_subject():
    user = _user_row()
    level = uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(level, "fr")], []])  # lookup matières vide
    payload = {"is_teacher": True, "is_student": False, "school_system": "fr",
               "teaching": _block(levels=[level])}
    response = _client(session).put("/api/v1/users/me/profile", json=payload)
    assert response.status_code == 422
    assert "Matières inconnues" in response.json()["detail"]


def test_onboarding_happy_path_dual_role():
    user = _user_row()
    taught_level, learned_level = uuid.uuid4(), uuid.uuid4()
    taught_subject, learned_subject = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession(
        [
            [user],
            ["fr"],
            [(taught_level, "fr"), (learned_level, "fr")],
            [taught_subject, learned_subject],
        ]
    )
    payload = {
        "is_teacher": True,
        "is_student": True,
        "school_system": "fr",
        "teaching": _block(levels=[taught_level], subjects=[taught_subject]),
        "learning": _block(levels=[learned_level], subjects=[learned_subject]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["onboarding_complete"] is True
    assert body["is_teacher"] is True and body["is_student"] is True
    assert body["school_system"] == "fr"
    assert body["teaching"]["education_level_ids"] == [str(taught_level)]
    assert body["learning"]["subject_ids"] == [str(learned_subject)]

    # L'état du user est mis à jour et daté.
    assert user.is_teacher is True and user.is_student is True
    assert user.school_system == "fr"
    assert user.onboarded_at is not None

    # Les associations sont remplacées (delete) puis écrites avec le contexte.
    assert sum(isinstance(stmt, Delete) for stmt, _ in session.executed) == 2
    [(_, level_params)] = _inserts(session, "user_education_levels")
    assert {p["context"] for p in level_params} == {"teaching", "learning"}
    [(_, subject_params)] = _inserts(session, "user_subjects")
    assert {(p["subject_id"], p["context"]) for p in subject_params} == {
        (taught_subject, "teaching"),
        (learned_subject, "learning"),
    }


def test_profile_public_name_saved_and_exposed():
    # Le nom public (J2) est la seule donnée d'identité montrée sur les
    # pages publiques ; un blanc devient None (catalogue anonyme).
    user = _user_row()
    level, subject = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(level, "fr")], [subject]])
    payload = {
        "is_teacher": True,
        "is_student": False,
        "school_system": "fr",
        "public_name": "  M. Dupont  ",
        "teaching": _block(levels=[level], subjects=[subject]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["public_name"] == "M. Dupont"  # trimé par le schéma
    assert user.public_name == "M. Dupont"


def test_profile_blank_public_name_becomes_none():
    user = _user_row(public_name="Ancien nom")
    level, subject = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(level, "fr")], [subject]])
    payload = {
        "is_teacher": True,
        "is_student": False,
        "school_system": "fr",
        "public_name": "   ",
        "teaching": _block(levels=[level], subjects=[subject]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["public_name"] is None
    assert user.public_name is None  # remplacement complet : l'ancien nom part


def test_profile_searchable_saved_and_exposed():
    # Opt-in à la recherche publique de profs (J3) : porté par le même PUT
    # de remplacement complet que le reste du profil.
    user = _user_row()
    level, subject = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(level, "fr")], [subject]])
    payload = {
        "is_teacher": True,
        "is_student": False,
        "school_system": "fr",
        "public_name": "M. Dupont",
        "searchable": True,
        "teaching": _block(levels=[level], subjects=[subject]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["searchable"] is True
    assert user.searchable is True


def test_profile_searchable_absent_unchecks():
    # PUT = remplacement complet : un payload sans le champ retombe sur False
    # (comportement sûr — on ne reste jamais cherchable par accident).
    user = _user_row(searchable=True)
    level, subject = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(level, "fr")], [subject]])
    payload = {
        "is_teacher": True,
        "is_student": False,
        "school_system": "fr",
        "teaching": _block(levels=[level], subjects=[subject]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["searchable"] is False
    assert user.searchable is False


def test_profile_searchable_without_public_name_accepted():
    # Toléré par le schéma : la règle de visibilité (searchable AND
    # public_name AND ≥1 cours public) vit dans le service de recherche.
    user = _user_row()
    level, subject = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(level, "fr")], [subject]])
    payload = {
        "is_teacher": True,
        "is_student": False,
        "school_system": "fr",
        "searchable": True,
        "teaching": _block(levels=[level], subjects=[subject]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["searchable"] is True
    assert response.json()["public_name"] is None


def test_onboarding_deduplicates_and_keeps_date():
    first_date = datetime(2026, 1, 1, tzinfo=UTC)
    user = _user_row(is_teacher=True, school_system="fr", onboarded_at=first_date)
    level, subject = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(level, "fr")], [subject]])
    payload = {
        "is_teacher": True,
        "is_student": False,
        "school_system": "fr",
        "teaching": _block(levels=[level, level], subjects=[subject, subject]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["teaching"]["education_level_ids"] == [str(level)]
    [(_, level_params)] = _inserts(session, "user_education_levels")
    assert len(level_params) == 1
    # La date de première complétion n'est pas écrasée par la re-soumission.
    assert user.onboarded_at == first_date


# --- Avatar (photo de profil) -------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/users/me/avatar", {"mime": "image/jpeg", "size": 10}),
        ("POST", "/api/v1/users/me/avatar/confirm", None),
        ("DELETE", "/api/v1/users/me/avatar", None),
    ],
)
def test_avatar_requires_auth(client: TestClient, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_me_exposes_avatar_url_when_available():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_status="available",
    )
    session = _FakeSession([[user], [], []])
    storage = _FakeStorage()
    response = _client(session, storage=storage).get("/api/v1/users/me")

    assert response.status_code == 200
    assert response.json()["avatar_url"] == (
        "https://s3.test/get/users/u/avatar/x/avatar.jpg"
    )
    # Présignée en inline, sous le nom constant de la clé (jamais la clé brute).
    assert storage.inline_calls == [True]
    assert storage.get_calls == [("users/u/avatar/x/avatar.jpg", "avatar.jpg")]


def test_me_pending_avatar_null_url():
    # Un upload jamais confirmé ne sert rien : avatar_url reste None.
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_status="pending",
    )
    session = _FakeSession([[user], [], []])
    response = _client(session).get("/api/v1/users/me")
    assert response.status_code == 200
    assert response.json()["avatar_url"] is None


def test_avatar_presign_happy_path():
    user = _user_row()
    session = _FakeSession([[user]])
    storage = _FakeStorage()
    response = _client(session, storage=storage).post(
        "/api/v1/users/me/avatar", json={"mime": "image/jpeg", "size": 1024}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["upload_url"].startswith("https://s3.test/put/users/")
    assert body["expires_in"] > 0
    # La clé suit le gabarit users/<id>/avatar/<uuid>/avatar.<ext> et la ligne
    # attend la confirmation.
    [(s3_key, content_type)] = storage.put_calls
    prefix, suffix = f"users/{user.id}/avatar/", "/avatar.jpg"
    assert s3_key.startswith(prefix) and s3_key.endswith(suffix)
    uuid.UUID(s3_key[len(prefix) : -len(suffix)])  # segment central = uuid valide
    assert content_type == "image/jpeg"
    assert user.avatar_s3_key == s3_key
    assert user.avatar_mime == "image/jpeg"
    assert user.avatar_status == "pending"
    assert storage.deleted == []  # pas d'ancien avatar à purger
    assert session.commits >= 2  # get_or_create + presign


def test_avatar_presign_overwrites_and_purges_old():
    old_key = "users/u/avatar/vieux/avatar.png"
    user = _user_row(
        avatar_s3_key=old_key, avatar_mime="image/png", avatar_status="available"
    )
    session = _FakeSession([[user]])
    storage = _FakeStorage()
    response = _client(session, storage=storage).post(
        "/api/v1/users/me/avatar", json={"mime": "image/webp", "size": 2048}
    )

    assert response.status_code == 201
    # L'ancien objet est purgé (après commit) ; la nouvelle clé porte la
    # nouvelle extension et repart en statut pending.
    assert storage.deleted == [old_key]
    assert user.avatar_s3_key.endswith("/avatar.webp")
    assert user.avatar_status == "pending"


def test_avatar_presign_mime_outside_whitelist_422():
    session = _FakeSession()
    response = _client(session).post(
        "/api/v1/users/me/avatar", json={"mime": "image/gif", "size": 1024}
    )
    assert response.status_code == 422
    assert session.executed == []


def test_avatar_presign_size_above_cap_422():
    session = _FakeSession()
    response = _client(session).post(
        "/api/v1/users/me/avatar",
        json={"mime": "image/jpeg", "size": 5_242_881},
    )
    assert response.status_code == 422
    assert session.executed == []


def test_avatar_confirm_happy_path():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_status="pending",
    )
    # SELECTs : user, puis read_profile (niveaux, matières).
    session = _FakeSession([[user], [], []])
    storage = _FakeStorage(
        head_result={"ContentLength": 1024, "ContentType": "image/jpeg"}
    )
    response = _client(session, storage=storage).post("/api/v1/users/me/avatar/confirm")

    assert response.status_code == 200
    assert user.avatar_status == "available"
    assert response.json()["avatar_url"] == (
        "https://s3.test/get/users/u/avatar/x/avatar.jpg"
    )
    assert storage.head_calls == ["users/u/avatar/x/avatar.jpg"]
    assert storage.deleted == []


def test_avatar_confirm_without_upload_409():
    user = _user_row()  # aucun avatar déclaré
    session = _FakeSession([[user]])
    response = _client(session).post("/api/v1/users/me/avatar/confirm")
    assert response.status_code == 409
    assert "Aucun upload" in response.json()["detail"]


def test_avatar_confirm_already_confirmed_409():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_status="available",
    )
    session = _FakeSession([[user]])
    response = _client(session).post("/api/v1/users/me/avatar/confirm")
    assert response.status_code == 409


def test_avatar_confirm_missing_object_409():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_status="pending",
    )
    session = _FakeSession([[user]])
    storage = _FakeStorage(head_result=None)
    response = _client(session, storage=storage).post("/api/v1/users/me/avatar/confirm")
    assert response.status_code == 409
    assert "upload non abouti" in response.json()["detail"]
    assert user.avatar_status == "pending"


def test_avatar_confirm_out_of_spec_409_and_purges():
    # Une URL présignée PUT ne borne pas la taille : l'objet hors plafond est
    # refusé ET purgé (best-effort) ; la ligne reste en statut pending.
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_status="pending",
    )
    session = _FakeSession([[user]])
    storage = _FakeStorage(
        head_result={"ContentLength": 6_000_000, "ContentType": "image/jpeg"}
    )
    response = _client(session, storage=storage).post("/api/v1/users/me/avatar/confirm")
    assert response.status_code == 409
    assert storage.deleted == ["users/u/avatar/x/avatar.jpg"]
    assert user.avatar_status == "pending"


def test_avatar_confirm_wrong_content_type_409():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_status="pending",
    )
    session = _FakeSession([[user]])
    storage = _FakeStorage(
        head_result={"ContentLength": 1024, "ContentType": "text/html"}
    )
    response = _client(session, storage=storage).post("/api/v1/users/me/avatar/confirm")
    assert response.status_code == 409
    assert storage.deleted == ["users/u/avatar/x/avatar.jpg"]


def test_avatar_delete():
    s3_key = "users/u/avatar/x/avatar.jpg"
    user = _user_row(
        avatar_s3_key=s3_key, avatar_mime="image/jpeg", avatar_status="available"
    )
    # SELECTs : user, puis read_profile (niveaux, matières).
    session = _FakeSession([[user], [], []])
    storage = _FakeStorage()
    response = _client(session, storage=storage).delete("/api/v1/users/me/avatar")

    assert response.status_code == 200
    assert response.json()["avatar_url"] is None
    assert user.avatar_s3_key is None
    assert user.avatar_mime is None
    assert user.avatar_status is None
    assert storage.deleted == [s3_key]


def test_avatar_delete_without_avatar_idempotent():
    user = _user_row()
    session = _FakeSession([[user], [], []])
    storage = _FakeStorage()
    response = _client(session, storage=storage).delete("/api/v1/users/me/avatar")
    assert response.status_code == 200
    assert response.json()["avatar_url"] is None
    assert storage.deleted == []  # rien à purger
