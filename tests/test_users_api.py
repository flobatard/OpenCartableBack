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
        est_prof=False,
        est_eleve=False,
        systeme_scolaire=None,
        nom_public=None,
        cherchable=False,
        avatar_s3_key=None,
        avatar_mime=None,
        avatar_statut=None,
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

    def presign_get(self, s3_key, nom_original, inline=False):
        self.get_calls.append((s3_key, nom_original))
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


def test_me_premiere_connexion_auto_provisionne():
    user = _user_row()
    # SELECTs : ligne user, associations niveaux (vides), matières (vides)
    session = _FakeSession([[user], [], []])
    response = _client(session).get("/api/v1/users/me")

    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "prof-123"
    assert body["onboarding_complete"] is False
    assert body["est_prof"] is False and body["est_eleve"] is False
    assert body["enseignement"] is None and body["apprentissage"] is None

    # Le premier statement est bien l'upsert ON CONFLICT sur users.
    stmt, _ = session.executed[0]
    assert isinstance(stmt, Insert) and stmt.table.name == "users"
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT" in sql
    assert session.commits >= 1


def test_me_rafraichit_email_depuis_le_claim():
    user = _user_row(email="ancien@example.org")
    session = _FakeSession([[user], [], []])
    response = _client(session, email="nouveau@example.org").get("/api/v1/users/me")
    assert response.status_code == 200
    assert user.email == "nouveau@example.org"
    assert response.json()["email"] == "nouveau@example.org"


def test_me_user_onboarde_double_role():
    user = _user_row(
        est_prof=True,
        est_eleve=True,
        systeme_scolaire="fr",
        onboarded_at=datetime.now(UTC),
    )
    niveau_enseigne, niveau_appris = uuid.uuid4(), uuid.uuid4()
    matiere_enseignee, matiere_apprise = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession(
        [
            [user],
            [(niveau_appris, "apprend"), (niveau_enseigne, "enseigne")],
            [(matiere_apprise, "apprend"), (matiere_enseignee, "enseigne")],
        ]
    )
    response = _client(session).get("/api/v1/users/me")

    assert response.status_code == 200
    body = response.json()
    assert body["onboarding_complete"] is True
    assert body["enseignement"]["education_level_ids"] == [str(niveau_enseigne)]
    assert body["enseignement"]["subject_ids"] == [str(matiere_enseignee)]
    assert body["apprentissage"]["education_level_ids"] == [str(niveau_appris)]
    assert body["apprentissage"]["subject_ids"] == [str(matiere_apprise)]


def _bloc(niveaux=None, matieres=None):
    return {
        "education_level_ids": [str(i) for i in (niveaux or [uuid.uuid4()])],
        "subject_ids": [str(i) for i in (matieres or [uuid.uuid4()])],
    }


@pytest.mark.parametrize(
    "payload",
    [
        # Aucun rôle coché
        {"est_prof": False, "est_eleve": False, "systeme_scolaire": "fr",
         "enseignement": None, "apprentissage": None},
        # Rôle coché sans son bloc
        {"est_prof": True, "est_eleve": False, "systeme_scolaire": "fr"},
        # Bloc fourni sans le rôle correspondant
        {"est_prof": True, "est_eleve": False, "systeme_scolaire": "fr",
         "enseignement": _bloc(), "apprentissage": _bloc()},
        # Listes vides
        {"est_prof": True, "est_eleve": False, "systeme_scolaire": "fr",
         "enseignement": {"education_level_ids": [], "subject_ids": []}},
    ],
)
def test_onboarding_payload_invalide_sans_acces_bdd(payload):
    session = _FakeSession()
    response = _client(session).put("/api/v1/users/me/profile", json=payload)
    assert response.status_code == 422
    assert session.executed == []


def test_onboarding_systeme_inconnu():
    user = _user_row()
    session = _FakeSession([[user], ["fr", "uk"]])
    payload = {"est_prof": True, "est_eleve": False, "systeme_scolaire": "xx",
               "enseignement": _bloc()}
    response = _client(session).put("/api/v1/users/me/profile", json=payload)
    assert response.status_code == 422
    assert "Système scolaire inconnu" in response.json()["detail"]


