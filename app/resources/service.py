"""Upload de ressources S3 : presign, confirmation, lecture.

Flow (Descriptions.md §5.2) : la ligne ``resources`` est créée ``en_attente``
*avant* l'upload direct navigateur→S3 (presigned PUT) ; une confirmation vérifie
l'objet (HEAD S3), passe le statut à ``disponible`` et matérialise le bloc
``ressource`` du cours. Comme :mod:`app.courses.service`, l'ordre des ``execute``
de chaque fonction est stable et rejoué par une fausse session FIFO
(tests/test_resources_api.py), et tout est scopé au propriétaire du cours
(introuvable → 404, jamais 403).
"""

import re
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.storage import Storage
from app.courses.schemas import BlockRead
from app.models.block import TYPE_RESSOURCE, Block
from app.models.course import Course
from app.models.resource import STATUT_DISPONIBLE, STATUT_EN_ATTENTE, Resource
from app.models.user import User
from app.resources.schemas import (
    ResourceConfirm,
    ResourceCreate,
    ResourceDownload,
    ResourcePresign,
)

# Caractères conservés dans un nom de fichier sanitizé (le reste → « _ »).
_NOM_AUTORISE = re.compile(r"[^A-Za-z0-9._-]+")


def _invalide(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _introuvable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _conflit(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _sanitize_nom(nom: str) -> str:
    """Nom de fichier sûr pour une clé S3 : basename, chars restreints, borné.

    Neutralise toute tentative de traversée de chemin (``/``, ``\\``) et borne
    la longueur ; la partie ``uuid/`` de la clé garantit déjà l'unicité.
    """
    base = nom.replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _NOM_AUTORISE.sub("_", base).strip("._")
    return (base or "fichier")[:200]


async def _get_owned_course(db: AsyncSession, user: User, course_id: uuid.UUID) -> Course:
    """Charge le cours du prof ; 404 s'il n'existe pas ou appartient à autrui.

    L'instance ORM sert au bump d'``updated_at`` lorsqu'une ressource matérialise
    un bloc.
    """
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


async def _get_resource(
    db: AsyncSession, course: Course, resource_id: uuid.UUID
) -> Resource:
    """Charge une ressource scopée à ce cours ; 404 sinon."""
    resource = (
        (
            await db.execute(
                select(Resource).where(
                    Resource.id == resource_id, Resource.course_id == course.id
                )
            )
        )
        .scalars()
        .one_or_none()
    )
    if resource is None:
        raise _introuvable("Ressource introuvable")
    return resource


async def presign_upload(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    payload: ResourceCreate,
    storage: Storage,
) -> ResourcePresign:
    """Crée la ressource ``en_attente`` et renvoie l'URL présignée d'upload.

    Ordre des execute : 1) cours (contrôle de propriété), 2) insert ressource.
    L'URL présignée PUT est du calcul local (pas d'execute). Clé S3 plate
    ``<resource_id>/<nom-sanitizé>`` (l'uuid garantit l'unicité).
    """
    course = await _get_owned_course(db, user, course_id)
    resource_id = uuid.uuid4()
    s3_key = f"{resource_id}/{_sanitize_nom(payload.nom_original)}"
    await db.execute(
        insert(Resource).values(
            id=resource_id,
            course_id=course.id,
            type=payload.type,
            s3_key=s3_key,
            nom_original=payload.nom_original,
            taille=payload.taille,
            mime=payload.mime,
            statut=STATUT_EN_ATTENTE,
        )
    )
    await db.commit()
    upload_url = storage.presign_put(s3_key, payload.mime)
    return ResourcePresign(
        resource_id=resource_id,
        s3_key=s3_key,
        upload_url=upload_url,
        statut=STATUT_EN_ATTENTE,
        expires_in=settings.S3_PRESIGN_PUT_TTL,
    )


async def confirm_upload(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    resource_id: uuid.UUID,
    payload: ResourceConfirm,
    storage: Storage,
) -> BlockRead:
    """Vérifie l'objet S3, passe la ressource à ``disponible`` et crée le bloc.

    Ordre des execute : 1) cours (contrôle de propriété), 2) ressource (scopée
    cours), 3) position suivante (max+1), 4) insert du bloc ``ressource``.
    Entre 2) et 3), HEAD S3 : objet absent ou taille incohérente → 409 (upload
    non abouti), ressource déjà confirmée → 409. La ressource passe à
    ``disponible`` et le cours est « touché » par mutation ORM (pas d'UPDATE
    explicite — flush au commit).
    """
    course = await _get_owned_course(db, user, course_id)
    resource = await _get_resource(db, course, resource_id)
    if resource.statut == STATUT_DISPONIBLE:
        raise _conflit("Ressource déjà confirmée")

    metadata = await storage.head(resource.s3_key)
    if metadata is None:
        raise _conflit("Objet introuvable sur S3 : upload non abouti")
    taille_reelle = metadata.get("ContentLength")
    if taille_reelle is not None and taille_reelle != resource.taille:
        raise _conflit(
            f"Taille incohérente (déclarée {resource.taille}, réelle {taille_reelle})"
        )

    position = (
        (
            await db.execute(
                select(func.coalesce(func.max(Block.position) + 1, 0)).where(
                    Block.course_id == course.id
                )
            )
        )
        .scalars()
        .one()
    )
    block_id = uuid.uuid4()
    content = {"legende": payload.legende or "", "affichage": payload.affichage}
    await db.execute(
        insert(Block).values(
            id=block_id,
            course_id=course.id,
            position=position,
            type=TYPE_RESSOURCE,
            titre=payload.titre,
            description=payload.description,
            content=content,
            resource_id=resource.id,
        )
    )
    resource.statut = STATUT_DISPONIBLE
    course.updated_at = datetime.now(UTC)
    await db.commit()
    return BlockRead(
        id=block_id,
        position=position,
        type=TYPE_RESSOURCE,
        titre=payload.titre,
        description=payload.description,
        content=content,
        resource_id=resource.id,
    )


async def presign_download(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    resource_id: uuid.UUID,
    storage: Storage,
) -> ResourceDownload:
    """URL présignée de lecture ; 409 tant que la ressource n'est pas disponible.

    Ordre des execute : 1) cours (contrôle de propriété), 2) ressource (scopée
    cours). Lecture seule : pas de commit.
    """
    course = await _get_owned_course(db, user, course_id)
    resource = await _get_resource(db, course, resource_id)
    if resource.statut != STATUT_DISPONIBLE:
        raise _conflit("Ressource non disponible (upload non confirmé)")
    download_url = storage.presign_get(resource.s3_key, resource.nom_original)
    return ResourceDownload(
        download_url=download_url, expires_in=settings.S3_PRESIGN_GET_TTL
    )
