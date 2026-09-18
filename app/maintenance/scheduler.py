"""Process résident qui déclenche les jobs de maintenance — ``python -m app.maintenance.scheduler``.

C'est le process du service compose ``scheduler`` : un ``AsyncIOScheduler`` qui
dort entre deux occurrences et exécute les coroutines **dans sa propre boucle**
(ni thread, ni sous-process — c'est ce qui rend ``max_instances`` et le verrou
cohérents avec des sessions SQLAlchemy async). Il remplace une boucle shell qui
lançait les sept purges d'un bloc toutes les 24 h : ici chaque job a sa cadence,
et les deux tâches au coût non borné passent une nuit de week-end, séparément.

Quatre pièges, tous vérifiés, dont ce module est en grande partie la parade :

1. **``shutdown(wait=True)`` n'attend pas, il annule.** ``AsyncIOExecutor`` de
   APScheduler 3.x fait ``for f in self._pending_futures: f.cancel()`` — son
   propre commentaire dit qu'honorer ``wait`` demanderait une coroutine. Un
   ``CancelledError`` est une ``BaseException`` : il traverserait le
   ``except Exception`` du runner, tuant une transaction en vol sans que l'état
   soit écrit. D'où le travail détaché dans une tâche trackée, attendue derrière
   un ``asyncio.shield`` : l'annulation frappe l'attente, pas le travail.
2. **``CronTrigger.from_crontab`` indexe 0 = lundi**, là où crontab indexe
   0 = dimanche. Le jour de semaine s'écrit donc **en lettres**, partout, et un
   test le vérifie sur toute la table des jobs.
3. **Heure d'été** : en ``Europe/Paris``, 02:00–02:59 n'existe pas au printemps
   et se produit deux fois à l'automne. Aucune occurrence par défaut n'y tombe.
4. **SIGTERM en PID 1** : le noyau ne délivre pas à PID 1 un signal dont le
   handler est celui par défaut. Sans ``add_signal_handler``, ``docker stop``
   attendrait dix secondes puis SIGKILL — à chaque déploiement.

Pas de passe au démarrage, contrairement à l'ancienne boucle shell : lancer
neuf jobs au boot annulerait l'objectif de répartition, et taperait sur le Pi
au moment précis où l'api applique ses migrations. À la place, le démarrage
**journalise le plan** — cadence et prochaine occurrence de chaque job — et une
passe immédiate reste à un ``docker compose exec scheduler python -m
app.maintenance`` près, ou à un clic dans le backoffice.

Le backoffice passe par le **canal de contrôle** (:mod:`app.maintenance.control`,
Redis) : une boucle asyncio à côté des crons (:func:`consume_requests`) relève
les demandes de passe manuelle et les exécute par le même :func:`_execute` que
les crons — même verrou, même état. Une demande ne patiente jamais derrière le
verrou : elle n'est prise que quand aucune passe n'est en vol. Le statut (passe
en cours, plan) est republié au démarrage puis au début et à la fin de chaque
passe, **jamais périodiquement** ; aucun battement : que le process vive est
l'affaire de docker, et rien ici ne touche Postgres hors des passes (une base
managée qui se met en veille, comme Neon, doit pouvoir le faire entre deux).
"""

import asyncio
import contextlib
import logging
import signal
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.kv import KeyValueStore, KVUnavailable, close_kv, get_kv
from app.core.storage import get_storage
from app.maintenance import control
from app.maintenance.registry import (
    JOBS,
    JOBS_BY_NAME,
    MaintenanceJob,
    cron_for,
    retention_for,
)
from app.maintenance.results import SKIP_BUSY
from app.maintenance.runner import record_skip, run_job
from app.maintenance.schema import wait_until_current

logger = logging.getLogger(__name__)

# Façons d'écrire « ne planifie pas ce job ». Un job non planifié n'atteint
# jamais le runner et n'écrit aucune ligne d'état — à ne pas confondre avec une
# rétention à 0, qui planifie le job et le fait sortir en `skipped`.
DISABLED_CRONS = frozenset({"", "off", "none", "-"})

# Passes en vol, pour les laisser finir après un SIGTERM.
_INFLIGHT: set[asyncio.Task] = set()
# Un seul job de maintenance à la fois : `max_instances=1` ne protège un job que
# contre lui-même, et deux balayages lourds en parallèle sur un Pi, non.
_LOCK = asyncio.Lock()
# La passe qui tient le verrou (nom, début), publiée dans le statut. Scalaire,
# puisque le verrou sérialise tout.
_RUNNING: tuple[str, datetime] | None = None