def test_onboarding_niveau_inconnu():
    user = _user_row()
    session = _FakeSession([[user], ["fr"], []])  # lookup niveaux vide
    payload = {"est_prof": True, "est_eleve": False, "systeme_scolaire": "fr",
               "enseignement": _bloc()}
    response = _client(session).put("/api/v1/users/me/profile", json=payload)
    assert response.status_code == 422
    assert "Niveaux d'étude inconnus" in response.json()["detail"]


def test_onboarding_niveau_hors_systeme():
    user = _user_row()
    niveau_uk = uuid.uuid4()
    session = _FakeSession([[user], ["fr", "uk"], [(niveau_uk, "uk")]])
    payload = {"est_prof": True, "est_eleve": False, "systeme_scolaire": "fr",
               "enseignement": _bloc(niveaux=[niveau_uk])}
    response = _client(session).put("/api/v1/users/me/profile", json=payload)
    assert response.status_code == 422
    assert "hors du système scolaire 'fr'" in response.json()["detail"]


def test_onboarding_matiere_inconnue():
    user = _user_row()
    niveau = uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(niveau, "fr")], []])  # lookup matières vide
    payload = {"est_prof": True, "est_eleve": False, "systeme_scolaire": "fr",
               "enseignement": _bloc(niveaux=[niveau])}
    response = _client(session).put("/api/v1/users/me/profile", json=payload)
    assert response.status_code == 422
    assert "Matières inconnues" in response.json()["detail"]


