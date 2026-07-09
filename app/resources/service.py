"""Bibliothèque de ressources S3 d'un cours : CRUD + flow presigned.

Flow d'upload (Descriptions.md §5.2) : la ligne ``resources`` est créée
``en_attente`` *avant* l'upload direct navigateur→S3 (presigned PUT) ; la
confirmation vérifie l'objet (HEAD S3) et passe le statut à ``disponible``.
La ressource est **indépendante des blocs** : confirmer un upload ne crée
rien d'autre, et supprimer une ressource supprime les blocs ``document`` qui
la pointent (FK ``CASCADE`` — un document sans son fichier n'a pas de sens).
Comme
:mod:`app.courses.service`, l'ordre des ``execute`` de chaque fonction est
stable et rejoué par une fausse session FIFO (tests/test_resources_api.py),
et tout est scopé au propriétaire du cours (introuvable → 404, jamais 403).
"""

import re
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.storage import Storage
from app.models.course import Course
from app.models.resource import STATUT_DISPONIBLE, STATUT_EN_ATTENTE, Resource
from app.models.user import User
from app.resources.schemas import (
    ResourceCreate,
    ResourceDownload,
    ResourcePresign,
    ResourceRead,
    ResourceUpdate,
)

# Caractères conservés dans un nom de fichier sanitizé (le reste → « _ »).
_NOM_AUTORISE = re.compile(r"[^A-Za-z0-9._-]+")


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


def _resource_read(resource: Resource) -> ResourceRead:
    return ResourceRead(
        id=resource.id,
        type=resource.type,
        nom_original=resource.nom_original,
        taille=resource.taille,
        mime=resource.mime,
        statut=resource.statut,
        created_at=resource.created_at,
        updated_at=resource.updated_at,
    )


async def _get_owned_course(db: AsyncSession, user: User, course_id: uuid.UUID) -> Course:
    """Charge le cours du prof ; 404 s'il n'existe pas ou appartient à autrui.

    L'instance ORM sert au bump d'``updated_at`` des mutations de la
    bibliothèque (la bibliothèque fait partie du cours).
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


async def list_resources(
    db: AsyncSession, user: User, course_id: uuid.UUID
) -> list[ResourceRead]:
    """Bibliothèque du cours, de la plus récente à la plus ancienne.

    Ordre des execute : 1) cours (contrôle de propriété), 2) ressources
    (tri stable ``created_at desc, id``). Les ``en_attente`` sont incluses
    (le front les affiche atténuées et permet de purger un upload raté).
    Lecture seule : pas de commit.
    """
    course = await _get_owned_course(db, user, course_id)
    resources = (
        (
            await db.execute(
                select(Resource)
                .where(Resource.course_id == course.id)
                .order_by(Resource.created_at.desc(), Resource.id)
            )
        )
        .scalars()
        .all()
    )
    return [_resource_read(r) for r in resources]


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
    s3_key = f"courses/{course_id}/resources/{resource_id}/{_sanitize_nom(payload.nom_original)}"
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
    storage: Storage,
) -> ResourceRead:
    """Vérifie l'objet S3 et passe la ressource à ``disponible``.

    Ordre des execute : 1) cours (contrôle de propriété), 2) ressource
    (scopée cours). Après 2), HEAD S3 : objet absent ou taille incohérente
    → 409 (upload non abouti), ressource déjà confirmée → 409. La ressource
    passe à ``disponible`` et le cours est « touché » par mutation ORM (pas
    d'UPDATE explicite — flush au commit). Ne crée AUCUN bloc : la ressource
    rejoint la bibliothèque, les blocs ``document`` la pointeront via
    ``BlockUpdate.resource_id``.
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

    resource.statut = STATUT_DISPONIBLE
    course.updated_at = datetime.now(UTC)
    await db.commit()
    return _resource_read(resource)


async def update_resource(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    resource_id: uuid.UUID,
    payload: ResourceUpdate,
) -> ResourceRead:
    """Renomme une ressource (nom affiché seulement, la clé S3 reste figée).

    Ordre des execute : 1) cours (contrôle de propriété), 2) ressource
    (scopée cours). Le ``Content-Disposition`` des prochains téléchargements
    suit le nouveau nom (``presign_get(s3_key, nom_original)``).
    """
    course = await _get_owned_course(db, user, course_id)
    resource = await _get_resource(db, course, resource_id)
    resource.nom_original = payload.nom_original
    course.updated_at = datetime.now(UTC)
    await db.commit()
    return _resource_read(resource)


async def delete_resource(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    resource_id: uuid.UUID,
    storage: Storage,
) -> None:
    """Supprime une ressource de la bibliothèque et son objet S3.

    Ordre des execute : 1) cours (contrôle de propriété), 2) ressource
    (scopée cours — on relit sa ``s3_key``), 3) delete. Les blocs
    ``document`` qui la pointaient partent avec elle par la FK ``CASCADE``
    (aucun execute supplémentaire). L'objet S3 (hors cascade
    DB) est supprimé APRÈS le commit — motif ``delete_course`` : un échec S3
    laisse un orphelin dans le bucket (job de réconciliation à venir),
    jamais une réf DB pointant un objet absent.
    """
    course = await _get_owned_course(db, user, course_id)
    resource = await _get_resource(db, course, resource_id)
    s3_key = resource.s3_key
    await db.execute(delete(Resource).where(Resource.id == resource.id))
    course.updated_at = datetime.now(UTC)
    await db.commit()
    await storage.delete_many([s3_key])


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
