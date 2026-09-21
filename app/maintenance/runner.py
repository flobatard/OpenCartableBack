"""Exécution d'**un** job : leviers d'inactivité, garde, chrono, état.

Ce module existe pour que le scheduler et le one-shot aient *exactement* la même
sémantique — une divergence entre « ce que fait le conteneur » et « ce que fait
la commande à la main » serait un piège à diagnostic. Il n'importe pas
APScheduler : la boucle de vie est l'affaire de :mod:`app.maintenance.scheduler`,
et cette séparation rend tout ce qui suit testable avec la fausse session FIFO
du projet.

**Ordre des execute sur la session du job — c'est un contrat**, rejoué par les
tests :

1. *aucun* si la rétention est nulle (la tâche est désactivée, on sort avant) ;
2. ``SELECT version_num`` — la garde de schéma, sans attente ;
3. ``SELECT last_detail`` — seulement si le job a demandé son détail précédent ;
4. les execute de la tâche elle-même.

L'écriture de l'état ne passe **jamais** par cette session (cf.
:func:`app.maintenance.state.record_state`).
"""

import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import correlation_scope, new_correlation_id
from app.core.storage import Storage
from app.maintenance.registry import MaintenanceJob, retention_for
from app.maintenance.results import (
    SKIP_RETENTION,
    SKIP_SCHEMA,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    JobContext,
    JobOutcome,
    JobResult,
    MaintenanceReport,
)
from app.maintenance.schema import is_current
from app.maintenance.state import format_error, load_detail, record_state

logger = logging.getLogger(__name__)


async def record_skip(job: MaintenanceJob, reason: str) -> JobResult:
    """Enregistre une passe qui n'a pas eu lieu, et dit pourquoi.

    La raison va dans ``last_error`` et **non** dans ``last_detail`` : celui-ci
    porte le curseur de rotation de ``missing_s3_objects``, qu'un ``skipped``
    ne doit surtout pas effacer. ``last_error`` se lit donc « pourquoi cette
    passe n'est pas un succès franc » — il est remis à ``NULL`` au premier
    ``ok``.
    """
    now = datetime.now(UTC)
    await _record_safely(
        job,
        started_at=now,
        finished_at=now,
        status=STATUS_SKIPPED,
        count=0,
        duration_ms=0,
        error=reason,
        detail=None,
    )
    return JobResult(name=job.name, status=STATUS_SKIPPED)


async def run_job(
    job: MaintenanceJob, *, db: AsyncSession, storage: Storage | None = None
) -> JobResult:
    """Exécute une passe du job et en enregistre l'issue.

    Les **deux leviers d'inactivité** sont distincts et le restent jusque dans
    les logs : une rétention à ``0`` désactive la *tâche* (le job est bien
    planifié, il sort ici en ``skipped``), tandis qu'un cron vide ne planifie
    pas le *job* — celui-là n'atteint jamais cette fonction.

    Un échec est journalisé avec sa stacktrace, la session est rollbackée et
    l'état est écrit quand même : c'est précisément quand un job tombe que
    savoir qu'il est tombé compte.
    """
    # Un id de corrélation PAR JOB, pas par passe : run_jobs en enchaîne jusqu'à
    # neuf, et ce qu'on veut grepper c'est « tout ce qu'a produit s3_orphans
    # cette nuit-là » — lignes de service.py, checks.py, state.py et schema.py
    # comprises. Utile même sur une passe d'un seul job : la boucle de relève
    # Redis du scheduler tourne dans la même boucle asyncio, et ses lignes
    # s'entrelacent aujourd'hui sans moyen de les démêler.
    with correlation_scope(new_correlation_id()):
        retention = retention_for(job)
        if retention is not None and retention <= 0:
            logger.info("job %s : ignoré — rétention à 0 (tâche désactivée)", job.label)
            return await record_skip(job, SKIP_RETENTION)

        if not await is_current(db, job_label=f"job {job.label}"):
            return await record_skip(job, SKIP_SCHEMA)

        previous_detail = None
        if job.needs_previous_detail:
            previous_detail = await load_detail(db, job.name)

        logger.info("job %s : démarrage", job.label)
        started_at = datetime.now(UTC)
        started = time.monotonic()
        count, detail, error = 0, None, None
        status = STATUS_OK
        try:
            outcome = await job.bind(
                JobContext(db=db, storage=storage, previous_detail=previous_detail)
            )()
        except Exception as exc:  # noqa: BLE001 — un job qui tombe n'arrête pas les autres
            status = STATUS_FAILED
            error = format_error(exc)
            logger.exception("job %s : échec", job.label)
            await db.rollback()
        else:
            if isinstance(outcome, JobOutcome):
                count, detail = outcome.count, outcome.detail
            else:
                count = outcome
        duration_ms = int((time.monotonic() - started) * 1000)

        if status == STATUS_OK:
            logger.info("job %s : %d en %d ms", job.label, count, duration_ms)
        else:
            logger.error("job %s : échec en %d ms", job.label, duration_ms)

        await _record_safely(
            job,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            status=status,
            count=count,
            duration_ms=duration_ms,
            error=error,
            detail=detail,
        )
        return JobResult(
            name=job.name, status=status, count=count, duration_ms=duration_ms
        )


async def run_jobs(
    jobs: Iterable[MaintenanceJob],
    *,
    db: AsyncSession,
    storage: Storage | None = None,
) -> MaintenanceReport:
    """Enchaîne des jobs en isolant chaque échec — la passe continue toujours.

    Une erreur sur un jeu de données ne doit priver aucun autre de sa purge :
    :func:`run_job` absorbe déjà les exceptions, cette fonction ne fait que
    séquencer et agréger.
    """
    return MaintenanceReport(
        results=[await run_job(job, db=db, storage=storage) for job in jobs]
    )


async def _record_safely(job: MaintenanceJob, **state) -> None:
    """Écrit l'état sans jamais laisser son propre échec masquer celui du job.

    Même règle que le remboursement de quota : une écriture d'intendance qui
    tombe se journalise et se tait. Perdre la trace d'une passe est ennuyeux ;
    perdre son résultat parce que la trace a échoué le serait davantage.
    """
    try:
        await record_state(job.name, **state)
    except Exception:  # noqa: BLE001 — intendance : jamais fatale
        logger.exception("job %s : état non enregistré", job.label)
