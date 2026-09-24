"""Le registre : **la** liste des jobs de maintenance, et la seule.

Le scheduler, le runner et le one-shot la lisent ; personne ne la duplique.
Ajouter une tâche, c'est ajouter une entrée ici, un réglage ``MAINTENANCE_CRON_*``
dans :mod:`app.core.config` et sa ligne commentée dans les trois
``config/*.yaml`` — rien d'autre.

Les ``name`` sont des identifiants **anglais** : ils servent de clé primaire
dans ``maintenance_job_state`` et d'id de job APScheduler. Le français vit dans
``label``, qui n'apparaît que dans les logs.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial

from app.core.config import Settings, settings
from app.maintenance.checks import missing_s3_objects, storage_inventory
from app.maintenance.results import JobContext, JobOutcome
from app.maintenance.service import (
    purge_ai_conversations,
    purge_ai_daily_usage,
    purge_exercise_submissions,
    purge_pending_resources,
    purge_share_links,
    purge_tool_message_content,
    purge_unsent_attachments,
    reconcile_s3_orphans,
)


@dataclass(frozen=True)
class MaintenanceJob:
    """Une tâche : ce qu'elle est, ce qu'elle exige, quand elle passe."""

    name: str
    label: str
    # Rend un *callable*, pas une coroutine — motif de l'ancien `run_purge` :
    # rien ne démarre avant son tour, et un job qui renonce ne laisse pas
    # derrière lui une coroutine jamais attendue. Les settings sont lus au
    # moment du bind, ce qui garde les fixtures `monkeypatch.setattr` valides.
    bind: Callable[[JobContext], Callable[[], Awaitable[int | JobOutcome]]]
    # Nom du réglage qui porte la cadence. Son DÉFAUT dans `Settings` fait foi :
    # on ne le recopie pas ici, une seconde vérité finirait par diverger.
    cron_setting: str
    # Nom du réglage de rétention. `None` = la tâche n'en a pas (un contrôle).
    retention_setting: str | None = None
    needs_storage: bool = False
    # Le job relit le `last_detail` de sa passe précédente (curseur tournant).
    needs_previous_detail: bool = False


JOBS: tuple[MaintenanceJob, ...] = (
    MaintenanceJob(
        name="ai_usage_counters",
        label="compteurs de quota IA",
        bind=lambda ctx: partial(
            purge_ai_daily_usage, ctx.db, settings.PURGE_AI_USAGE_DAYS
        ),
        cron_setting="MAINTENANCE_CRON_AI_USAGE_COUNTERS",
        retention_setting="PURGE_AI_USAGE_DAYS",
    ),
    MaintenanceJob(
        name="tool_turn_content",
        label="contenu des tours d'outil",
        bind=lambda ctx: partial(
            purge_tool_message_content, ctx.db, settings.PURGE_AI_TOOL_CONTENT_DAYS
        ),
        cron_setting="MAINTENANCE_CRON_TOOL_TURN_CONTENT",
        retention_setting="PURGE_AI_TOOL_CONTENT_DAYS",
    ),
    MaintenanceJob(
        name="ai_conversations",
        label="conversations de l'assistant",
        bind=lambda ctx: partial(
            purge_ai_conversations,
            ctx.db,
            ctx.storage,
            settings.PURGE_AI_CONVERSATIONS_DAYS,
        ),
        cron_setting="MAINTENANCE_CRON_AI_CONVERSATIONS",
        retention_setting="PURGE_AI_CONVERSATIONS_DAYS",
        needs_storage=True,
    ),
    MaintenanceJob(
        name="exercise_submissions",
        label="tentatives d'élèves",
        bind=lambda ctx: partial(
            purge_exercise_submissions, ctx.db, settings.PURGE_EXERCISE_SUBMISSIONS_DAYS
        ),
        cron_setting="MAINTENANCE_CRON_EXERCISE_SUBMISSIONS",
        retention_setting="PURGE_EXERCISE_SUBMISSIONS_DAYS",
    ),
    MaintenanceJob(
        name="share_links",
        label="liens de partage expirés",
        bind=lambda ctx: partial(
            purge_share_links, ctx.db, settings.PURGE_SHARE_LINKS_DAYS
        ),
        cron_setting="MAINTENANCE_CRON_SHARE_LINKS",
        retention_setting="PURGE_SHARE_LINKS_DAYS",
    ),
    MaintenanceJob(
        name="pending_resources",
        label="ressources jamais confirmées",
        bind=lambda ctx: partial(
            purge_pending_resources,
            ctx.db,
            ctx.storage,
            settings.PURGE_PENDING_RESOURCES_DAYS,
        ),
        cron_setting="MAINTENANCE_CRON_PENDING_RESOURCES",
        retention_setting="PURGE_PENDING_RESOURCES_DAYS",
        needs_storage=True,
    ),
    MaintenanceJob(
        name="ai_attachments",
        label="pièces jointes jamais envoyées",
        bind=lambda ctx: partial(
            purge_unsent_attachments,
            ctx.db,
            ctx.storage,
            settings.PURGE_AI_ATTACHMENTS_DAYS,
        ),
        cron_setting="MAINTENANCE_CRON_AI_ATTACHMENTS",
        retention_setting="PURGE_AI_ATTACHMENTS_DAYS",
        needs_storage=True,
    ),
    MaintenanceJob(
        name="s3_orphans",
        label="orphelins du bucket S3",
        bind=lambda ctx: partial(
            reconcile_s3_orphans,
            ctx.db,
            ctx.storage,
            settings.PURGE_S3_ORPHANS_DAYS,
            settings.PURGE_S3_ORPHANS_DRY_RUN,
        ),
        cron_setting="MAINTENANCE_CRON_S3_ORPHANS",
        retention_setting="PURGE_S3_ORPHANS_DAYS",
        needs_storage=True,
    ),
    MaintenanceJob(
        name="missing_s3_objects",
        label="objets S3 manquants",
        bind=lambda ctx: partial(
            missing_s3_objects,
            ctx.db,
            ctx.storage,
            grace_days=settings.MAINTENANCE_MISSING_S3_GRACE_DAYS,
            max_checks=settings.MAINTENANCE_MISSING_S3_MAX_CHECKS,
            concurrency=settings.MAINTENANCE_MISSING_S3_CONCURRENCY,
            cursor=(ctx.previous_detail or {}).get("cursor"),
        ),
        cron_setting="MAINTENANCE_CRON_MISSING_S3_OBJECTS",
        needs_storage=True,
        needs_previous_detail=True,
    ),
    MaintenanceJob(
        name="storage_inventory",
        label="inventaire de volumétrie",
        bind=lambda ctx: partial(storage_inventory, ctx.db, ctx.storage),
        cron_setting="MAINTENANCE_CRON_STORAGE_INVENTORY",
        needs_storage=True,
    ),
)

JOBS_BY_NAME: dict[str, MaintenanceJob] = {job.name: job for job in JOBS}


def cron_for(job: MaintenanceJob) -> str:
    """Expression cron configurée pour ce job (espaces superflus retirés)."""
    return str(getattr(settings, job.cron_setting)).strip()


def default_cron(job: MaintenanceJob) -> str:
    """Cadence par défaut, lue là où elle vit : le défaut du champ ``Settings``."""
    return str(Settings.model_fields[job.cron_setting].default).strip()


def retention_for(job: MaintenanceJob) -> int | None:
    """Rétention configurée en jours, ou ``None`` si le job n'en a pas.

    ``0`` (ou moins) **désactive la tâche** : le job reste planifié, mais sort
    avant le moindre ``execute`` et s'enregistre en ``skipped``. À distinguer
    d'un cron vide, qui ne planifie rien du tout.
    """
    if job.retention_setting is None:
        return None
    return int(getattr(settings, job.retention_setting))
