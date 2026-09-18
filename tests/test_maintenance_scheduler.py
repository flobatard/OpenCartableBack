"""Tests du câblage APScheduler (:mod:`app.maintenance.scheduler`).

**Aucun scheduler n'est démarré** : on construit, on inspecte, on jette. Les
jobs ajoutés avant ``start()`` sont bien rendus par ``get_jobs()`` (ils vivent
dans ``_pending_jobs`` tant que le scheduler est à l'arrêt), ce qui suffit à
vérifier tout ce qui compte ici : qui est planifié, avec quelles options, sous
quel fuseau.

Ce qu'on protège avant tout : **une configuration cassée ne doit jamais coûter
toute la maintenance**. Une expression cron fautive dans un YAML de production
laisse le scheduler démarrer, et seul le job concerné reste au tapis.
"""

from datetime import UTC

import pytest

from app.core.config import settings
from app.maintenance import scheduler as sched
from app.maintenance.registry import JOBS


@pytest.fixture
def paris(monkeypatch):
    monkeypatch.setattr(settings, "MAINTENANCE_TIMEZONE", "Europe/Paris")


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