@dataclass(frozen=True)
class StatusContext:
    """De quoi republier le statut — posé par :func:`main`, absent ailleurs."""

    kv: KeyValueStore
    scheduler: AsyncIOScheduler
    started_at: datetime
    timezone: str


# `None` hors du process résident (tests, one-shot) : rien n'est publié.
_STATUS: StatusContext | None = None

TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"

# Attente maximale de la boucle de contrôle à l'arrêt, avant le drain des passes.
CONTROL_STOP_SECONDS = 5.0
# Après une panne de Redis, la boucle réessaie à ce rythme (une trace, puis rien).
CONTROL_RETRY_SECONDS = 10.0


def resolve_timezone() -> tzinfo:
    """Fuseau des expressions cron, avec repli sûr sur UTC.

    Le repli est ``datetime.UTC`` et non ``ZoneInfo("UTC")`` : si la base tzdata
    manque entièrement, même « UTC » serait introuvable et le repli échouerait
    avec ce qu'il est censé rattraper.
    """
    name = settings.MAINTENANCE_TIMEZONE.strip() or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.error(
            "fuseau %r introuvable (base tzdata absente de l'image ?) — repli sur UTC",
            name,
        )
        return UTC


def build_trigger(job: MaintenanceJob, tz: tzinfo) -> CronTrigger | None:
    """Déclencheur du job, ou ``None`` s'il ne doit pas être planifié.

    Une expression invalide **n'empêche jamais le démarrage** : une faute de
    frappe dans un YAML de production ne doit pas coûter toute la maintenance,
    seulement le job concerné — et une ligne d'erreur bien visible au boot.
    """
    expression = cron_for(job)
    if expression.lower() in DISABLED_CRONS:
        return None
    try:
        return CronTrigger.from_crontab(expression, timezone=tz)
    except (ValueError, TypeError):
        logger.error(
            "job %s : cron invalide %r — job NON PLANIFIÉ "
            "(le scheduler démarre quand même)",
            job.name,
            expression,
        )
        return None


def build_scheduler() -> AsyncIOScheduler:
    """Construit le scheduler et y pose un job par cadence valide — **sans démarrer**.

    Le fuseau est passé au scheduler *et* à chaque déclencheur : ``tzlocal``
    n'est jamais consulté, donc l'absence de ``/etc/timezone`` dans le conteneur
    est sans effet.
    """
    tz = resolve_timezone()
    scheduler = AsyncIOScheduler(
        timezone=tz,
        job_defaults={
            # Plusieurs occurrences en retard (reprise après une longue passe)
            # n'en déclenchent qu'une : les tâches sont idempotentes.
            "coalesce": True,
            "max_instances": 1,
            # Au-delà de ce retard, l'occurrence est SAUTÉE. Pas de rattrapage.
            "misfire_grace_time": settings.MAINTENANCE_MISFIRE_GRACE_SECONDS,
        },
    )
    for job in JOBS:
        trigger = build_trigger(job, tz)
        if trigger is None:
            continue
        scheduler.add_job(
            run_scheduled,
            trigger,
            args=[job],
            id=job.name,
            name=job.label,
            replace_existing=True,
        )
    return scheduler


async def run_scheduled(job: MaintenanceJob) -> None:
    """Détache la passe et l'attend derrière un bouclier.

    Sans le ``shield``, l'annulation émise par ``AsyncIOExecutor.shutdown``
    interromprait le travail lui-même, au milieu d'une transaction et avant
    l'écriture de l'état. Ici elle interrompt l'attente ; la tâche continue et
    :func:`main` la retrouve dans ``_INFLIGHT`` pour lui laisser le temps de
    finir.
    """
    task = _spawn(job)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Volontairement non propagée : le process s'arrête de toute façon, et
        # la ré-émettre ferait cracher une stacktrace APScheduler à chaque
        # `docker stop`.
        logger.info("job %s : arrêt demandé — la passe en cours se termine", job.label)


def _spawn(job: MaintenanceJob) -> asyncio.Task:
    """Détache la passe dans une tâche suivie par ``_INFLIGHT`` (drainée à l'arrêt)."""
    task = asyncio.create_task(_execute(job), name=f"maintenance:{job.name}")
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)
    return task


