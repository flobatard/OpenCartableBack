"""Tests du job de purge (:mod:`app.maintenance`) — sans Postgres ni S3.

Deux motifs déjà en place dans la suite :

- **SQL compilé** (``stmt.compile(dialect=postgresql.dialect())`` + ses
  ``params``), comme ``test_search_api.py`` et ``test_student_exercises_api.py``:
  chaque tâche est vérifiée sur sa table, son prédicat et sa borne — c'est là
  que se jouent les pièges (fuseau, plancher de rétention, prédicat de
  ``share_links``), pas dans un aller-retour base.
- **Faux client S3** enregistrant ses appels (motif ``_FakeStorage`` de
  ``test_resources_api.py``), enrichi de ``iter_objects``.

Les deux fakes partagent une liste d'``events`` : c'est ce qui permet
d'affirmer que la purge du bucket a bien lieu **après** le commit.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.sql.dml import Delete, Update

from app.core.config import settings
from app.core.storage import S3Object
from app.maintenance import schema as schema_guard
from app.maintenance import service as maintenance


class _FakeResult:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
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
        return self._results.pop(0) if self._results else _FakeResult()

    async def commit(self):
        self.commits += 1
        self.events.append("commit")

    async def rollback(self):
        self.rollbacks += 1


class _FakeStorage:
    """Faux S3 : listing scripté par préfixe, suppressions enregistrées."""

    def __init__(self, pages=None, events=None):
        self._pages = pages or {}
        self.deleted: list[str] = []
        self.listed: list[str] = []
        self.events = events if events is not None else []

    async def delete_many(self, s3_keys):
        self.deleted.extend(s3_keys)
        self.events.append("delete_many")

    async def iter_objects(self, prefix, page_size=1000):
        self.listed.append(prefix)
        for page in self._pages.get(prefix, []):
            yield page


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect())).replace("\n", " ")


def _params(stmt) -> dict:
    return stmt.compile(dialect=postgresql.dialect()).params


def _obj(key: str, age_days: float) -> S3Object:
    return S3Object(
        key=key, last_modified=datetime.now(UTC) - timedelta(days=age_days), size=1
    )


# ─────────────────────────────────────────────
# Rétention 0 = tâche désactivée
# ─────────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "task",
    [
        maintenance.purge_ai_daily_usage,
        maintenance.purge_tool_message_content,
        maintenance.purge_ai_conversations,
        maintenance.purge_exercise_submissions,
        maintenance.purge_share_links,
    ],
)
async def test_retention_zero_disables_task(task):
    """Rétention nulle : la tâche sort avant le moindre execute."""
    db = _FakeSession()
    assert await task(db, 0) == 0
    assert db.statements == []
    assert db.commits == 0


@pytest.mark.anyio
async def test_retention_zero_disables_s3_tasks():
    db, storage = _FakeSession(), _FakeStorage()
    assert await maintenance.purge_pending_resources(db, storage, 0) == 0
    assert await maintenance.reconcile_s3_orphans(db, storage, 0, dry_run=False) == 0
    assert db.statements == []
    assert storage.listed == []
    assert storage.deleted == []


# ─────────────────────────────────────────────
# Compteurs de quota
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_ai_daily_usage_cutoff_is_a_utc_date():
    """La borne est un ``date`` UTC, pas un timestamp du fuseau serveur."""
    db = _FakeSession([_FakeResult(rowcount=7)])
    assert await maintenance.purge_ai_daily_usage(db, 90) == 7

    stmt = db.statements[0]
    assert isinstance(stmt, Delete)
    assert "DELETE FROM ai_daily_usage" in _sql(stmt)
    cutoff = _params(stmt)["day_1"]
    assert cutoff == datetime.now(UTC).date() - timedelta(days=90)
    assert not isinstance(cutoff, datetime)  # un date, pas un datetime
    assert db.commits == 1


@pytest.mark.anyio
@pytest.mark.parametrize("days", [1, 2])
async def test_ai_daily_usage_never_touches_today_nor_yesterday(days):
    """Plancher dur : le jour courant porte le quota vivant, la veille peut
    encore recevoir un remboursement à cheval sur minuit UTC."""
    db = _FakeSession()
    await maintenance.purge_ai_daily_usage(db, days)

    cutoff = _params(db.statements[0])["day_1"]
    today = datetime.now(UTC).date()
    assert cutoff <= today - timedelta(days=maintenance.MIN_USAGE_RETENTION_DAYS)


# ─────────────────────────────────────────────
# Contenu des tours tool
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_tool_content_is_trimmed_not_deleted():
    """UPDATE et non DELETE : la ligne doit survivre pour rester appariée à
    son tool_call_id. Seuls les tours ``tool`` sont concernés."""
    db = _FakeSession([_FakeResult(rowcount=3)])
    assert await maintenance.purge_tool_message_content(db, 180) == 3

    stmt = db.statements[0]
    assert isinstance(stmt, Update)
    sql = _sql(stmt)
    assert sql.startswith("UPDATE ai_messages SET content=")
    assert "left(ai_messages.content" in sql
    assert "ai_messages.role =" in sql
    params = _params(stmt)
    assert params["role_1"] == "tool"
    assert params["left_1"] == maintenance.TOOL_CONTENT_KEEP_CHARS
    assert params["left_2"] == maintenance.TOOL_CONTENT_MARKER


@pytest.mark.anyio
async def test_tool_content_trim_is_idempotent():
    """Le seuil de longueur vaut exactement la taille d'une ligne déjà allégée :
    une seconde passe ne la resélectionne pas."""
    db = _FakeSession()
    await maintenance.purge_tool_message_content(db, 180)

    threshold = _params(db.statements[0])["length_1"]
    trimmed_length = maintenance.TOOL_CONTENT_KEEP_CHARS + len(
        maintenance.TOOL_CONTENT_MARKER
    )
    assert threshold == trimmed_length
    assert not trimmed_length > threshold  # le prédicat est un `>` strict


# ─────────────────────────────────────────────
# Conversations, tentatives, liens
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_conversations_purged_on_last_activity():
    db = _FakeSession([_FakeResult(rowcount=2)])
    assert await maintenance.purge_ai_conversations(db, 365) == 2

    sql = _sql(db.statements[0])
    assert "DELETE FROM ai_conversations" in sql
    assert "updated_at <" in sql  # dernière activité, pas la création


@pytest.mark.anyio
async def test_exercise_submissions_purged_on_created_at():
    db = _FakeSession([_FakeResult(rowcount=5)])
    assert await maintenance.purge_exercise_submissions(db, 365) == 5

    sql = _sql(db.statements[0])
    assert "DELETE FROM exercise_submissions" in sql
    assert "created_at <" in sql


@pytest.mark.anyio
async def test_share_links_purged_on_expiry_only():
    """Un lien révoqué mais non expiré est délibérément conservé (audit) :
    le prédicat ne doit mentionner que ``expires_at``."""
    db = _FakeSession([_FakeResult(rowcount=4)])
    assert await maintenance.purge_share_links(db, 365) == 4

    sql = _sql(db.statements[0])
    assert "DELETE FROM share_links WHERE share_links.expires_at <" in sql
    assert "revoked" not in sql


# ─────────────────────────────────────────────
# Ressources pending
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_pending_resources_purge_s3_after_commit():
    """Motif ``delete_course`` : clés relevées, DELETE, commit, PUIS bucket."""
    events: list[str] = []
    db = _FakeSession(
        [_FakeResult(rows=["courses/c/resources/r/a.pdf"]), _FakeResult(rowcount=1)],
        events=events,
    )
    storage = _FakeStorage(events=events)

    assert await maintenance.purge_pending_resources(db, storage, 30) == 1

    assert storage.deleted == ["courses/c/resources/r/a.pdf"]
    assert events == ["execute", "execute", "commit", "delete_many"]
    assert "resources.status =" in _sql(db.statements[0])
    assert _params(db.statements[0])["status_1"] == "pending"


@pytest.mark.anyio
async def test_pending_resources_noop_without_candidates():
    """Aucune ressource à purger : pas de DELETE, pas d'appel S3."""
    events: list[str] = []
    db = _FakeSession([_FakeResult(rows=[])], events=events)
    storage = _FakeStorage(events=events)

    assert await maintenance.purge_pending_resources(db, storage, 30) == 0
    assert len(db.statements) == 1  # le select seul
    assert db.commits == 0
    assert storage.deleted == []


