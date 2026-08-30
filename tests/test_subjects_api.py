"""Route /subjects/tree et assemblage de l'arbre — aucun Postgres requis."""

import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.main import create_app
from app.subjects.service import build_tree


def _row(name, depth, parent_id=None, position=0):
    return SimpleNamespace(
        id=uuid.uuid4(),
        parent_id=parent_id,
        name=name,
        code=name.lower().replace(" ", "-"),
        depth=depth,
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
    domain = _row("Algèbre", 1, parent_id=discipline.id)
    topic = _row("Espaces vectoriels", 2, parent_id=domain.id)
    client = _client_with_overrides([discipline, domain, topic])

    response = client.get("/api/v1/subjects/tree")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "Mathématiques"
    assert body[0]["children"][0]["name"] == "Algèbre"
    assert body[0]["children"][0]["children"][0]["name"] == "Espaces vectoriels"


def test_build_tree_empty():
    assert build_tree([]) == []


def test_build_tree_sibling_order():
    root = _row("Physique", 0)
    # Le service trie par (depth, position) : on fournit les lignes déjà triées
    first = _row("Mécanique", 1, parent_id=root.id, position=0)
    second = _row("Optique", 1, parent_id=root.id, position=1)
    tree = build_tree([root, first, second])
    assert [n.name for n in tree[0].children] == ["Mécanique", "Optique"]


def test_build_tree_orphan_tolerated():
    orphan = _row("Sans parent", 1, parent_id=uuid.uuid4())
    tree = build_tree([orphan])
    assert len(tree) == 1
    assert tree[0].name == "Sans parent"
