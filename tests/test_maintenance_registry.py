"""Tests du registre des jobs (:mod:`app.maintenance.registry`).

Le registre est la seule liste de jobs, et il est *stringly-typed* par nature :
il désigne ses réglages par leur nom. Ces tests sont la contrepartie — ils
transforment en échec de suite ce qui serait sinon une faute de frappe découverte
à 3 h du matin, en production.

Deux d'entre eux encodent des pièges d'APScheduler documentés dans
:mod:`app.maintenance.scheduler` : la numérotation des jours de semaine et
l'heure qui n'existe pas au passage à l'heure d'été.
"""

import re

import pytest
from apscheduler.triggers.cron import CronTrigger

from app.core.config import Settings, settings
from app.maintenance.registry import JOBS, JOBS_BY_NAME, cron_for, default_cron

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# 5ᵉ champ d'un cron : `*`, ou des noms de jours — jamais un chiffre.
DAY_OF_WEEK_PATTERN = re.compile(r"^(\*|[a-z]{3}(-[a-z]{3})?(,[a-z]{3}(-[a-z]{3})?)*)$")


def test_job_names_are_unique():
    assert len(JOBS_BY_NAME) == len(JOBS)


@pytest.mark.parametrize("job", JOBS, ids=lambda job: job.name)
def test_job_names_are_english_identifiers(job):
    """Règle du projet : les identifiants sont en anglais, ASCII, snake_case.

    Ce n'est pas cosmétique — ces noms sont une clé primaire en base et un id de
    job APScheduler.
    """
    assert NAME_PATTERN.match(job.name), job.name
    assert job.name.isascii()


@pytest.mark.parametrize("job", JOBS, ids=lambda job: job.name)
def test_job_labels_are_french_prose(job):
    """Le libellé est pour les logs : de la prose, jamais un second identifiant."""
    assert " " in job.label
    assert "_" not in job.label
    assert job.label != job.name


@pytest.mark.parametrize("job", JOBS, ids=lambda job: job.name)
def test_settings_named_by_the_registry_exist(job):
    """Parade au stringly-typed : chaque réglage cité existe vraiment."""
    assert job.cron_setting in Settings.model_fields
    if job.retention_setting is not None:
        assert job.retention_setting in Settings.model_fields


@pytest.mark.parametrize("job", JOBS, ids=lambda job: job.name)
def test_default_crons_parse(job):
    CronTrigger.from_crontab(default_cron(job))


@pytest.mark.parametrize("job", JOBS, ids=lambda job: job.name)
def test_day_of_week_is_written_with_names(job):
    """``from_crontab`` indexe **0 = lundi**, crontab **0 = dimanche**.

    Un ``0 1 * * 0`` copié d'un crontab Unix tomberait un jour à côté,
    silencieusement, pendant des mois. On n'écrit donc jamais de chiffre dans
    le 5ᵉ champ.
    """
    day_of_week = default_cron(job).split()[4]
    assert DAY_OF_WEEK_PATTERN.match(day_of_week), day_of_week


@pytest.mark.parametrize("job", JOBS, ids=lambda job: job.name)
def test_default_crons_avoid_the_dst_hour(job):
    """02:00–02:59 n'existe pas la nuit du passage à l'heure d'été (et arrive
    deux fois à l'automne) : aucune occurrence par défaut n'y tombe."""
    assert default_cron(job).split()[1] != "2"


def test_default_crons_do_not_collide():
    """Une tâche par créneau : chaque job forme son propre bloc dans les logs,
    et le verrou de maintenance n'est jamais contendu en nominal."""
    slots = [tuple(default_cron(job).split()[:2]) for job in JOBS]
    assert len(set(slots)) == len(slots)


def test_jobs_needing_storage_are_declared():
    """Un job qui oublie ``needs_storage`` recevrait ``None`` à la place du
    client S3 — et tomberait à sa première passe, la nuit."""
    assert {job.name for job in JOBS if job.needs_storage} == {
        "pending_resources",
        "s3_orphans",
        "storage_inventory",
        "missing_s3_objects",
    }


def test_only_the_rotating_check_reads_its_previous_detail():
    assert {job.name for job in JOBS if job.needs_previous_detail} == {
        "missing_s3_objects"
    }


def test_read_only_checks_have_no_retention():
    """Un contrôle n'a rien à purger : lui donner une rétention le rendrait
    désactivable par un réglage qui ne le concerne pas."""
    assert {job.name for job in JOBS if job.retention_setting is None} == {
        "storage_inventory",
        "missing_s3_objects",
    }


def test_cron_for_reads_the_live_setting(monkeypatch):
    """La cadence est relue dans les settings (et détourée), pas figée au bind.

    ``default_cron`` va la chercher ailleurs — dans le défaut du champ — ce qui
    la rend insensible à ce que l'opérateur a décommenté dans son YAML : c'est
    ce qui permet aux tests de la grille horaire d'être fiables.
    """
    job = JOBS_BY_NAME["share_links"]
    monkeypatch.setattr(settings, job.cron_setting, "  15 5 * * *  ")
    assert cron_for(job) == "15 5 * * *"
    assert default_cron(job) != "15 5 * * *"