# ─────────────────────────────────────────────
# Réconciliation des orphelins S3
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_orphans_spare_referenced_and_recent_keys():
    """Une clé référencée en base est épargnée ; une clé plus jeune que la
    grâce aussi (fenêtre d'un import ou d'un upload en cours)."""
    known = "courses/c/resources/known/a.pdf"
    orphan = "courses/c/resources/gone/b.pdf"
    fresh = "courses/c/resources/new/c.pdf"
    storage = _FakeStorage(
        {"courses/": [[_obj(known, 200), _obj(orphan, 200), _obj(fresh, 1)]]}
    )
    db = _FakeSession([_FakeResult(rows=[known]), _FakeResult(rows=[])])

    found = await maintenance.reconcile_s3_orphans(db, storage, 90, dry_run=False)

    assert found == 1
    assert storage.deleted == [orphan]
    # `fresh` n'a même pas été soumis à l'anti-jointure : trop récent.
    assert fresh not in _params(db.statements[0]).values()


@pytest.mark.anyio
async def test_orphans_dry_run_deletes_nothing():
    orphan = "courses/c/resources/gone/b.pdf"
    storage = _FakeStorage({"courses/": [[_obj(orphan, 200)]]})
    db = _FakeSession([_FakeResult(rows=[]), _FakeResult(rows=[])])

    assert await maintenance.reconcile_s3_orphans(db, storage, 90, dry_run=True) == 1
    assert storage.deleted == []


