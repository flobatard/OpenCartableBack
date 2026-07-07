"""Route /education-levels/tree et assemblage de l'arbre — aucun Postgres requis."""

import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.education_levels.service import build_tree
from app.main import create_app


def _row(nom, profondeur, parent_id=None, position=0, cite=None, age_min=None, age_max=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        parent_id=parent_id,
        nom=nom,
        code=f"fr.{nom.lower().replace(' ', '-')}",
        systeme="fr",
        cite=cite,
        age_min=age_min,
        age_max=age_max,
        profondeur=profondeur,
        position=position,
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        return _FakeResult(self._rows)


def _client_with_overrides(rows) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="prof-123", email=None, roles=frozenset(), claims={}
    )
    app.dependency_overrides[get_db] = lambda: _FakeSession(rows)
    return TestClient(app)


def test_tree_requires_auth(client: TestClient):
    response = client.get("/api/v1/education-levels/tree")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_tree_nested_response_avec_pivots_internationaux():
    college = _row("Collège", 0, cite=2, age_min=11, age_max=15)
    sixieme = _row("6e", 1, parent_id=college.id, cite=2, age_min=11, age_max=12)
    superieur = _row("Supérieur", 0, position=1, age_min=18)  # cite/age_max None
    client = _client_with_overrides([college, superieur, sixieme])

    response = client.get("/api/v1/education-levels/tree")
    assert response.status_code == 200
    body = response.json()
    assert [n["nom"] for n in body] == ["Collège", "Supérieur"]
    assert body[0]["cite"] == 2
    assert body[0]["systeme"] == "fr"
    assert body[0]["children"][0]["nom"] == "6e"
    assert body[0]["children"][0]["age_min"] == 11
    assert body[0]["children"][0]["age_max"] == 12
    # Les pivots absents sortent en null explicite (contrat front).
    assert body[1]["cite"] is None
    assert body[1]["age_max"] is None


def test_build_tree_vide():
    assert build_tree([]) == []


def test_build_tree_ordre_des_freres():
    racine = _row("Lycée", 0, cite=3)
    # Le service trie par (profondeur, position) : on fournit les lignes déjà triées
    premiere = _row("Seconde", 1, parent_id=racine.id, position=0, cite=3)
    seconde = _row("Terminale", 1, parent_id=racine.id, position=1, cite=3)
    arbre = build_tree([racine, premiere, seconde])
    assert [n.nom for n in arbre[0].children] == ["Seconde", "Terminale"]


def test_build_tree_orphelin_tolere():
    orphelin = _row("Sans parent", 1, parent_id=uuid.uuid4())
    arbre = build_tree([orphelin])
    assert len(arbre) == 1
    assert arbre[0].nom == "Sans parent"
