"""Routes /users/me et /users/me/onboarding — aucun Postgres requis.

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
from app.main import create_app


def _user_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        sub="prof-123",
        email=None,
        est_prof=False,
        est_eleve=False,
        systeme_scolaire=None,
        onboarded_at=None,
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


def _client(session, email=None) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="prof-123", email=email, roles=frozenset(), claims={}
    )
    app.dependency_overrides[get_db] = lambda: session
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
    response = client.put("/api/v1/users/me/onboarding", json={})
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
    response = _client(session).put("/api/v1/users/me/onboarding", json=payload)
    assert response.status_code == 422
    assert session.executed == []


def test_onboarding_systeme_inconnu():
    user = _user_row()
    session = _FakeSession([[user], ["fr", "uk"]])
    payload = {"est_prof": True, "est_eleve": False, "systeme_scolaire": "xx",
               "enseignement": _bloc()}
    response = _client(session).put("/api/v1/users/me/onboarding", json=payload)
    assert response.status_code == 422
    assert "Système scolaire inconnu" in response.json()["detail"]


def test_onboarding_niveau_inconnu():
    user = _user_row()
    session = _FakeSession([[user], ["fr"], []])  # lookup niveaux vide
    payload = {"est_prof": True, "est_eleve": False, "systeme_scolaire": "fr",
               "enseignement": _bloc()}
    response = _client(session).put("/api/v1/users/me/onboarding", json=payload)
    assert response.status_code == 422
    assert "Niveaux d'étude inconnus" in response.json()["detail"]


def test_onboarding_niveau_hors_systeme():
    user = _user_row()
    niveau_uk = uuid.uuid4()
    session = _FakeSession([[user], ["fr", "uk"], [(niveau_uk, "uk")]])
    payload = {"est_prof": True, "est_eleve": False, "systeme_scolaire": "fr",
               "enseignement": _bloc(niveaux=[niveau_uk])}
    response = _client(session).put("/api/v1/users/me/onboarding", json=payload)
    assert response.status_code == 422
    assert "hors du système scolaire 'fr'" in response.json()["detail"]


def test_onboarding_matiere_inconnue():
    user = _user_row()
    niveau = uuid.uuid4()
    session = _FakeSession([[user], ["fr"], [(niveau, "fr")], []])  # lookup matières vide
    payload = {"est_prof": True, "est_eleve": False, "systeme_scolaire": "fr",
               "enseignement": _bloc(niveaux=[niveau])}
    response = _client(session).put("/api/v1/users/me/onboarding", json=payload)
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
    response = _client(session).put("/api/v1/users/me/onboarding", json=payload)

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
    response = _client(session).put("/api/v1/users/me/onboarding", json=payload)

    assert response.status_code == 200
    assert response.json()["enseignement"]["education_level_ids"] == [str(niveau)]
    [(_, params_niveaux)] = _inserts(session, "user_education_levels")
    assert len(params_niveaux) == 1
    # La date de première complétion n'est pas écrasée par la re-soumission.
    assert user.onboarded_at == premiere_date
