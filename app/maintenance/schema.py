"""Garde de schéma : le job ne purge que si la base est à la révision attendue.

Pourquoi ce n'est pas un problème d'orchestration. Le ``depends_on`` du compose
ordonne le **premier** démarrage, et rien d'autre :

- au redémarrage du démon Docker (reboot du Pi), les conteneurs
  ``restart: unless-stopped`` repartent **sans** réévaluation des dépendances ;
- surtout, le conteneur de purge vit des **semaines** : un déploiement au
  troisième jour recrée l'api avec une nouvelle migration pendant que la boucle
  dort. Aucune règle de démarrage ne peut couvrir ça.

D'où le renversement : au lieu d'ordonner, on **vérifie**. Chaque passe compare
la révision inscrite en base (``alembic_version``) à la tête du dossier
``alembic/`` **de cette image** ; tant qu'elles diffèrent, le job attend, puis
renonce à la passe. Conséquence utile : après un rollback vers une image plus
ancienne, la purge s'abstient aussi (la base est en avance sur son code).
"""

import asyncio
import logging
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# app/maintenance/schema.py -> racine du projet (motif _CONFIG_DIR de config.py) :
# résolu depuis __file__, donc indépendant du cwd du conteneur.
ALEMBIC_DIR = Path(__file__).resolve().parent.parent.parent / "alembic"

# Une migration sur ce volume de données se compte en secondes ; 5 minutes
# couvrent très large un démarrage d'api qui applique un lot de migrations.
SCHEMA_POLL_SECONDS = 5.0
SCHEMA_WAIT_SECONDS = 300.0


def expected_head() -> str | None:
    """Révision de tête du dossier ``alembic/`` embarqué dans cette image.

    ``Config()` est construite en mémoire plutôt que lue depuis ``alembic.ini``
    (dont le ``script_location`` est relatif au cwd) : on impose un chemin
    absolu. ``None`` si la tête est indéterminable — dossier absent, ou
    plusieurs têtes.
    """
    try:
        config = Config()
        config.set_main_option("script_location", str(ALEMBIC_DIR))
        return ScriptDirectory.from_config(config).get_current_head()
    except Exception:  # noqa: BLE001 — indéterminable = on ne purgera pas
        logger.exception("révision de tête Alembic indéterminable (%s)", ALEMBIC_DIR)
        return None


async def current_revision(db: AsyncSession) -> str | None:
    """Révision inscrite en base, ou ``None`` si la table n'existe pas encore.

    Une base jamais migrée n'a pas d'``alembic_version`` : l'erreur avorte la
    transaction, d'où le ``rollback`` avant de rendre la main.
    """
    try:
        result = await db.execute(text("SELECT version_num FROM alembic_version"))
    except SQLAlchemyError:
        await db.rollback()
        return None
    return result.scalars().first()


async def wait_until_current(
    db: AsyncSession,
    *,
    timeout: float = SCHEMA_WAIT_SECONDS,
    poll: float = SCHEMA_POLL_SECONDS,
) -> bool:
    """Attend que la base soit à la révision attendue par cette image.

    ``True`` dès qu'elles coïncident. ``False`` si la tête est indéterminable
    (échec immédiat, rien à attendre) ou au bout de ``timeout`` — l'appelant
    renonce alors à la passe : ne rien purger est toujours sûr, purger contre un
    schéma inattendu ne l'est pas.
    """
    head = expected_head()
    if head is None:
        return False

    waited = 0.0
    while True:
        revision = await current_revision(db)
        if revision == head:
            if waited:
                logger.info("schéma à jour après %.0f s d'attente (%s)", waited, head)
            return True
        if waited >= timeout:
            logger.error(
                "schéma pas à la révision attendue après %.0f s "
                "(base=%s, image=%s) — passe abandonnée",
                waited,
                revision or "aucune",
                head,
            )
            return False
        if not waited:
            logger.info(
                "schéma en attente de migration (base=%s, image=%s)…",
                revision or "aucune",
                head,
            )
        await asyncio.sleep(poll)
        waited += poll
