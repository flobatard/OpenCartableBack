"""Tests du câblage APScheduler (:mod:`app.maintenance.scheduler`).

**Aucun scheduler n'est démarré** : on construit, on inspecte, on jette. Les
jobs ajoutés avant ``start()`` sont bien rendus par ``get_jobs()`` (ils vivent
dans ``_pending_jobs`` tant que le scheduler est à l'arrêt), ce qui suffit à
vérifier tout ce qui compte ici : qui est planifié, avec quelles options, sous
quel fuseau.

Ce qu'on protège avant tout : **une configuration cassée ne doit jamais coûter
toute la maintenance**. Une expression cron fautive dans un YAML de production
laisse le scheduler démarrer, et seul le job concerné reste au tapis.

La seconde moitié couvre le **canal de contrôle** du backoffice (Redis, ici un
``FakeKV``) : une demande n'est prise que quand aucune passe n'est en vol,
passe par le même ``_execute`` que les crons, le statut est republié autour de
chaque passe, et une panne du canal ne coûte jamais une passe.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime

import pytest

from app.core.config import settings
from app.core.kv import KVUnavailable
from app.maintenance import control
from app.maintenance import scheduler as sched
from app.maintenance.registry import JOBS, JOBS_BY_NAME
from tests.fakes import FakeKV
from tests.maintenance_fakes import FakeSession


@pytest.fixture
def paris(monkeypatch):
    monkeypatch.setattr(settings, "MAINTENANCE_TIMEZONE", "Europe/Paris")


@pytest.fixture(autouse=True)
def fresh_process_state(monkeypatch):
    """Chaque test part d'un process vierge : aucune passe en vol, verrou neuf."""
    monkeypatch.setattr(sched, "_INFLIGHT", set())
    monkeypatch.setattr(sched, "_RUNNING", None)
    monkeypatch.setattr(sched, "_LOCK", asyncio.Lock())
    monkeypatch.setattr(sched, "_STATUS", None)


def test_scheduler_registers_one_job_per_valid_cron(paris):
    built = sched.build_scheduler()
    assert [job.id for job in built.get_jobs()] == [job.name for job in JOBS]


def test_job_ids_and_names_come_from_the_registry(paris):
    built = sched.build_scheduler()
    by_id = {job.id: job for job in built.get_jobs()}
    for job in JOBS:
        assert by_id[job.name].name == job.label


@pytest.mark.anyio
async def test_job_defaults_are_single_instance_and_coalescing(paris, monkeypatch):
    """``max_instances=1`` : jamais deux exemplaires du même job. ``coalesce`` :
    plusieurs occurrences en retard n'en déclenchent qu'une — les tâches sont
    idempotentes, les rejouer n'apporterait rien.

    Démarré **en pause** : les ``job_defaults`` ne sont appliqués aux jobs qu'au
    ``start()`` (avant, ils dorment dans ``_pending_jobs``), mais une pause
    garantit qu'aucune occurrence ne part — y compris si la suite tournait à
    03:10 pile.
    """
    monkeypatch.setattr(settings, "MAINTENANCE_MISFIRE_GRACE_SECONDS", 123)
    built = sched.build_scheduler()
    built.start(paused=True)
    try:
        jobs = built.get_jobs()
        assert len(jobs) == len(JOBS)
        for job in jobs:
            assert job.max_instances == 1
            assert job.coalesce is True
            assert job.misfire_grace_time == 123
            # Toutes les occurrences sont à venir, aucune n'est due.
            assert job.next_run_time is not None
    finally:
        built.shutdown(wait=False)


@pytest.mark.parametrize("value", ["", "   ", "off", "OFF", "none", "-"])
def test_empty_and_off_mean_unscheduled(paris, monkeypatch, value):
    """Cron vide = job NON PLANIFIÉ. À ne pas confondre avec une rétention à 0,
    qui planifie le job et le fait sortir en ``skipped``."""
    job = next(j for j in JOBS if j.name == "share_links")
    monkeypatch.setattr(settings, job.cron_setting, value)

    assert sched.build_trigger(job, UTC) is None


def test_build_trigger_rejects_an_invalid_cron_without_raising(paris, monkeypatch):
    job = next(j for j in JOBS if j.name == "share_links")
    monkeypatch.setattr(settings, job.cron_setting, "tous les jours")

    assert sched.build_trigger(job, UTC) is None


def test_an_invalid_cron_does_not_prevent_the_others(paris, monkeypatch):
    """Le test qui compte : une faute de frappe en production ne doit coûter que
    son job, jamais le démarrage du scheduler."""
    broken = next(j for j in JOBS if j.name == "s3_orphans")
    monkeypatch.setattr(settings, broken.cron_setting, "99 99 * * *")

    built = sched.build_scheduler()

    ids = [job.id for job in built.get_jobs()]
    assert "s3_orphans" not in ids
    assert len(ids) == len(JOBS) - 1


