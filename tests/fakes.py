"""Fakes partagés par les tests d'API — aucun réseau, ni Postgres, ni S3.

Pas un module de tests (pas de préfixe ``test_``). La fausse session est une
**FIFO des résultats de SELECT** : l'ordre des ``execute`` de chaque fonction
de service est un contrat, documenté dans sa docstring — un test qui casse
après un réordonnancement des requêtes signale une rupture de ce contrat.
"""

import json
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete, Insert, Update

from app.core.ai import get_ai_client
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.storage import get_storage
from app.main import create_app


class FakeResult:
    """Résultat d'un ``execute`` : lignes servies telles quelles."""

    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def one(self):
        [row] = self._rows
        return row

    def one_or_none(self):
        if not self._rows:
            return None
        [row] = self._rows
        return row

    def scalar_one(self):
        [row] = self._rows
        return row


class FakeSession:
    """FIFO des SELECT ; les écritures sont tracées sans consommer la file.

    Exception : un ``Insert`` porteur de RETURNING consomme la file (il sert
    les timestamps relus). ``upsert_rowcount`` scripte le ``rowcount`` des
    écritures — seul le quota quotidien de l'IA le lit (1 = ligne écrite,
    0 = garde du ``DO UPDATE`` non satisfaite).
    """

    def __init__(self, select_results=(), upsert_rowcount=1):
        self._select_results = list(select_results)
        self.upsert_rowcount = upsert_rowcount
        self.executed: list[tuple[Any, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        if isinstance(stmt, Insert):
            if stmt._returning:
                return FakeResult(self._select_results.pop(0))
            return FakeResult([], rowcount=self.upsert_rowcount)
        if isinstance(stmt, (Delete, Update)):
            return FakeResult([], rowcount=self.upsert_rowcount)
        return FakeResult(self._select_results.pop(0))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakeStorage:
    """Faux client S3 : URLs déterministes, HEAD scriptable, appels tracés."""

    def __init__(self, head_result=None):
        self._head_result = head_result
        self.put_calls: list[tuple[str, str]] = []
        self.get_calls: list[tuple[str, str]] = []
        self.inline_calls: list = []
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


def make_client(
    session, storage=None, *, ai_client=None, authenticated=True, email=None
) -> TestClient:
    """TestClient sur ``create_app()`` avec la session, le storage et
    (optionnellement) l'auth et le client IA remplacés.

    ``authenticated=False`` laisse ``get_current_user`` en place : c'est le
    cas des routes publiques, qui ne le portent pas.
    """
    app = create_app()
    if authenticated:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            sub="prof-123", email=email, roles=frozenset(), claims={}
        )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_storage] = lambda: storage or FakeStorage()
    if ai_client is not None:
        app.dependency_overrides[get_ai_client] = lambda: ai_client
    return TestClient(app)


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Le corps d'un flux SSE en couples ``(event, data)``, data désérialisée."""
    events = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def inserts(session, table_name):
    """Les ``(stmt, params)`` des INSERT tracés sur une table."""
    return [
        (stmt, params)
        for stmt, params in session.executed
        if isinstance(stmt, Insert) and stmt.table.name == table_name
    ]


def updates(session):
    return [(stmt, params) for stmt, params in session.executed if isinstance(stmt, Update)]


def deletes(session):
    return [(stmt, params) for stmt, params in session.executed if isinstance(stmt, Delete)]
