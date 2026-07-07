"""Route /subjects/tree et assemblage de l'arbre — aucun Postgres requis."""

import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.main import create_app
from app.subjects.service import build_tree


def _row(nom, profondeur, parent_id=None, position=0):
    return SimpleNamespace(
        id=uuid.uuid4(),
        parent_id=parent_id,
        nom=nom,
        code=nom.lower().replace(" ", "-"),
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
    response = client.get("/api/v1/subjects/tree")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_tree_nested_response():
    discipline = _row("Mathématiques", 0)
    domaine = _row("Algèbre", 1, parent_id=discipline.id)
    sujet = _row("Espaces vectoriels", 2, parent_id=domaine.id)
    client = _client_with_overrides([discipline, domaine, sujet])

    response = client.get("/api/v1/subjects/tree")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["nom"] == "Mathématiques"
    assert body[0]["children"][0]["nom"] == "Algèbre"
    assert body[0]["children"][0]["children"][0]["nom"] == "Espaces vectoriels"


def test_build_tree_vide():
    assert build_tree([]) == []


def test_build_tree_ordre_des_freres():
    racine = _row("Physique", 0)
    # Le service trie par (profondeur, position) : on fournit les lignes déjà triées
    premier = _row("Mécanique", 1, parent_id=racine.id, position=0)
    second = _row("Optique", 1, parent_id=racine.id, position=1)
    arbre = build_tree([racine, premier, second])
    assert [n.nom for n in arbre[0].children] == ["Mécanique", "Optique"]


def test_build_tree_orphelin_tolere():
    orphelin = _row("Sans parent", 1, parent_id=uuid.uuid4())
    arbre = build_tree([orphelin])
    assert len(arbre) == 1
    assert arbre[0].nom == "Sans parent"