def test_timezone_is_passed_to_every_trigger(paris):
    """Le fuseau est imposé au scheduler ET à chaque déclencheur : ``tzlocal``
    n'est jamais consulté, l'absence de ``/etc/timezone`` est sans effet."""
    built = sched.build_scheduler()

    assert str(built.timezone) == "Europe/Paris"
    for job in built.get_jobs():
        assert str(job.trigger.timezone) == "Europe/Paris"


def test_unknown_timezone_falls_back_to_utc(monkeypatch):
    """Un fuseau introuvable ne fait pas tomber le process : il décale les crons
    et le dit. Le repli est ``datetime.UTC`` et non ``ZoneInfo("UTC")``, qui
    échouerait pour la même raison si la base tzdata manquait."""
    monkeypatch.setattr(settings, "MAINTENANCE_TIMEZONE", "Mars/Olympus")

    assert sched.resolve_timezone() is UTC
    assert sched.build_scheduler().get_jobs()  # il démarre quand même


def test_empty_timezone_means_utc(monkeypatch):
    monkeypatch.setattr(settings, "MAINTENANCE_TIMEZONE", "  ")
    assert str(sched.resolve_timezone()) == "UTC"


def test_scheduler_never_schedules_a_startup_pass(paris):
    """Aucun job n'est déclenché au démarrage : la répartition serait vaine si
    les neuf partaient d'un bloc au boot — et l'api applique ses migrations à
    ce moment-là précisément."""
    built = sched.build_scheduler()

    # Tant que le scheduler est à l'arrêt, rien n'a de prochaine occurrence
    # calculée, et surtout rien n'a été soumis à l'exécuteur.
    assert built.running is False
    assert not sched._INFLIGHT


# ─────────────────────────────────────────────
# Canal de contrôle : relève des demandes
# ─────────────────────────────────────────────


def pending(job_name, requested_by="u-1"):
    """Une demande déposée comme le fait l'API."""
    payload = {"requested_at": datetime.now(UTC).isoformat(), "requested_by": requested_by}
    return control.request_key(job_name), json.dumps(payload)


@pytest.fixture
def executed(monkeypatch):
    """Remplace la passe par un enregistreur : quels jobs `_spawn` a lancés."""
    ran: list[str] = []

    async def fake_execute(job):
        ran.append(job.name)

    monkeypatch.setattr(sched, "_execute", fake_execute)
    return ran


@pytest.mark.anyio
async def test_poll_does_not_even_read_redis_while_a_pass_is_in_flight(executed):
    """Une demande ne patiente jamais derrière le verrou (elle finirait sautée
    en `busy`) : elle attend son tour dans le magasin."""
    kv = FakeKV(dict([pending("share_links")]), down=True)  # toute lecture lèverait
    sched._INFLIGHT.add(object())  # une passe planifiée en vol (ou en attente du verrou)

    await sched.poll_once(kv)

    assert executed == []


@pytest.mark.anyio
async def test_poll_spawns_the_claimed_job_as_a_tracked_pass(executed):
    kv = FakeKV(dict([pending("share_links")]))

    await sched.poll_once(kv)

    # Suivie par `_INFLIGHT` comme une passe planifiée : drainée à l'arrêt.
    [task] = sched._INFLIGHT
    await task
    assert executed == ["share_links"]
    assert not kv.data  # la prise a retiré la demande


@pytest.mark.anyio
async def test_poll_with_nothing_pending_spawns_nothing(executed):
    await sched.poll_once(FakeKV())

    assert not sched._INFLIGHT
    assert executed == []


@pytest.mark.anyio
async def test_poll_republishes_a_status_lost_with_a_redis_restart(monkeypatch, executed):
    """Redis tourne sans persistance : un redémarrage efface le statut, que le
    scheduler republie au tour suivant plutôt qu'à sa prochaine passe."""
    kv = FakeKV()
    monkeypatch.setattr(sched, "_STATUS", status_context(kv))

    await sched.poll_once(kv)

    assert (await control.read_status(kv))["timezone"] == "Europe/Paris"


# ─────────────────────────────────────────────
# Canal de contrôle : statut publié autour de la passe
# ─────────────────────────────────────────────


@pytest.fixture
def pass_events(monkeypatch):
    """Trace l'ordre : statut publié, passe exécutée (état écrit), statut republié."""
    events: list = []

    async def publish_status(kv, *, started_at, timezone, running, next_runs):
        events.append(("status", running[0] if running else None))

    async def fake_run_job(job, *, db, storage=None):
        assert sched._LOCK.locked()
        events.append(("run", job.name))

    monkeypatch.setattr(sched.control, "publish_status", publish_status)
    monkeypatch.setattr(sched, "run_job", fake_run_job)
    monkeypatch.setattr(sched, "AsyncSessionLocal", lambda: FakeSession())
    return events


