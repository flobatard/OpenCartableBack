"""Tests de l'exécution d'un job (:mod:`app.maintenance.runner`).

L'**ordre des execute est un contrat**, comme partout dans ce projet — ici il
est plus long qu'ailleurs parce que le runner en insère avant ceux de la tâche :
la garde de schéma, puis le détail de la passe précédente. C'est ce que vérifie
``test_run_job_execute_order_is_a_contract``.

Les **deux leviers d'inactivité** ont chacun leur test, et ils ne disent pas la
même chose : une rétention à 0 sort **avant le moindre execute** (la tâche est
désactivée), un schéma désynchronisé sort **après la seule garde** (la passe est
remise à plus tard). Le troisième cas, un cron vide, n'atteint jamais ce module.
"""

from dataclasses import replace
from functools import partial

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.maintenance import runner, state
from app.maintenance.registry import JOBS, JOBS_BY_NAME
from app.maintenance.results import (
    SKIP_RETENTION,
    SKIP_SCHEMA,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
)
from tests.maintenance_fakes import FakeResult, FakeSession, FakeStorage, compiled_sql


@pytest.fixture
def recorded(monkeypatch):
    """Capture les écritures d'état au lieu de les envoyer en base."""
    written: list[dict] = []

    async def record(job_name, **state_kwargs):
        written.append({"job_name": job_name, **state_kwargs})

    monkeypatch.setattr(runner, "record_state", record)
    return written


@pytest.fixture
def current_schema(monkeypatch):
    """La base est à la révision de l'image — la garde laisse passer."""

    async def is_current(db, *, job_label=""):
        await db.execute(text("SELECT version_num FROM alembic_version"))
        return True

    monkeypatch.setattr(runner, "is_current", is_current)


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


# ─────────────────────────────────────────────
# Les deux leviers d'inactivité
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_run_job_skips_a_disabled_retention_before_any_execute(
    monkeypatch, recorded, current_schema
):
    """Rétention à 0 : la tâche est désactivée, rien n'est même demandé à la
    base — pas même la garde de schéma."""
    job = JOBS_BY_NAME["ai_conversations"]
    monkeypatch.setattr(settings, "PURGE_AI_CONVERSATIONS_DAYS", 0)
    db = FakeSession()

    result = await runner.run_job(job, db=db, storage=None)

    assert result.status == STATUS_SKIPPED
    assert db.statements == []
    assert recorded[0]["error"] == SKIP_RETENTION


@pytest.mark.anyio
async def test_run_job_skips_when_schema_is_not_current(
    monkeypatch, recorded, purge_settings
):
    """Le scheduler vit des semaines pendant que l'api migre : la garde est
    rejouée avant chaque job, et une passe sautée n'est jamais un échec."""

    async def stale(db, *, job_label=""):
        await db.execute(text("SELECT version_num FROM alembic_version"))
        return False

    monkeypatch.setattr(runner, "is_current", stale)
    db = FakeSession()

    result = await runner.run_job(JOBS_BY_NAME["share_links"], db=db, storage=None)

    assert result.status == STATUS_SKIPPED
    assert len(db.statements) == 1  # la garde, et rien de la tâche
    assert recorded[0]["error"] == SKIP_SCHEMA


@pytest.mark.anyio
async def test_a_skip_does_not_touch_the_detail(monkeypatch, recorded, current_schema):
    """La raison d'un saut va dans ``last_error``, jamais dans ``last_detail`` :
    celui-ci porte le curseur de rotation, qu'un saut ne doit pas effacer."""
    monkeypatch.setattr(settings, "PURGE_SHARE_LINKS_DAYS", 0)

    await runner.run_job(JOBS_BY_NAME["share_links"], db=FakeSession(), storage=None)

    assert recorded[0]["detail"] is None


# ─────────────────────────────────────────────
# Une passe nominale
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_run_job_records_ok_with_count_and_duration(
    recorded, current_schema, purge_settings
):
    db = FakeSession([FakeResult(rows=["rev"]), FakeResult(rowcount=12)])

    result = await runner.run_job(JOBS_BY_NAME["share_links"], db=db, storage=None)

    assert (result.status, result.count) == (STATUS_OK, 12)
    assert result.duration_ms >= 0
    assert recorded[0]["status"] == STATUS_OK
    assert recorded[0]["count"] == 12
    assert recorded[0]["error"] is None


@pytest.mark.anyio
async def test_run_job_execute_order_is_a_contract(
    monkeypatch, recorded, current_schema, purge_settings
):
    """Garde de schéma, puis détail précédent (si le job le demande), puis les
    execute de la tâche."""
    job = JOBS_BY_NAME["missing_s3_objects"]
    monkeypatch.setattr(settings, "MAINTENANCE_MISSING_S3_MAX_CHECKS", 10)
    db = FakeSession(
        [
            FakeResult(rows=["rev"]),  # la garde
            FakeResult(rows=[{"cursor": "courses/a"}]),  # last_detail
            FakeResult(rows=[]),  # avatars
            FakeResult(rows=["courses/b/x.pdf"]),  # ressources
        ]
    )

    await runner.run_job(job, db=db, storage=FakeStorage())

    assert len(db.statements) == 4
    assert "maintenance_job_state.last_detail" in compiled_sql(db.statements[1])
    assert "users.avatar_s3_key" in compiled_sql(db.statements[2])
    # Le curseur de la passe précédente a bien atteint la requête.
    assert "resources.s3_key > " in compiled_sql(db.statements[3])


@pytest.mark.anyio
async def test_a_job_without_previous_detail_does_not_read_it(
    recorded, current_schema, purge_settings
):
    db = FakeSession([FakeResult(rows=["rev"]), FakeResult(rowcount=1)])

    await runner.run_job(JOBS_BY_NAME["share_links"], db=db, storage=None)

    assert len(db.statements) == 2  # garde + DELETE, pas de lecture d'état


