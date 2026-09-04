"""Entrypoint du job de purge : ``python -m app.maintenance``.

One-shot : il applique une passe puis rend la main. La cadence n'est pas son
affaire — c'est la boucle shell du service ``purge`` du compose qui le rappelle
(rien de résident entre deux passes : le process Python sort, il ne dort pas).
Lançable aussi à la main, en local depuis la racine du back ou dans le
conteneur, pour vérifier une politique avant de l'activer.

Sort en **code 1** si au moins une tâche a échoué : la boucle du conteneur
continue (une purge ratée n'est pas une raison d'arrêter le service), mais
l'échec est visible dans les logs et dans le code de sortie d'un appel manuel.
"""

import asyncio
import logging
import sys

from app.core.database import AsyncSessionLocal, engine
from app.core.storage import get_storage
from app.maintenance.schema import wait_until_current
from app.maintenance.service import run_purge

logger = logging.getLogger("app.maintenance")


async def main() -> int:
    storage = get_storage()
    try:
        async with AsyncSessionLocal() as db:
            # Garde de schéma AVANT toute écriture : purger contre une base qui
            # n'est pas à la révision de cette image n'est jamais souhaitable
            # (migration en cours côté api, ou rollback vers une image plus
            # ancienne). Ne rien faire est toujours sûr — cf. schema.py.
            if not await wait_until_current(db):
                return 1
            report = await run_purge(db, storage)
    finally:
        # Même clôture que le shutdown du lifespan de l'API : le pool ne doit
        # pas retenir de connexion au-delà de la passe.
        await engine.dispose()
    logger.info("purge terminée — %s", report.summary())
    return 1 if report.failed else 0


if __name__ == "__main__":
    # Process autonome : il pose sa propre config de log (l'API n'en a aucune).
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s",
    )
    sys.exit(asyncio.run(main()))