def status_context(kv, scheduler=None):
    return sched.StatusContext(
        kv=kv,
        scheduler=scheduler or sched.build_scheduler(),
        started_at=datetime.now(UTC),
        timezone="Europe/Paris",
    )


@pytest.mark.anyio
async def test_execute_republishes_the_status_around_the_run(monkeypatch, pass_events):
    monkeypatch.setattr(sched, "_STATUS", status_context(FakeKV()))

    await sched._execute(JOBS_BY_NAME["share_links"])

    assert pass_events == [
        ("status", "share_links"),
        ("run", "share_links"),  # `run_job` écrit l'état AVANT la republication
        ("status", None),
    ]
    assert sched._RUNNING is None
    assert not sched._LOCK.locked()


@pytest.mark.anyio
async def test_outside_the_resident_process_nothing_is_published(pass_events):
    """Sans contexte de statut (tests, one-shot), la passe tourne sans rien publier."""
    await sched._execute(JOBS_BY_NAME["share_links"])

    assert pass_events == [("run", "share_links")]


@pytest.mark.anyio
async def test_an_unreachable_redis_never_costs_the_pass(monkeypatch, caplog):
    ran = []

    async def fake_run_job(job, *, db, storage=None):
        ran.append(job.name)

    monkeypatch.setattr(sched, "run_job", fake_run_job)
    monkeypatch.setattr(sched, "AsyncSessionLocal", lambda: FakeSession())
    monkeypatch.setattr(sched, "_STATUS", status_context(FakeKV(down=True)))

    with caplog.at_level(logging.WARNING, logger=sched.logger.name):
        await sched._execute(JOBS_BY_NAME["share_links"])

    assert ran == ["share_links"]
    assert not sched._LOCK.locked()
    assert "statut du scheduler non publié" in caplog.text


@pytest.mark.anyio
async def test_the_status_carries_the_scheduler_own_plan_in_utc(paris, monkeypatch):
    kv = FakeKV()
    built = sched.build_scheduler()
    built.start(paused=True)  # calcule les prochaines occurrences, n'en déclenche aucune
    running = ("s3_orphans", datetime.now(UTC))
    monkeypatch.setattr(sched, "_RUNNING", running)
    monkeypatch.setattr(sched, "_STATUS", status_context(kv, built))
    try:
        await sched.publish_status()
    finally:
        built.shutdown(wait=False)

    status = await control.read_status(kv)
    assert status["timezone"] == "Europe/Paris"
    assert status["running_job"] == "s3_orphans"
    assert set(status["next_runs"]) == {job.name for job in JOBS}
    assert all(value.endswith("+00:00") for value in status["next_runs"].values())


# ─────────────────────────────────────────────
# Canal de contrôle : la boucle
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_failing_channel_is_logged_once_until_it_recovers(monkeypatch, caplog):
    """Redis redémarré ou pas encore prêt : une trace, puis le silence jusqu'au
    rétablissement — pas une erreur à chaque tour."""
    monkeypatch.setattr(settings, "MAINTENANCE_REQUEST_POLL_SECONDS", 0.001)
    monkeypatch.setattr(sched, "CONTROL_RETRY_SECONDS", 0.001)
    ticks = {"count": 0}

    async def poll(kv):
        ticks["count"] += 1
        if ticks["count"] <= 3:
            raise KVUnavailable("Connection refused")

    monkeypatch.setattr(sched, "poll_once", poll)
    stop = asyncio.Event()

    with caplog.at_level(logging.INFO, logger=sched.logger.name):
        loop = asyncio.create_task(sched.consume_requests(FakeKV(), stop))
        while ticks["count"] < 5:
            await asyncio.sleep(0.001)
        stop.set()
        await asyncio.wait_for(loop, timeout=1)

    assert caplog.text.count("canal de contrôle indisponible") == 1
    assert caplog.text.count("canal de contrôle rétabli") == 1


@pytest.mark.anyio
async def test_the_control_loop_stops_as_soon_as_asked(monkeypatch):
    """La boucle attend `stop` entre deux tours : l'arrêt n'attend pas la
    période de relève (le stop_grace_period du compose est de 30 s)."""
    monkeypatch.setattr(settings, "MAINTENANCE_REQUEST_POLL_SECONDS", 3600)

    async def noop(kv):
        pass

    monkeypatch.setattr(sched, "poll_once", noop)
    stop = asyncio.Event()
    loop = asyncio.create_task(sched.consume_requests(FakeKV(), stop))
    await asyncio.sleep(0)

    stop.set()
    await asyncio.wait_for(loop, timeout=1)