@pytest.mark.anyio
async def test_orphans_spare_avatars_still_referenced():
    """L'anti-jointure interroge aussi ``users.avatar_s3_key``."""
    avatar = "users/u/avatar/x/avatar.png"
    storage = _FakeStorage({"users/": [[_obj(avatar, 200)]]})
    db = _FakeSession([_FakeResult(rows=[]), _FakeResult(rows=[avatar])])

    assert await maintenance.reconcile_s3_orphans(db, storage, 90, dry_run=False) == 0
    assert storage.deleted == []


@pytest.mark.anyio
async def test_orphans_sweep_only_known_prefixes():
    """Le bucket peut contenir autre chose : on ne balaye que nos deux préfixes."""
    storage = _FakeStorage()
    await maintenance.reconcile_s3_orphans(_FakeSession(), storage, 90, dry_run=True)
    assert storage.listed == ["courses/", "users/"]


@pytest.mark.anyio
async def test_orphans_process_pages_independently():
    """Deux pages ⇒ deux anti-jointures : le bucket n'est jamais tenu en RAM."""
    page_one, page_two = "courses/a/1", "courses/a/2"
    storage = _FakeStorage({"courses/": [[_obj(page_one, 200)], [_obj(page_two, 200)]]})
    db = _FakeSession([_FakeResult(rows=[]) for _ in range(4)])

    assert await maintenance.reconcile_s3_orphans(db, storage, 90, dry_run=False) == 2
    assert len(db.statements) == 4  # 2 pages × (resources + users)
    assert storage.deleted == [page_one, page_two]
    assert db.commits == 0  # la réconciliation ne touche jamais la base


# ─────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────


