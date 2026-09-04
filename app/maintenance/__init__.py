"""Purge périodique des données — job hors API.

Plusieurs tables croissent **sans borne** (compteurs de quota, messages de
l'assistant — dont les tours ``tool`` qui persistent jusqu'à 40 000 caractères
par lecture de PDF —, tentatives d'élèves, liens de partage périmés, uploads
jamais confirmés), et le bucket S3 accumule les orphelins des purges tentées
après commit. Ce package applique une **politique de rétention paramétrable**
(réglages ``PURGE_*`` de :mod:`app.core.config`, ``0`` = tâche désactivée).

Il n'expose **aucune route** : ce n'est pas une feature de l'API mais un job,
exécuté par le service ``purge`` du compose (même image, boucle shell qui lance
``python -m app.maintenance`` puis dort). Le faire tourner dans le process
uvicorn contredirait la contrainte Pi « déporter le lourd » — la réconciliation
S3 énumère tout le bucket — et le couplerait à l'uptime de l'API.
"""

from app.maintenance.service import PurgeReport, TaskResult, run_purge

__all__ = ["PurgeReport", "TaskResult", "run_purge"]