async def _execute(job: MaintenanceJob) -> None:
    """Prend le verrou de maintenance, ouvre une session neuve, exécute la passe.

    Le statut est republié une fois le verrou pris (« en cours » = en train de
    travailler, pas en attente du verrou) puis après l'écriture de l'état : le
    backoffice voit le résultat avant que la passe en cours disparaisse, et le
    plan publié suit la prochaine occurrence du job.
    """
    global _RUNNING
    try:
        await asyncio.wait_for(
            _LOCK.acquire(), timeout=settings.MAINTENANCE_LOCK_WAIT_SECONDS
        )
    except TimeoutError:
        logger.warning(
            "job %s : ignoré — un autre job de maintenance tient le verrou depuis %d s",
            job.label,
            settings.MAINTENANCE_LOCK_WAIT_SECONDS,
        )
        await record_skip(job, SKIP_BUSY)
        return
    try:
        _RUNNING = (job.name, datetime.now(UTC))
        await publish_status()
        storage = get_storage() if job.needs_storage else None
        async with AsyncSessionLocal() as db:
            await run_job(job, db=db, storage=storage)
    finally:
        # Verrou rendu AVANT la publication : une annulation pendant l'écriture
        # (arrêt au-delà du délai de grâce) ne doit jamais le laisser pris.
        _RUNNING = None
        _LOCK.release()
        await publish_status()


async def publish_status() -> None:
    """Republie le statut (passe en cours, plan) — best effort, jamais au prix
    d'une passe : un Redis en panne ne coûte que l'affichage du backoffice."""
    if _STATUS is None:
        return
    try:
        await control.publish_status(
            _STATUS.kv,
            started_at=_STATUS.started_at,
            timezone=_STATUS.timezone,
            running=_RUNNING,
            # `getattr` : un job pas encore remis à son jobstore (scheduler non
            # démarré) n'a pas d'attribut `next_run_time` du tout.
            next_runs=control.serialize_plan(
                {
                    job.id: getattr(job, "next_run_time", None)
                    for job in _STATUS.scheduler.get_jobs()
                }
            ),
        )
    except KVUnavailable as exc:
        logger.warning("statut du scheduler non publié : %s", exc)


def log_plan(scheduler: AsyncIOScheduler, tz: tzinfo) -> None:
    """Journalise la cadence et la prochaine occurrence de chaque job.

    C'est le remplaçant de la passe au démarrage : après un déploiement, on lit
    dans les logs ce qui va tourner et quand, sans attendre 24 h ni faire
    travailler le Pi.
    """
    now = datetime.now(tz)
    logger.info(
        "scheduler de maintenance — fuseau %s, heure locale %s (UTC %s)",
        tz,
        now.strftime(TIME_FORMAT),
        datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
    )
    scheduled = 0
    for job in JOBS:
        planned = scheduler.get_job(job.name)
        if planned is None:
            continue
        scheduled += 1
        retention = retention_for(job)
        if retention is None:
            note = "contrôle en lecture seule"
        elif retention <= 0:
            note = "rétention 0 j (TÂCHE DÉSACTIVÉE)"
        else:
            note = f"rétention {retention} j"
        logger.info(
            "job %s — %s : cron « %s », %s, prochaine passe %s",
            job.name,
            job.label,
            cron_for(job),
            note,
            planned.next_run_time.strftime(TIME_FORMAT)
            if planned.next_run_time
            else "aucune",
        )
    logger.info(
        "%d job(s) déclaré(s), %d planifié(s), %d non planifié(s) — en attente",
        len(JOBS),
        scheduled,
        len(JOBS) - scheduled,
    )


async def poll_once(kv: KeyValueStore) -> None:
    """Prend au plus une demande de passe manuelle et la lance.

    Rien — pas même une lecture de Redis — tant qu'une passe est en vol ou
    attend le verrou : une demande ne patiente jamais derrière lui (elle
    finirait sautée en ``busy``), elle attend son tour dans le magasin. La
    prise la supprime (``GETDEL``) : au plus une exécution par demande.
    """
    if _INFLIGHT:
        return
    if _STATUS is not None and await control.read_status(kv) is None:
        # Redis redémarré (il est sans persistance) : sans ça, le backoffice
        # afficherait « aucun statut » jusqu'à la prochaine passe.
        await publish_status()
    claimed = await control.claim_request(kv, JOBS_BY_NAME)
    if claimed is None:
        return
    job = JOBS_BY_NAME[claimed.job_name]
    logger.info(
        "job %s — %s : passe manuelle demandée par l'utilisateur %s",
        job.name,
        job.label,
        claimed.requested_by or "(inconnu)",
    )
    _spawn(job)