@pytest.fixture
def purge_settings(monkeypatch):
    """Toutes les tâches activées, sauf indication contraire du test."""
    for name, value in {
        "PURGE_AI_USAGE_DAYS": 90,
        "PURGE_AI_TOOL_CONTENT_DAYS": 60,
        "PURGE_AI_CONVERSATIONS_DAYS": 365,
        "PURGE_EXERCISE_SUBMISSIONS_DAYS": 365,
        "PURGE_SHARE_LINKS_DAYS": 365,
        "PURGE_PENDING_RESOURCES_DAYS": 30,
        "PURGE_S3_ORPHANS_DAYS": 90,
        "PURGE_S3_ORPHANS_DRY_RUN": True,
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest.mark.anyio
async def test_run_purge_reports_every_task(purge_settings):
    db = _FakeSession([_FakeResult(rowcount=1) for _ in range(6)])
    report = await maintenance.run_purge(db, _FakeStorage())

    assert not report.failed
    assert [task.name for task in report.tasks] == [
        "compteurs_quota",
        "contenu_tours_tool",
        "conversations_ia",
        "tentatives_eleves",
        "liens_partage",
        "ressources_pending",
        "orphelins_s3",
    ]


@pytest.mark.anyio
async def test_run_purge_isolates_a_failing_task(purge_settings):
    """Une tâche qui tombe est rollbackée et journalisée ; les suivantes
    tournent quand même, et la passe est signalée en échec."""
    db = _FakeSession(
        [_FakeResult(rowcount=1) for _ in range(6)], fail_on=1
    )  # la première tâche lève

    report = await maintenance.run_purge(db, _FakeStorage())

    assert report.failed
    assert report.tasks[0].failed and report.tasks[0].count == 0
    assert db.rollbacks == 1
    assert [task.failed for task in report.tasks[1:]] == [False] * 6
    assert "compteurs_quota=échec" in report.summary()


@pytest.mark.anyio
async def test_run_purge_skips_disabled_tasks(monkeypatch, purge_settings):
    """Les défauts prudents (conversations et tentatives à 0) n'émettent rien."""
    monkeypatch.setattr(settings, "PURGE_AI_CONVERSATIONS_DAYS", 0)
    monkeypatch.setattr(settings, "PURGE_EXERCISE_SUBMISSIONS_DAYS", 0)
    db = _FakeSession([_FakeResult(rowcount=1) for _ in range(4)])

    report = await maintenance.run_purge(db, _FakeStorage())

    assert not report.failed
    tables = " ".join(_sql(stmt) for stmt in db.statements)
    assert "ai_conversations" not in tables
    assert "exercise_submissions" not in tables


# ─────────────────────────────────────────────
# Garde de schéma
# ─────────────────────────────────────────────


class _RevisionSession(_FakeSession):
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
        return _FakeResult(rows=[revision])


@pytest.mark.anyio
async def test_expected_head_is_the_image_alembic_head():
    """La tête est lue dans le dossier alembic/ embarqué, sans dépendre du cwd."""
    assert schema_guard.ALEMBIC_DIR.is_dir()
    assert schema_guard.expected_head()  # une chaîne de révision non vide


@pytest.mark.anyio
async def test_schema_guard_passes_when_revisions_match(monkeypatch):
    monkeypatch.setattr(schema_guard, "expected_head", lambda: "abc123")
    db = _RevisionSession(["abc123"])

    assert await schema_guard.wait_until_current(db, timeout=0, poll=0) is True


@pytest.mark.anyio
async def test_schema_guard_waits_then_passes(monkeypatch):
    """Migration en cours côté api : la garde repasse dès que la base rattrape."""
    monkeypatch.setattr(schema_guard, "expected_head", lambda: "neuve")
    db = _RevisionSession(["ancienne", "ancienne", "neuve"])

    assert await schema_guard.wait_until_current(db, timeout=10, poll=0) is True
    assert len(db.statements) == 3


@pytest.mark.anyio
async def test_schema_guard_gives_up_on_stale_database(monkeypatch):
    """Au-delà du délai, on renonce : ne rien purger est toujours sûr."""
    monkeypatch.setattr(schema_guard, "expected_head", lambda: "neuve")
    db = _RevisionSession(["ancienne"] * 5)

    assert await schema_guard.wait_until_current(db, timeout=0, poll=0) is False


@pytest.mark.anyio
async def test_schema_guard_handles_never_migrated_database(monkeypatch):
    """Pas de table alembic_version : pas de crash, un rollback, et un refus."""
    monkeypatch.setattr(schema_guard, "expected_head", lambda: "neuve")
    db = _RevisionSession([None])

    assert await schema_guard.wait_until_current(db, timeout=0, poll=0) is False
    assert db.rollbacks == 1


@pytest.mark.anyio
async def test_schema_guard_refuses_when_head_is_undeterminable(monkeypatch):
    """Tête introuvable (dossier alembic absent) : on ne touche même pas la base."""
    monkeypatch.setattr(schema_guard, "expected_head", lambda: None)
    db = _RevisionSession([])

    assert await schema_guard.wait_until_current(db, timeout=0, poll=0) is False
    assert db.statements == []
