"""Canal de contrôle entre le backoffice (API) et le scheduler — par Redis.

L'API ne fait jamais tourner un job (décisions 14 et 37 : pas de maintenance
dans uvicorn) ; elle parle au conteneur ``scheduler`` par le Redis partagé
(:mod:`app.core.kv`), **jamais par Postgres** — une base managée qui se met en
veille (Neon) doit pouvoir le faire entre deux passes : le scheduler ne touche
la base que pour travailler.

Deux sortes de clés, une par sens :

- ``oc:maintenance:request:<job>`` — **API → scheduler** : une demande de passe
  manuelle, posée en ``SET NX`` (au plus une par job) avec une **expiration**
  (``MAINTENANCE_REQUEST_TTL_SECONDS``) : une demande que personne ne prend —
  scheduler arrêté — disparaît d'elle-même. Le scheduler relève ces clés quand
  aucune passe n'est en vol et prend la plus ancienne en ``GETDEL`` : au plus
  une exécution par demande.
- ``oc:maintenance:status`` — **scheduler → API** : démarrage, fuseau des
  crons, passe en cours et **son propre plan** (prochaines occurrences, UTC),
  republiés à son démarrage puis au début et à la fin de chaque passe — jamais
  périodiquement, et effacés à l'arrêt propre. Personne ne vérifie que le
  scheduler vit : c'est l'affaire de docker (restart, healthcheck).

Ce module n'importe pas APScheduler : l'API l'importe.
"""

import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.core.kv import KeyValueStore

logger = logging.getLogger(__name__)

REQUEST_KEY_PREFIX = "oc:maintenance:request:"
STATUS_KEY = "oc:maintenance:status"


@dataclass(frozen=True)
class ClaimedRequest:
    """Une demande prise en charge — déjà retirée du magasin."""

    job_name: str
    requested_by: str | None
    requested_at: datetime


def request_key(job_name: str) -> str:
    return f"{REQUEST_KEY_PREFIX}{job_name}"


def _load(raw: str | None) -> dict[str, Any] | None:
    """JSON d'une clé, ``None`` si absente ou illisible (jamais une exception)."""
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _requested_at(payload: dict[str, Any] | None) -> datetime | None:
    try:
        return datetime.fromisoformat(payload["requested_at"]) if payload else None
    except (KeyError, TypeError, ValueError):
        return None


def serialize_plan(next_runs: Mapping[str, datetime | None]) -> dict[str, str]:
    """Le plan en JSON : ISO 8601 **en UTC** ; un job sans occurrence est omis.

    APScheduler rend des datetimes dans le fuseau des crons, et ``json.dumps``
    ne sait pas sérialiser un datetime.
    """
    return {
        name: moment.astimezone(UTC).isoformat()
        for name, moment in next_runs.items()
        if moment is not None
    }


# ─────────────────────────────────────────────
# Côté API
# ─────────────────────────────────────────────


async def request_run(kv: KeyValueStore, job_name: str, requested_by: uuid.UUID) -> datetime | None:
    """Dépose une demande ; ``None`` si une demande de ce job attend déjà."""
    requested_at = datetime.now(UTC)
    payload = json.dumps(
        {"requested_at": requested_at.isoformat(), "requested_by": str(requested_by)}
    )
    created = await kv.set_if_absent(
        request_key(job_name), payload, settings.MAINTENANCE_REQUEST_TTL_SECONDS
    )
    return requested_at if created else None


async def pending_requests(kv: KeyValueStore, job_names: Iterable[str]) -> dict[str, datetime]:
    """Demandes en attente, par job (un seul ``MGET``)."""
    names = list(job_names)
    values = await kv.get_many([request_key(name) for name in names])
    pending = {name: _requested_at(_load(raw)) for name, raw in zip(names, values, strict=True)}
    return {name: moment for name, moment in pending.items() if moment is not None}


async def read_status(kv: KeyValueStore) -> dict[str, Any] | None:
    """Dernier statut publié par le scheduler, ``None`` s'il n'y en a pas."""
    return _load(await kv.get(STATUS_KEY))


# ─────────────────────────────────────────────
# Côté scheduler
# ─────────────────────────────────────────────


async def claim_request(kv: KeyValueStore, job_names: Iterable[str]) -> ClaimedRequest | None:
    """Prend la plus ancienne demande en attente, ``None`` s'il n'y en a aucune.

    Un ``MGET`` puis un ``GETDEL`` : si la demande a disparu entre les deux
    (expirée, ou prise ailleurs), on passe à la suivante. Une demande illisible
    est retirée et journalisée plutôt que de bloquer son job jusqu'à expiration.
    """
    names = list(job_names)
    values = await kv.get_many([request_key(name) for name in names])
    candidates = []
    for name, raw in zip(names, values, strict=True):
        if raw is None:
            continue
        moment = _requested_at(_load(raw))
        if moment is None:
            logger.warning("demande illisible pour le job %s — retirée", name)
            await kv.delete(request_key(name))
            continue
        candidates.append((moment, name))
    for _, name in sorted(candidates):
        payload = _load(await kv.take(request_key(name)))
        moment = _requested_at(payload)
        if moment is not None:
            return ClaimedRequest(
                job_name=name, requested_by=payload.get("requested_by"), requested_at=moment
            )
    return None


async def publish_status(
    kv: KeyValueStore,
    *,
    started_at: datetime,
    timezone: str,
    running: tuple[str, datetime] | None,
    next_runs: dict[str, str],
) -> None:
    """Réécrit le statut en entier : démarrage, fuseau, passe en cours, plan."""
    running_job, running_since = running if running is not None else (None, None)
    await kv.put(
        STATUS_KEY,
        json.dumps(
            {
                "started_at": started_at.isoformat(),
                "timezone": timezone,
                "running_job": running_job,
                "running_since": running_since.isoformat() if running_since else None,
                "next_runs": next_runs,
            }
        ),
    )


async def clear_status(kv: KeyValueStore) -> None:
    """Arrêt propre : plus de statut — le backoffice n'affiche rien de périmé."""
    await kv.delete(STATUS_KEY)