# ─────────────────────────────────────────────
# Échecs
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_run_job_rolls_back_and_records_a_failure(
    recorded, current_schema, purge_settings
):
    """C'est précisément quand un job tombe que savoir qu'il est tombé compte :
    la session est rollbackée, et l'état est écrit quand même."""
    db = FakeSession(fail_on=2)  # 1 = la garde, 2 = la tâche

    result = await runner.run_job(JOBS_BY_NAME["share_links"], db=db, storage=None)

    assert result.status == STATUS_FAILED
    assert db.rollbacks == 1
    assert recorded[0]["status"] == STATUS_FAILED
    assert recorded[0]["error"].startswith("RuntimeError: boom")


@pytest.mark.anyio
async def test_a_failing_state_write_never_hides_the_job_result(
    monkeypatch, current_schema, purge_settings
):
    """L'écriture d'état est de l'intendance : son échec se journalise et se
    tait. Perdre la trace d'une passe est ennuyeux ; perdre son résultat parce
    que la trace a échoué le serait davantage."""

    async def exploding(*args, **kwargs):
        raise RuntimeError("état inaccessible")

    monkeypatch.setattr(runner, "record_state", exploding)
    db = FakeSession([FakeResult(rows=["rev"]), FakeResult(rowcount=4)])

    result = await runner.run_job(JOBS_BY_NAME["share_links"], db=db, storage=None)

    assert (result.status, result.count) == (STATUS_OK, 4)


@pytest.mark.anyio
async def test_record_state_is_called_with_the_job_name(recorded, current_schema):
    """La clé primaire de l'état est le nom **anglais** du registre."""
    await runner.record_skip(JOBS_BY_NAME["s3_orphans"], SKIP_SCHEMA)
    assert recorded[0]["job_name"] == "s3_orphans"


# ─────────────────────────────────────────────
# Enchaînement
# ─────────────────────────────────────────────

# Les sept purges : elles partagent une FIFO simple (un execute de garde, puis
# un ou deux de tâche). Les deux contrôles ont leurs propres tests — ce qui se
# vérifie ici est la mécanique d'enchaînement, pas le SQL de chaque tâche.
PURGES = tuple(job for job in JOBS if job.retention_setting is not None)


@pytest.mark.anyio
async def test_run_jobs_covers_the_whole_registry(recorded, current_schema):
    """Les neuf jobs du registre sont rapportés, dans l'ordre du registre.

    Sur des tâches factices : l'enchaînement est une mécanique, indépendante de
    ce que chaque job fait de sa session.
    """
    stubs = [
        replace(job, bind=lambda ctx, n=index: partial(_counted, n), retention_setting=None)
        for index, job in enumerate(JOBS)
    ]

    report = await runner.run_jobs(stubs, db=FakeSession(), storage=None)

    assert [result.name for result in report.results] == [job.name for job in JOBS]
    assert [result.count for result in report.results] == list(range(len(JOBS)))


async def _counted(value: int) -> int:
    return value


@pytest.mark.anyio
async def test_run_jobs_reports_every_purge_in_order(
    recorded, current_schema, purge_settings
):
    db = FakeSession([FakeResult(rowcount=1) for _ in range(30)])

    report = await runner.run_jobs(PURGES, db=db, storage=FakeStorage())

    assert [result.name for result in report.results] == [job.name for job in PURGES]
    assert not report.failed


@pytest.mark.anyio
async def test_run_jobs_isolates_a_failing_job(recorded, current_schema, purge_settings):
    """Une erreur sur un jeu de données ne prive aucun autre de sa purge."""
    # 1 = la garde du premier job, 2 = sa tâche.
    db = FakeSession([FakeResult(rowcount=1) for _ in range(30)], fail_on=2)

    report = await runner.run_jobs(PURGES, db=db, storage=FakeStorage())

    assert report.failed
    assert report.results[0].status == STATUS_FAILED
    assert [r.failed for r in report.results[1:]] == [False] * (len(PURGES) - 1)
    assert "ai_usage_counters=échec" in report.summary()


@pytest.mark.anyio
async def test_run_jobs_skips_disabled_tasks(
    monkeypatch, recorded, current_schema, purge_settings
):
    """Les défauts prudents (conversations et tentatives à 0) n'émettent rien."""
    monkeypatch.setattr(settings, "PURGE_AI_CONVERSATIONS_DAYS", 0)
    monkeypatch.setattr(settings, "PURGE_EXERCISE_SUBMISSIONS_DAYS", 0)
    db = FakeSession([FakeResult(rowcount=1) for _ in range(30)])

    report = await runner.run_jobs(PURGES, db=db, storage=FakeStorage())

    assert not report.failed  # un skip n'est pas un échec
    emitted = " ".join(compiled_sql(stmt) for stmt in db.statements)
    assert "DELETE FROM ai_conversations" not in emitted
    assert "DELETE FROM exercise_submissions" not in emitted


@pytest.mark.anyio
async def test_a_fully_skipped_report_is_not_a_failure(
    monkeypatch, recorded, current_schema
):
    """Code de sortie 0 : ne rien faire est toujours sûr."""
    for job in PURGES:
        monkeypatch.setattr(settings, job.retention_setting, 0)

    report = await runner.run_jobs(PURGES, db=FakeSession(), storage=None)

    assert not report.failed
    assert {result.status for result in report.results} == {STATUS_SKIPPED}


def test_state_module_is_the_only_writer_of_the_state_table():
    """Le runner passe par ``state.record_state`` — jamais par la session du job."""
    assert runner.record_state is state.record_state
