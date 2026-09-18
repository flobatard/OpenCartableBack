"""Maintenance périodique des données — jobs hors API.

Plusieurs tables croissent **sans borne** (compteurs de quota, messages de
l'assistant — dont les tours ``tool`` qui persistent jusqu'à 40 000 caractères
par lecture de PDF —, tentatives d'élèves, liens de partage périmés, uploads
jamais confirmés), et le bucket S3 accumule les orphelins des purges tentées
après commit. Ce paquet applique une **politique de rétention paramétrable**
(réglages ``PURGE_*`` de :mod:`app.core.config`) et **observe** ce qu'il ne
corrige pas (volumétrie, objets S3 manquants).

Il n'expose **aucune route** : ce ne sont pas des features de l'API mais des
jobs, déclenchés par le service ``scheduler`` du compose
(:mod:`app.maintenance.scheduler`, un ``AsyncIOScheduler`` résident, une
expression cron par job). Les faire tourner dans le process uvicorn
contredirait la contrainte Pi « déporter le lourd » — la réconciliation S3
énumère tout le bucket — et les coupleraient à l'uptime de l'API.

Deux **leviers d'inactivité** coexistent et ne veulent pas dire la même chose :

- **rétention à 0 = tâche désactivée** : le job reste planifié, sort avant le
  moindre ``execute`` et s'enregistre en ``skipped`` ;
- **cron vide (ou invalide) = job non planifié** : rien ne le déclenche, il
  n'écrit aucune ligne d'état.

Carte du paquet : ``registry`` la liste des jobs · ``service`` les sept purges ·
``checks`` les deux contrôles en lecture seule · ``runner`` l'exécution d'un job
· ``state`` l'état en base · ``schema`` la garde Alembic · ``scheduler`` le
process résident · ``__main__`` la passe à la main.
"""

from app.maintenance.registry import JOBS, JOBS_BY_NAME, MaintenanceJob
from app.maintenance.results import JobOutcome, JobResult, MaintenanceReport
from app.maintenance.runner import run_job, run_jobs

__all__ = [
    "JOBS",
    "JOBS_BY_NAME",
    "JobOutcome",
    "JobResult",
    "MaintenanceJob",
    "MaintenanceReport",
    "run_job",
    "run_jobs",
]
