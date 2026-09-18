"""Écriture de l'état d'un job dans ``maintenance_job_state``.

**Une seule écriture par passe, à la fin.** Rien n'est posé au démarrage d'un
job : la table est un *état de dernière passe*, pas un état d'exécution — « en
cours » vit ailleurs, dans le statut que le scheduler publie sur Redis
(:mod:`app.maintenance.control`), et un état à moitié écrit ne renseignerait
personne tout en doublant les allers-retours de pool.

Trois règles de l'upsert, qui ne sont pas des détails :

1. ``consecutive_failures`` monte sur un échec, **retombe à 0** sur un succès et
   reste **intouché** sur un ``skipped`` — le compteur doit survivre à une
   semaine de schéma désynchronisé sans être ni remis à zéro ni gonflé.
2. ``last_detail`` n'est écrasé **que si** la passe en a produit un : sans cette
   garde, un ``skipped`` effacerait le curseur de rotation de
   ``missing_s3_objects``, qui ne repartirait jamais du bon endroit.
3. ``total_runs`` s'incrémente toujours.

L'écriture passe par une **session dédiée** (:func:`record_state`), jamais celle
du job — voir sa docstring.
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import Insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.maintenance_job_state import (
    STATUS_FAILED,
    STATUS_OK,
    MaintenanceJobState,
)

logger = logging.getLogger(__name__)

TRUNCATION_MARKER = " … (tronqué)"
REDACTED = "***"


def format_error(exc: BaseException) -> str:
    """Message d'erreur stockable : typé, tronqué, sans secret.

    On garde ``Type: message`` et **jamais la stacktrace** — celle-ci vit dans
    les logs (``logger.exception``), où elle a sa place ; en base elle ferait
    grossir une table censée rester à quelques kilo-octets.

    Le mot de passe Postgres est expurgé : certains messages de driver citent
    le DSN complet, et la règle du projet est qu'un secret ne passe jamais dans
    une trace persistée.
    """
    message = f"{type(exc).__name__}: {exc}"
    password = settings.POSTGRES_PASSWORD
    if password:
        message = message.replace(password, REDACTED)
    limit = settings.MAINTENANCE_ERROR_MAX_CHARS
    if len(message) > limit:
        message = message[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return message


def bounded(detail: Any) -> Any:
    """Tronque récursivement toute liste à ``MAINTENANCE_DETAIL_MAX_ITEMS``.

    Un JSONB libre attire la donnée non bornée (« mettons donc toutes les clés
    manquantes ») ; la table d'état doit rester lisible d'un ``psql`` et tenir
    en quelques kilo-octets. Ce qui est tronqué l'est déjà intégralement dans
    les logs de la passe.
    """
    limit = settings.MAINTENANCE_DETAIL_MAX_ITEMS
    if isinstance(detail, list):
        return [bounded(item) for item in detail[:limit]]
    if isinstance(detail, dict):
        return {key: bounded(value) for key, value in detail.items()}
    return detail


def build_state_select(job_name: str) -> Select:
    """Le ``last_detail`` de la passe précédente (curseur de rotation)."""
    return select(MaintenanceJobState.last_detail).where(
        MaintenanceJobState.job_name == job_name
    )


def build_states_select() -> Select:
    """Toutes les lignes d'état, pour le backoffice (une par job au plus)."""
    return select(MaintenanceJobState)


def build_state_upsert(
    job_name: str,
    *,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    count: int,
    duration_ms: int,
    error: str | None,
    detail: dict | None,
) -> Insert:
    """``INSERT … ON CONFLICT (job_name) DO UPDATE`` de l'état d'une passe."""
    statement = pg_insert(MaintenanceJobState).values(
        job_name=job_name,
        last_started_at=started_at,
        last_finished_at=finished_at,
        last_status=status,
        last_count=count,
        last_duration_ms=duration_ms,
        last_error=error,
        last_detail=detail,
        consecutive_failures=1 if status == STATUS_FAILED else 0,
        total_runs=1,
    )
    updates: dict[str, Any] = {
        column: statement.excluded[column]
        for column in (
            "last_started_at",
            "last_finished_at",
            "last_status",
            "last_count",
            "last_duration_ms",
            "last_error",
        )
    }
    updates["total_runs"] = MaintenanceJobState.total_runs + 1
    if status == STATUS_FAILED:
        updates["consecutive_failures"] = MaintenanceJobState.consecutive_failures + 1
    elif status == STATUS_OK:
        updates["consecutive_failures"] = 0
    # `skipped` : ni succès ni échec, le compteur n'est pas touché.
    if detail is not None:
        updates["last_detail"] = statement.excluded.last_detail
    # …sinon la colonne est absente du SET : une passe sans détail ne doit pas
    # effacer le curseur laissé par la précédente.
    return statement.on_conflict_do_update(index_elements=["job_name"], set_=updates)


async def load_detail(db: AsyncSession, job_name: str) -> dict | None:
    """Détail de la passe précédente, sur la session **du job** (lecture seule)."""
    return (await db.execute(build_state_select(job_name))).scalars().first()


async def record_state(
    job_name: str,
    *,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    count: int = 0,
    duration_ms: int = 0,
    error: str | None = None,
    detail: dict | None = None,
) -> None:
    """Écrit l'état sur une session **dédiée** — jamais celle du job.

    Après un échec, la session du job peut être en ``InFailedSqlTransaction``
    ou posée sur une connexion morte : y écrire l'état échouerait précisément
    quand l'état est le plus utile. Un ``rollback()`` préalable n'y suffirait
    pas — il ne ressuscite pas une connexion coupée. Le coût est d'un
    aller-retour de pool par passe, quelques fois par jour.
    """
    async with AsyncSessionLocal() as db:
        await db.execute(
            build_state_upsert(
                job_name,
                started_at=started_at,
                finished_at=finished_at,
                status=status,
                count=count,
                duration_ms=duration_ms,
                error=error,
                detail=bounded(detail) if detail is not None else None,
            )
        )
        await db.commit()
