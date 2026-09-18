"""Backoffice : état des jobs de maintenance et demandes de passe manuelle.

L'état des passes vient de Postgres (``maintenance_job_state``) ; le statut du
scheduler et les demandes en attente viennent du canal de contrôle, Redis
(:mod:`app.maintenance.control`). **L'API n'exécute jamais un job**
(décisions 14 et 37) : elle dépose une demande que le scheduler relève.

Un Redis en panne ne rend pas la vue illisible : l'état des passes reste servi,
``control_available`` le dit, et seuls les lancements sont refusés (503).

L'ordre des execute est un contrat, rejoué par la fausse session FIFO
(tests/test_admin_api.py) : :func:`read_overview` et :func:`request_run` n'en
font qu'un, la lecture des états.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import (
    JobRunRead,
    MaintenanceJobRead,
    MaintenanceOverviewRead,
    SchedulerStatusRead,
)
from app.core.http import conflict, not_found, unavailable
from app.core.kv import KeyValueStore, KVUnavailable
from app.maintenance import control
from app.maintenance.registry import JOBS, JOBS_BY_NAME, cron_for, retention_for
from app.maintenance.state import build_states_select
from app.models.maintenance_job_state import MaintenanceJobState
from app.models.user import User

CONTROL_DOWN = "Le canal de contrôle (Redis) ne répond pas : aucune passe ne peut être demandée"


def _last_run(state: MaintenanceJobState | None) -> JobRunRead | None:
    if state is None:
        return None
    return JobRunRead(
        started_at=state.last_started_at,
        finished_at=state.last_finished_at,
        status=state.last_status,
        count=state.last_count,
        duration_ms=state.last_duration_ms,
        error=state.last_error,
        detail=state.last_detail,
        consecutive_failures=state.consecutive_failures,
        total_runs=state.total_runs,
    )


def _scheduler(status: Mapping[str, Any] | None) -> SchedulerStatusRead | None:
    """Le statut publié, ``None`` s'il manque ou ne se lit pas (jamais une 500)."""
    if status is None:
        return None
    try:
        return SchedulerStatusRead.model_validate(status)
    except ValidationError:
        return None


def _next_run(next_runs: Any, job_name: str) -> datetime | None:
    try:
        return datetime.fromisoformat(next_runs[job_name])
    except (KeyError, TypeError, ValueError):
        return None


def build_overview(
    status: Mapping[str, Any] | None,
    states: Mapping[str, MaintenanceJobState],
    requests: Mapping[str, datetime],
    *,
    control_available: bool,
) -> MaintenanceOverviewRead:
    """Assemble la vue, jobs dans l'ordre du registre (pur)."""
    scheduler = _scheduler(status)
    next_runs = status.get("next_runs") if scheduler is not None else None
    return MaintenanceOverviewRead(
        control_available=control_available,
        scheduler=scheduler,
        jobs=[
            MaintenanceJobRead(
                name=job.name,
                cron=cron_for(job),
                retention_days=retention_for(job),
                next_run_at=_next_run(next_runs, job.name),
                requested_at=requests.get(job.name),
                last_run=_last_run(states.get(job.name)),
            )
            for job in JOBS
        ],
    )


async def read_overview(db: AsyncSession, kv: KeyValueStore) -> MaintenanceOverviewRead:
    """Vue d'ensemble. Un execute (les états) ; le reste est lu dans Redis."""
    states = {
        row.job_name: row for row in (await db.execute(build_states_select())).scalars().all()
    }
    try:
        status = await control.read_status(kv)
        requests = await control.pending_requests(kv, JOBS_BY_NAME)
    except KVUnavailable:
        return build_overview(None, states, {}, control_available=False)
    return build_overview(status, states, requests, control_available=True)


async def request_run(
    db: AsyncSession, kv: KeyValueStore, user: User, job_name: str
) -> MaintenanceOverviewRead:
    """Dépose une demande de passe manuelle et rend la vue à jour.

    - job inconnu du registre → 404, avant toute lecture ;
    - Redis injoignable → 503 ;
    - job en cours (selon le statut publié) ou déjà demandé → 409 — le
      ``SET NX`` du dépôt tranche la course entre deux demandes.

    Aucune vérification que le scheduler vit : une demande déposée pendant
    qu'il est arrêté attend son redémarrage, et expire si elle attend trop.
    """
    if job_name not in JOBS_BY_NAME:
        raise not_found("Job de maintenance introuvable")
    overview = await read_overview(db, kv)
    if not overview.control_available:
        raise unavailable(CONTROL_DOWN)
    if overview.scheduler is not None and overview.scheduler.running_job == job_name:
        raise conflict("Ce job est déjà en cours")
    try:
        requested_at = await control.request_run(kv, job_name, user.id)
    except KVUnavailable as exc:
        raise unavailable(CONTROL_DOWN) from exc
    if requested_at is None:
        raise conflict("Une passe de ce job est déjà demandée")
    next(job for job in overview.jobs if job.name == job_name).requested_at = requested_at
    return overview
