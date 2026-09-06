"""Cours d'exemple déposé au premier onboarding d'un prof.

Visite guidée des possibilités d'écriture (formules, diagrammes, figures,
schémas, exercices, modules interactifs), livrée en **brouillon** dans « Mes
cours ». Le contenu vit dans ``manifest.json`` — le format d'échange de
:mod:`app.course_transfer` — précisément pour être régénéré par un
``GET /courses/{id}/export`` sur un cours composé dans l'UI : écrire du LaTeX
correctement échappé à la main dans du JSON est la principale source d'erreur
de ce contenu.

Invariant : **aucune ressource binaire** (blocs ``text``/``exercise``/``module``
seulement), donc aucun put S3 au seed — d'où l'absence de paramètre ``Storage``
dans tout ce module. La garde de :func:`load_manifest` est load-bearing : un
manifeste porteur de ressources insérerait des lignes ``resources`` au statut
``available`` pointant des objets S3 qui n'existent pas.
"""

import logging
import uuid
from functools import lru_cache
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http import unavailable
from app.course_transfer.importer import insert_manifest_course
from app.course_transfer.schemas import CourseManifest
from app.courses.schemas import CourseRead
from app.models.user import User

logger = logging.getLogger(__name__)

MANIFEST_PATH = Path(__file__).with_name("manifest.json")


@lru_cache(maxsize=1)
def load_manifest() -> CourseManifest:
    """Manifeste embarqué, validé et mémoïsé — lu au premier usage, pas au boot.

    Lecture synchrone assumée : quelques dizaines de Ko, une seule fois par
    process. ``lru_cache`` ne mémoïse pas les exceptions, donc un manifeste
    réparé à chaud repart sans redémarrage. Lève ``OSError`` (fichier absent
    ou illisible) ou ``ValueError`` (JSON invalide, manifeste non conforme,
    ressource binaire déclarée).
    """
    manifest = CourseManifest.model_validate_json(MANIFEST_PATH.read_bytes())
    if manifest.resources:
        raise ValueError("Le cours d'exemple ne doit porter aucune ressource binaire")
    return manifest


async def create_starter_course(db: AsyncSession, user: User) -> CourseRead:
    """Crée le cours d'exemple pour ``user`` et commit — toujours un NOUVEAU cours.

    Ordre des execute : celui d'``insert_manifest_course`` — 1) lookup
    matières par ``code``, 2) lookup niveaux, 3) insert cours (RETURNING des
    timestamps, ``visibility`` par défaut ``draft``), 4) insert
    course_subjects, 5) insert course_education_levels, 6) insert modules,
    7) insert blocks. Pas de ressource au manifeste ⇒ aucun insert
    ``resources`` et aucun appel S3.

    Un manifeste embarqué illisible est un incident serveur, pas une erreur
    de l'appelant : 503 (jamais un 500 nu).
    """
    try:
        manifest = load_manifest()
    except (OSError, ValueError) as exc:
        logger.error("Manifeste du cours d'exemple illisible", exc_info=True)
        raise unavailable("Cours d'exemple indisponible") from exc

    read, _uploads = await insert_manifest_course(db, user, manifest)
    await db.commit()
    return read


async def seed_starter_course(db: AsyncSession, user: User) -> uuid.UUID | None:
    """Dépose le cours d'exemple en **best effort** : n'échoue JAMAIS.

    Appelée par :func:`app.users.service.update_profile` après le commit de la
    première complétion d'un profil de prof : l'onboarding est déjà acquis,
    une erreur ici est journalisée puis rollbackée, jamais propagée — le prof
    garde le rattrapage manuel (``POST /courses/starter``).

    Le ``logger.warning`` est le **seul** témoin en production d'un manifeste
    cassé ou d'une rupture du contrat d'ordre des execute : ne pas le
    dégrader en ``debug``.
    """
    try:
        return (await create_starter_course(db, user)).id
    except Exception:
        logger.warning(
            "Seed du cours d'exemple impossible (user %s)", user.id, exc_info=True
        )
        try:
            await db.rollback()
        except Exception:  # session déjà morte : rien à sauver
            pass
        return None
