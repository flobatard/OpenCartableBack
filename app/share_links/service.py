"""Gestion prof des liens de partage élèves d'un cours (J2).

CRUD pur BDD (motif :mod:`app.modules.service`, sans storage) : un cours
peut porter plusieurs liens, chacun révocable individuellement (soft :
``revoked=True``, le lien reste listé côté prof — audit et anti-confusion).
La validité côté élève (expiration, révocation, visibilité du cours) est
vérifiée à l'accès dans :mod:`app.public.service`, jamais ici.

Comme partout, l'ordre des ``execute`` de chaque fonction est stable et
rejoué par une fausse session FIFO (tests/test_share_links_api.py), et tout
est scopé au propriétaire du cours (introuvable → 404, jamais 403).
"""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.course import Course
from app.models.share_link import ShareLink
from app.models.user import User
from app.share_links.schemas import ShareLinkCreate, ShareLinkRead


def _introuvable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _share_link_read(link: ShareLink) -> ShareLinkRead:
    return ShareLinkRead(
        id=link.id,
        token=link.token,
        libelle=link.libelle,
        expires_at=link.expires_at,
        revoked=link.revoked,
        created_at=link.created_at,
    )


async def _get_owned_course(db: AsyncSession, user: User, course_id: uuid.UUID) -> Course:
    """Charge le cours du prof ; 404 s'il n'existe pas ou appartient à autrui."""
    course = (
        (
            await db.execute(
                select(Course).where(Course.id == course_id, Course.owner_id == user.id)
            )
        )
        .scalars()
        .one_or_none()
    )
    if course is None:
        raise _introuvable("Cours introuvable")
    return course


async def list_share_links(
    db: AsyncSession, user: User, course_id: uuid.UUID
) -> list[ShareLinkRead]:
    """Liens du cours, du plus récent au plus ancien, révoqués inclus.

    Ordre des execute : 1) cours (contrôle de propriété), 2) liens (tri
    stable ``created_at desc, id``). Lecture seule : pas de commit.
    """
    course = await _get_owned_course(db, user, course_id)
    links = (
        (
            await db.execute(
                select(ShareLink)
                .where(ShareLink.course_id == course.id)
                .order_by(ShareLink.created_at.desc(), ShareLink.id)
            )
        )
        .scalars()
        .all()
    )
    return [_share_link_read(link) for link in links]


async def create_share_link(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: ShareLinkCreate
) -> ShareLinkRead:
    """Crée un lien de partage (token 256 bits, expiration à +SHARE_LINK_TTL_DAYS).

    Ordre des execute : 1) cours (contrôle de propriété), 2) insert du lien
    (RETURNING ``created_at`` server_default — motif ``create_course``).
    L'``expires_at`` est calculé en Python : valeur exacte connue, renvoyée
    telle quelle. Pas de bump d'``updated_at`` du cours : créer un lien ne
    change pas son contenu (seules les mutations de contenu bumpent).
    """
    course = await _get_owned_course(db, user, course_id)
    link_id = uuid.uuid4()
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=settings.SHARE_LINK_TTL_DAYS)
    created_at = (
        (
            await db.execute(
                insert(ShareLink)
                .values(
                    id=link_id,
                    course_id=course.id,
                    token=token,
                    libelle=payload.libelle,
                    expires_at=expires_at,
                    revoked=False,
                )
                .returning(ShareLink.created_at)
            )
        )
        .scalars()
        .one()
    )
    await db.commit()
    return ShareLinkRead(
        id=link_id,
        token=token,
        libelle=payload.libelle,
        expires_at=expires_at,
        revoked=False,
        created_at=created_at,
    )


async def revoke_share_link(
    db: AsyncSession, user: User, course_id: uuid.UUID, link_id: uuid.UUID
) -> None:
    """Révoque un lien (soft : ``revoked=True``, la ligne reste listée).

    Ordre des execute : 1) cours (contrôle de propriété), 2) lien (scopé
    cours) — 404 s'il n'existe pas dans ce cours. Révoquer un lien déjà
    révoqué est idempotent (204). Pas de bump d'``updated_at`` du cours.
    """
    course = await _get_owned_course(db, user, course_id)
    link = (
        (
            await db.execute(
                select(ShareLink).where(
                    ShareLink.id == link_id, ShareLink.course_id == course.id
                )
            )
        )
        .scalars()
        .one_or_none()
    )
    if link is None:
        raise _introuvable("Lien de partage introuvable")
    link.revoked = True
    await db.commit()