def test_onboarding_happy_path_double_role():
    user = _user_row()
    niveau_e, niveau_a = uuid.uuid4(), uuid.uuid4()
    matiere_e, matiere_a = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession(
        [
            [user],
            ["fr"],
            [(niveau_e, "fr"), (niveau_a, "fr")],
            [matiere_e, matiere_a],
        ]
    )
    payload = {
        "est_prof": True,
        "est_eleve": True,
        "systeme_scolaire": "fr",
        "enseignement": _bloc(niveaux=[niveau_e], matieres=[matiere_e]),
        "apprentissage": _bloc(niveaux=[niveau_a], matieres=[matiere_a]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["onboarding_complete"] is True
    assert body["est_prof"] is True and body["est_eleve"] is True
    assert body["systeme_scolaire"] == "fr"
    assert body["enseignement"]["education_level_ids"] == [str(niveau_e)]
    assert body["apprentissage"]["subject_ids"] == [str(matiere_a)]

    # L'état du user est mis à jour et daté.
    assert user.est_prof is True and user.est_eleve is True
    assert user.systeme_scolaire == "fr"
    assert user.onboarded_at is not None

    # Les associations sont remplacées (delete) puis écrites avec le contexte.
    assert sum(isinstance(stmt, Delete) for stmt, _ in session.executed) == 2
    [(_, params_niveaux)] = _inserts(session, "user_education_levels")
    assert {p["contexte"] for p in params_niveaux} == {"enseigne", "apprend"}
    [(_, params_matieres)] = _inserts(session, "user_subjects")
    assert {(p["subject_id"], p["contexte"]) for p in params_matieres} == {
        (matiere_e, "enseigne"),
        (matiere_a, "apprend"),
    }


def test_profil_nom_public_enregistre_et_expose():
    # Le nom public (J2) est la seule donnée d'identité montrée sur les
    # pages publiques ; un blanc devient None (catalogue anonyme).
    user = _user_row()
    niveau, matiere = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(niveau, "fr")], [matiere]])
    payload = {
        "est_prof": True,
        "est_eleve": False,
        "systeme_scolaire": "fr",
        "nom_public": "  M. Dupont  ",
        "enseignement": _bloc(niveaux=[niveau], matieres=[matiere]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["nom_public"] == "M. Dupont"  # trimé par le schéma
    assert user.nom_public == "M. Dupont"


def test_profil_nom_public_blanc_devient_none():
    user = _user_row(nom_public="Ancien nom")
    niveau, matiere = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(niveau, "fr")], [matiere]])
    payload = {
        "est_prof": True,
        "est_eleve": False,
        "systeme_scolaire": "fr",
        "nom_public": "   ",
        "enseignement": _bloc(niveaux=[niveau], matieres=[matiere]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["nom_public"] is None
    assert user.nom_public is None  # remplacement complet : l'ancien nom part


def test_profil_cherchable_enregistre_et_expose():
    # Opt-in à la recherche publique de profs (J3) : porté par le même PUT
    # de remplacement complet que le reste du profil.
    user = _user_row()
    niveau, matiere = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(niveau, "fr")], [matiere]])
    payload = {
        "est_prof": True,
        "est_eleve": False,
        "systeme_scolaire": "fr",
        "nom_public": "M. Dupont",
        "cherchable": True,
        "enseignement": _bloc(niveaux=[niveau], matieres=[matiere]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["cherchable"] is True
    assert user.cherchable is True


def test_profil_cherchable_absent_decoche():
    # PUT = remplacement complet : un payload sans le champ retombe sur False
    # (comportement sûr — on ne reste jamais cherchable par accident).
    user = _user_row(cherchable=True)
    niveau, matiere = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(niveau, "fr")], [matiere]])
    payload = {
        "est_prof": True,
        "est_eleve": False,
        "systeme_scolaire": "fr",
        "enseignement": _bloc(niveaux=[niveau], matieres=[matiere]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["cherchable"] is False
    assert user.cherchable is False


def test_profil_cherchable_sans_nom_public_accepte():
    # Toléré par le schéma : la règle de visibilité (cherchable AND
    # nom_public AND ≥1 cours public) vit dans le service de recherche.
    user = _user_row()
    niveau, matiere = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(niveau, "fr")], [matiere]])
    payload = {
        "est_prof": True,
        "est_eleve": False,
        "systeme_scolaire": "fr",
        "cherchable": True,
        "enseignement": _bloc(niveaux=[niveau], matieres=[matiere]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["cherchable"] is True
    assert response.json()["nom_public"] is None


def test_onboarding_dedoublonne_et_conserve_la_date():
    premiere_date = datetime(2026, 1, 1, tzinfo=UTC)
    user = _user_row(est_prof=True, systeme_scolaire="fr", onboarded_at=premiere_date)
    niveau, matiere = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(niveau, "fr")], [matiere]])
    payload = {
        "est_prof": True,
        "est_eleve": False,
        "systeme_scolaire": "fr",
        "enseignement": _bloc(niveaux=[niveau, niveau], matieres=[matiere, matiere]),
    }
    response = _client(session).put("/api/v1/users/me/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["enseignement"]["education_level_ids"] == [str(niveau)]
    [(_, params_niveaux)] = _inserts(session, "user_education_levels")
    assert len(params_niveaux) == 1
    # La date de première complétion n'est pas écrasée par la re-soumission.
    assert user.onboarded_at == premiere_date


# --- Avatar (photo de profil) -------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/users/me/avatar", {"mime": "image/jpeg", "taille": 10}),
        ("POST", "/api/v1/users/me/avatar/confirm", None),
        ("DELETE", "/api/v1/users/me/avatar", None),
    ],
)
def test_avatar_requires_auth(client: TestClient, method, path, body):
    response = client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_me_expose_avatar_url_si_disponible():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_statut="disponible",
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


def test_me_avatar_en_attente_url_nulle():
    # Un upload jamais confirmé ne sert rien : avatar_url reste None.
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_statut="en_attente",
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
        "/api/v1/users/me/avatar", json={"mime": "image/jpeg", "taille": 1024}
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
    assert user.avatar_statut == "en_attente"
    assert storage.deleted == []  # pas d'ancien avatar à purger
    assert session.commits >= 2  # get_or_create + presign


def test_avatar_presign_ecrase_et_purge_l_ancien():
    ancienne = "users/u/avatar/vieux/avatar.png"
    user = _user_row(
        avatar_s3_key=ancienne, avatar_mime="image/png", avatar_statut="disponible"
    )
    session = _FakeSession([[user]])
    storage = _FakeStorage()
    response = _client(session, storage=storage).post(
        "/api/v1/users/me/avatar", json={"mime": "image/webp", "taille": 2048}
    )

    assert response.status_code == 201
    # L'ancien objet est purgé (après commit) ; la nouvelle clé porte la
    # nouvelle extension et repart en_attente.
    assert storage.deleted == [ancienne]
    assert user.avatar_s3_key.endswith("/avatar.webp")
    assert user.avatar_statut == "en_attente"


