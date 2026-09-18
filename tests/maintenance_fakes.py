"""Fakes propres aux tests de la maintenance (:mod:`app.maintenance`).

Pas un module de tests (pas de préfixe ``test_``). Ces fakes diffèrent de ceux
de :mod:`tests.fakes` sur deux points qui comptent ici :

- la session est **FIFO pure** — chaque ``execute`` consomme la prochaine
  réponse prévue — et sait échouer au n-ième appel (``fail_on``), ce qui permet
  de vérifier qu'un job qui tombe est bien isolé ;
- session et stockage partagent une liste d'``events`` : c'est ce qui prouve que
  la purge du bucket a lieu **après** le commit, et non l'inverse.

Le faux S3 mesure aussi sa concurrence maximale, pour le contrôle des objets
manquants — dont le plafond de requêtes en vol est une garantie, pas un réglage
de confort.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import ProgrammingError

from app.core.storage import S3Object


class FakeResult:
    """Résultat scripté : lignes, ``rowcount``, et vues ``scalars``/``mappings``."""

    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalars(self):
        return self

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def one(self):
        return self._rows[0]

    def __iter__(self):
        return iter(self._rows)


class FakeSession:
    """Session FIFO : chaque ``execute`` consomme la prochaine réponse prévue."""

    def __init__(self, results=None, events=None, fail_on=None):
        self._results = list(results or [])
        self._fail_on = fail_on
        self.statements = []
        self.events = events if events is not None else []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, stmt):
        self.statements.append(stmt)
        self.events.append("execute")
        if self._fail_on is not None and len(self.statements) == self._fail_on:
            raise RuntimeError("boom")
        return self._results.pop(0) if self._results else FakeResult()

    async def commit(self):
        self.commits += 1
        self.events.append("commit")

    async def rollback(self):
        self.rollbacks += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class RevisionSession(FakeSession):
    """Session dont ``SELECT version_num`` rend une suite de révisions.

    ``None`` simule une base sans table ``alembic_version`` (jamais migrée) :
    la vraie session lève alors, et la garde doit rollbacker.
    """

    def __init__(self, revisions):
        super().__init__()
        self._revisions = list(revisions)

    async def execute(self, stmt):
        self.statements.append(stmt)
        revision = self._revisions.pop(0) if self._revisions else None
        if revision is None:
            raise ProgrammingError("SELECT version_num", {}, Exception("no table"))
        return FakeResult(rows=[revision])


class FakeStorage:
    """Faux S3 : listing scripté, HEAD scripté, suppressions enregistrées."""

    def __init__(self, pages=None, events=None, missing=()):
        self._pages = pages or {}
        # Clés dont le HEAD ne trouve rien (404) : l'objet a disparu du bucket.
        self._missing = set(missing)
        self.deleted: list[str] = []
        self.listed: list[str] = []
        self.headed: list[str] = []
        self.events = events if events is not None else []
        self.in_flight = 0
        self.max_in_flight = 0

    async def delete_many(self, s3_keys):
        self.deleted.extend(s3_keys)
        self.events.append("delete_many")

    async def head(self, s3_key):
        self.headed.append(s3_key)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        # Rend la main pour que les requêtes concurrentes démarrent : sans ça,
        # la mesure de concurrence verrait toujours 1.
        await asyncio.sleep(0)
        self.in_flight -= 1
        return None if s3_key in self._missing else {"ContentLength": 1}

    async def iter_objects(self, prefix, page_size=1000):
        self.listed.append(prefix)
        for page in self._pages.get(prefix, []):
            yield page


def compiled_sql(stmt) -> str:
    """Le SQL compilé pour Postgres, sur une ligne — motif de la suite."""
    return str(stmt.compile(dialect=postgresql.dialect())).replace("\n", " ")


def compiled_params(stmt) -> dict:
    return stmt.compile(dialect=postgresql.dialect()).params


def s3_object(key: str, age_days: float = 0, size: int = 1) -> S3Object:
    return S3Object(
        key=key, last_modified=datetime.now(UTC) - timedelta(days=age_days), size=size
    )