async def consume_requests(kv: KeyValueStore, stop: asyncio.Event) -> None:
    """Relève des demandes de passe manuelle, jusqu'à ``stop``.

    Une tâche asyncio et non un job APScheduler à intervalle : l'exécuteur
    écrit deux lignes INFO par exécution, et le job fausserait ``get_jobs()``
    — donc le plan journalisé et publié. La relève ne lit que Redis, jamais
    Postgres. Une panne du canal est journalisée **une fois**, trace comprise,
    puis tue jusqu'au premier tour réussi ; elle ne coûte jamais une passe
    planifiée : les crons ne dépendent pas de cette boucle.
    """
    failing = False
    while not stop.is_set():
        try:
            await poll_once(kv)
        except Exception:
            if not failing:
                logger.exception(
                    "canal de contrôle indisponible — nouvel essai toutes les %d s, "
                    "sans nouveau log jusqu'à son rétablissement",
                    CONTROL_RETRY_SECONDS,
                )
                failing = True
            delay = CONTROL_RETRY_SECONDS
        else:
            if failing:
                logger.info("canal de contrôle rétabli")
                failing = False
            delay = settings.MAINTENANCE_REQUEST_POLL_SECONDS
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)


async def _clear_status(kv: KeyValueStore) -> None:
    """À l'arrêt propre : plus de statut, le backoffice n'affiche rien de périmé."""
    try:
        await control.clear_status(kv)
    except KVUnavailable as exc:
        logger.warning("statut du scheduler non effacé à l'arrêt : %s", exc)


async def main() -> int:
    global _STATUS
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover — plateformes sans signaux
            logger.warning("signal %s non gérable ici : arrêt propre indisponible", sig)

    tz = resolve_timezone()
    scheduler = build_scheduler()
    kv = get_kv()
    control_task: asyncio.Task | None = None
    try:
        async with AsyncSessionLocal() as db:
            # NON fatal, contrairement au one-shot : ce process est résident et
            # `restart: unless-stopped`. Sortir en erreur pendant que l'api
            # migre mettrait le conteneur en boucle de crash — alors que chaque
            # job se garde tout seul, juste avant de travailler.
            await wait_until_current(db)
        scheduler.start()
        log_plan(scheduler, tz)
        _STATUS = StatusContext(
            kv=kv, scheduler=scheduler, started_at=datetime.now(UTC), timezone=str(tz)
        )
        await publish_status()
        control_task = asyncio.create_task(
            consume_requests(kv, stop), name="maintenance:control"
        )
        await stop.wait()
    finally:
        logger.info("arrêt demandé — plus aucune occurrence n'est déclenchée")
        # Plus aucune demande n'est relevée ; la boucle attend `stop`, elle
        # sort aussitôt — borné au cas où une écriture traînerait, et court :
        # ce délai s'ajoute à celui du drain, sous le stop_grace_period (30 s).
        stop.set()
        if control_task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(control_task, timeout=CONTROL_STOP_SECONDS)
        # `wait=True` serait un mensonge : l'exécuteur asyncio annule au lieu
        # d'attendre. On draine nous-mêmes les passes en vol.
        scheduler.shutdown(wait=False)
        if _INFLIGHT:
            _, pending = await asyncio.wait(
                set(_INFLIGHT), timeout=settings.MAINTENANCE_SHUTDOWN_GRACE_SECONDS
            )
            if pending:
                logger.warning(
                    "%d job(s) encore en cours après %d s — abandon",
                    len(pending),
                    settings.MAINTENANCE_SHUTDOWN_GRACE_SECONDS,
                )
        await _clear_status(kv)
        _STATUS = None
        await close_kv()
        await engine.dispose()
        logger.info("scheduler arrêté proprement")
    return 0


if __name__ == "__main__":
    # Process autonome : il pose sa propre config de log (l'API n'en a aucune).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s",
    )
    sys.exit(asyncio.run(main()))