def test_avatar_presign_mime_hors_whitelist_422():
    session = _FakeSession()
    response = _client(session).post(
        "/api/v1/users/me/avatar", json={"mime": "image/gif", "taille": 1024}
    )
    assert response.status_code == 422
    assert session.executed == []


def test_avatar_presign_taille_au_dessus_du_plafond_422():
    session = _FakeSession()
    response = _client(session).post(
        "/api/v1/users/me/avatar",
        json={"mime": "image/jpeg", "taille": 5_242_881},
    )
    assert response.status_code == 422
    assert session.executed == []


def test_avatar_confirm_happy_path():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_statut="en_attente",
    )
    # SELECTs : user, puis read_profile (niveaux, matières).
    session = _FakeSession([[user], [], []])
    storage = _FakeStorage(
        head_result={"ContentLength": 1024, "ContentType": "image/jpeg"}
    )
    response = _client(session, storage=storage).post("/api/v1/users/me/avatar/confirm")

    assert response.status_code == 200
    assert user.avatar_statut == "disponible"
    assert response.json()["avatar_url"] == (
        "https://s3.test/get/users/u/avatar/x/avatar.jpg"
    )
    assert storage.head_calls == ["users/u/avatar/x/avatar.jpg"]
    assert storage.deleted == []


def test_avatar_confirm_sans_upload_409():
    user = _user_row()  # aucun avatar déclaré
    session = _FakeSession([[user]])
    response = _client(session).post("/api/v1/users/me/avatar/confirm")
    assert response.status_code == 409
    assert "Aucun upload" in response.json()["detail"]


def test_avatar_confirm_deja_confirme_409():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_statut="disponible",
    )
    session = _FakeSession([[user]])
    response = _client(session).post("/api/v1/users/me/avatar/confirm")
    assert response.status_code == 409


def test_avatar_confirm_objet_absent_409():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_statut="en_attente",
    )
    session = _FakeSession([[user]])
    storage = _FakeStorage(head_result=None)
    response = _client(session, storage=storage).post("/api/v1/users/me/avatar/confirm")
    assert response.status_code == 409
    assert "upload non abouti" in response.json()["detail"]
    assert user.avatar_statut == "en_attente"


def test_avatar_confirm_hors_gabarit_409_et_purge():
    # Une URL présignée PUT ne borne pas la taille : l'objet hors plafond est
    # refusé ET purgé (best-effort) ; la ligne reste en_attente.
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_statut="en_attente",
    )
    session = _FakeSession([[user]])
    storage = _FakeStorage(
        head_result={"ContentLength": 6_000_000, "ContentType": "image/jpeg"}
    )
    response = _client(session, storage=storage).post("/api/v1/users/me/avatar/confirm")
    assert response.status_code == 409
    assert storage.deleted == ["users/u/avatar/x/avatar.jpg"]
    assert user.avatar_statut == "en_attente"


def test_avatar_confirm_mauvais_content_type_409():
    user = _user_row(
        avatar_s3_key="users/u/avatar/x/avatar.jpg",
        avatar_mime="image/jpeg",
        avatar_statut="en_attente",
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
        avatar_s3_key=s3_key, avatar_mime="image/jpeg", avatar_statut="disponible"
    )
    # SELECTs : user, puis read_profile (niveaux, matières).
    session = _FakeSession([[user], [], []])
    storage = _FakeStorage()
    response = _client(session, storage=storage).delete("/api/v1/users/me/avatar")

    assert response.status_code == 200
    assert response.json()["avatar_url"] is None
    assert user.avatar_s3_key is None
    assert user.avatar_mime is None
    assert user.avatar_statut is None
    assert storage.deleted == [s3_key]


def test_avatar_delete_sans_avatar_idempotent():
    user = _user_row()
    session = _FakeSession([[user], [], []])
    storage = _FakeStorage()
    response = _client(session, storage=storage).delete("/api/v1/users/me/avatar")
    assert response.status_code == 200
    assert response.json()["avatar_url"] is None
    assert storage.deleted == []  # rien à purger
