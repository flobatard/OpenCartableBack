"""Autorisation du régime d'accès élève : visibilité du cours + token de partage.

C'est la **seconde dépendance d'autorisation** de l'API (Descriptions.md §5.1),
distincte de ``get_current_user`` : aucune identité, aucun JWT, aucun appel
Zitadel — un token de partage opaque (capability URL) et la ``visibility``
du cours décident seuls de l'accès, vérifiés À CHAQUE requête :

- cours ``public``  → accessible sans token (le token, s'il est fourni,
  est ignoré) ;
- cours ``draft``   → 404 toujours, même avec un lien valide (les liens
  sont suspendus, pas supprimés) ;
- cours ``private`` → token requis, lié à CE cours, non révoqué, non expiré.

Sémantique d'erreur : **404 uniformément** (token inconnu/révoqué/expiré,
cours introuvable/en cours de rédaction) — un 410 serait un oracle
confirmant qu'un lien a existé, incohérent avec la doctrine du repo
(« introuvable, jamais interdit »). L'expiration est comparée EN PYTHON
(pas en SQL) pour rester testable sur une fausse session FIFO.

Réutilisé tel quel par le tuteur d'exercice (:mod:`app.student_exercises`),
dont les routes portent un JWT mais accèdent au cours par ce régime.
"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http import not_found
from app.models.course import VISIBILITY_DRAFT, VISIBILITY_PUBLIC, Course
from app.models.share_link import ShareLink


def course_not_found() -> HTTPException:
    """Le 404 uniforme du régime public — détail unique et volontairement vague :
    ne jamais distinguer « cours inexistant », « lien révoqué », « lien
    expiré », « cours dépublié »."""
    return not_found("Cours introuvable")


def _link_valid(link: ShareLink | None) -> bool:
    """Un lien ouvre l'accès s'il existe, n'est pas révoqué et n'a pas expiré."""
    return (
        link is not None
        and not link.revoked
        and link.expires_at > datetime.now(UTC)
    )


async def get_public_course(
    db: AsyncSession, course_id: uuid.UUID, token: str | None
) -> Course:
    """Autorise l'accès élève à un cours désigné par son id (voir module).

    Ordre des execute : 1) cours ; puis UNIQUEMENT si le cours est
    ``private`` et qu'un token est fourni : 2) lien (scopé à CE cours —
    un token du cours A n'ouvre jamais le cours B).
    """
    course = (
        (await db.execute(select(Course).where(Course.id == course_id)))
        .scalars()
        .one_or_none()
    )
    if course is None or course.visibility == VISIBILITY_DRAFT:
        raise course_not_found()
    if course.visibility == VISIBILITY_PUBLIC:
        return course
    if token is None:
        raise course_not_found()
    link = (
        (
            await db.execute(
                select(ShareLink).where(
                    ShareLink.course_id == course.id, ShareLink.token == token
                )
            )
        )
        .scalars()
        .one_or_none()
    )
    if not _link_valid(link):
        raise course_not_found()
    return course


async def get_course_for_token(db: AsyncSession, token: str) -> Course:
    """Résout un lien de partage vers son cours (entrée ``/shared/{token}``).

    Ordre des execute : 1) lien par token, 2) cours du lien. Un lien valide
    sur un cours ``public`` reste valide (le prof a élargi l'accès) ; un
    cours ``draft`` est introuvable même avec un lien valide.
    """
    link = (
        (await db.execute(select(ShareLink).where(ShareLink.token == token)))
        .scalars()
        .one_or_none()
    )
    if not _link_valid(link):
        raise course_not_found()
    course = (
        (await db.execute(select(Course).where(Course.id == link.course_id)))
        .scalars()
        .one_or_none()
    )
    if course is None or course.visibility == VISIBILITY_DRAFT:
        raise course_not_found()
    return course
