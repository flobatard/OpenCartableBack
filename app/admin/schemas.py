"""Schémas du backoffice : vue d'ensemble des jobs de maintenance.

Miroir front : ``core/admin/maintenance-jobs.model.ts``.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class SchedulerStatusRead(BaseModel):
    """Statut publié par le scheduler, tel qu'il l'a écrit — sans preuve de vie."""

    started_at: datetime
    # Fuseau résolu des expressions cron, tel que le scheduler l'applique.
    timezone: str
    running_job: str | None
    running_since: datetime | None


class JobRunRead(BaseModel):
    """Dernière passe d'un job (``maintenance_job_state``)."""

    started_at: datetime
    finished_at: datetime
    status: Literal["ok", "failed", "skipped"]
    count: int
    duration_ms: int
    # `Type: message` tronqué et expurgé sur un échec, raison d'un saut
    # (retention_disabled, schema_not_current, busy) ; null sur un succès.
    error: str | None
    detail: dict[str, Any] | None
    consecutive_failures: int
    total_runs: int


class MaintenanceJobRead(BaseModel):
    # Identifiant anglais du registre ; le libellé est l'affaire du front (i18n).
    name: str
    # Expression configurée, telle quelle ("", "off"… = non planifié).
    cron: str
    # Rétention en jours ; null = contrôle en lecture seule ; ≤ 0 = désactivée.
    retention_days: int | None
    # Prochaine occurrence publiée par le scheduler ; null = non planifié, ou
    # aucun statut publié.
    next_run_at: datetime | None
    # Demande de passe manuelle en attente (elle expire si personne ne la prend).
    requested_at: datetime | None
    last_run: JobRunRead | None


class MaintenanceOverviewRead(BaseModel):
    # Redis joignable ? Faux : ni statut ni demandes lisibles, lancements
    # refusés en 503 — l'état des passes, lui, vient de Postgres.
    control_available: bool
    # Dernier statut publié par le scheduler (démarrage, début et fin de passe) ;
    # null = rien de publié : arrêté proprement, pas encore démarré, ou canal
    # indisponible. Aucune vérification de vie ici — c'est l'affaire de docker.
    scheduler: SchedulerStatusRead | None
    # Dans l'ordre du registre.
    jobs: list[MaintenanceJobRead]
