"""Passe de maintenance à la main : ``python -m app.maintenance [job…]``.

One-shot : il applique une passe puis rend la main — la cadence n'est pas son
affaire, c'est celle du service ``scheduler`` du compose
(:mod:`app.maintenance.scheduler`). Sans argument, il exécute tous les jobs du
registre dans l'ordre ; avec des noms, ceux-là seulement :

    python -m app.maintenance
    python -m app.maintenance share_links s3_orphans

C'est le remplaçant explicite de la passe que l'ancienne boucle shell lançait au
démarrage du conteneur : après un déploiement,
``docker compose exec scheduler python -m app.maintenance`` vérifie tout de
suite qu'une purge passe, sans attendre son créneau.

Codes de sortie : **1** si au moins un job a échoué (un ``skipped`` n'est pas un
échec : ne rien faire reste sûr), **2** si un nom de job est inconnu.
"""

import asyncio
import logging
import sys

from app.core.database import AsyncSessionLocal, engine
from app.core.logging import configure_logging
from app.core.storage import get_storage
from app.maintenance.registry import JOBS, JOBS_BY_NAME, MaintenanceJob
from app.maintenance.runner import run_jobs
from app.maintenance.schema import wait_until_current

logger = logging.getLogger("app.maintenance")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


def select_jobs(names: list[str]) -> list[MaintenanceJob] | None:
    """Les jobs demandés, ou ``None`` si un nom est inconnu (message à l'appui)."""
    if not names:
        return list(JOBS)
    unknown = [name for name in names if name not in JOBS_BY_NAME]
    if unknown:
        print(f"job(s) inconnu(s) : {', '.join(unknown)}", file=sys.stderr)
        print(f"jobs disponibles : {', '.join(JOBS_BY_NAME)}", file=sys.stderr)
        return None
    return [JOBS_BY_NAME[name] for name in names]


async def main(argv: list[str]) -> int:
    jobs = select_jobs(argv)
    if jobs is None:
        return EXIT_USAGE

    # Construit seulement si un job demandé en a besoin : `python -m
    # app.maintenance share_links` tourne donc sans identifiants S3.
    storage = get_storage() if any(job.needs_storage for job in jobs) else None
    try:
        async with AsyncSessionLocal() as db:
            # Garde de schéma AVANT toute écriture, et **fatale** ici (le
            # scheduler, lui, la veut non fatale) : c'est une commande humaine,
            # elle doit échouer fort plutôt que travailler sur un schéma
            # inattendu — migration en cours côté api, ou rollback vers une
            # image plus ancienne. Ne rien faire est toujours sûr, cf. schema.py.
            if not await wait_until_current(db):
                return EXIT_FAILED
            report = await run_jobs(jobs, db=db, storage=storage)
    finally:
        # Même clôture que le shutdown du lifespan de l'API : le pool ne doit
        # pas retenir de connexion au-delà de la passe.
        await engine.dispose()
    logger.info("maintenance terminée — %s", report.summary())
    return EXIT_FAILED if report.failed else EXIT_OK


if __name__ == "__main__":
    # Process autonome : l'API pose la même config à l'import de app.main, lui
    # doit le faire à la main — même format, même LOG_LEVEL, donc des lignes
    # qui se lisent côte à côte dans `docker compose logs`.
    configure_logging()
    sys.exit(asyncio.run(main(sys.argv[1:])))
