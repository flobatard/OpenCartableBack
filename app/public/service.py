"""Régime d'accès élève (J2) : autorisation par visibilité + token de partage.

C'est la **seconde dépendance d'autorisation** actée au cadrage (§5.1),
distincte de ``get_current_user`` : aucune identité, aucun JWT, aucun appel
Zitadel — un token de partage opaque (capability URL) et la ``visibilite``
du cours décident seuls de l'accès, vérifiés À CHAQUE requête :

- cours ``public``   → accessible sans token (le token, s'il est fourni,
  est ignoré) ;
- cours ``en_cours`` → 404 toujours, même avec un lien valide (les liens
  sont suspendus, pas supprimés) ;
- cours ``prive``    → token requis, lié à CE cours, non révoqué, non expiré.

Sémantique d'erreur : **404 uniformément** (token inconnu/révoqué/expiré,
cours introuvable/en cours de rédaction) — un 410 serait un oracle
confirmant qu'un lien a existé, incohérent avec la doctrine du repo
(« introuvable, jamais interdit »). Seule exception : presign d'une
ressource ``en_attente`` → 409, miroir exact du régime prof (on est alors
déjà autorisé sur le cours, aucune fuite).

Tout est en lecture seule : aucune fonction ne commit. L'ordre des
``execute`` de chaque fonction est stable et rejoué par une fausse session
FIFO (tests/test_public_api.py) ; l'expiration est comparée EN PYTHON
(pas en SQL) pour rester testable ainsi.
"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.storage import Storage
from app.models.block import TYPE_EXERCICE, Block
from app.models.course import (
    VISIBILITE_EN_COURS,
    VISIBILITE_PUBLIC,
    Course,
    course_education_levels,
    course_subjects,
)
from app.models.education_level import EducationLevel
from app.models.module import Module
from app.models.resource import STATUT_DISPONIBLE, Resource
from app.models.share_link import ShareLink
from app.models.subject import Subject
from app.models.user import User
from app.public.schemas import (
    PublicBlockRead,
    PublicCourseDetailRead,
    PublicCourseRead,
    PublicDownloadRead,
    PublicModuleRead,
    PublicProfessorRead,
    PublicResourceRead,
)


def _introuvable() -> HTTPException:
    # Détail unique et volontairement vague : ne pas distinguer « cours
    # inexistant », « lien révoqué », « lien expiré », « cours dépublié ».
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Cours introuvable"
    )


def _lien_valide(link: ShareLink | None) -> bool:
    """Un lien ouvre l'accès s'il existe, n'est pas révoqué et n'a pas expiré."""
    return (
        link is not None
        and not link.revoked
        and link.expires_at > datetime.now(UTC)
    )


def _content_public(type_: str, content: dict) -> dict:
    """Content JSONB servi aux élèves — NOUVEAU dict, jamais l'original.

    Pour un bloc ``exercice``, reconstruction explicite sans les
    ``reponse_attendue`` (corrigé du prof, jamais servi — contrat de
    block.py). Les autres types sont copiés tels quels.
    """
    if type_ != TYPE_EXERCICE:
        return dict(content)
    return {
        "enonce": content.get("enonce", ""),
        "questions": [
            {"id": q.get("id"), "enonce": q.get("enonce", ""), "type": q.get("type")}
            for q in content.get("questions", [])
            if isinstance(q, dict)
        ],
    }


def _block_read(block: Block) -> PublicBlockRead:
    return PublicBlockRead(
        id=block.id,
        position=block.position,
        type=block.type,
        titre=block.titre,
        description=block.description,
        content=_content_public(block.type, block.content),
        resource_id=block.resource_id,
        module_id=block.module_id,
    )


def _resource_read(resource: Resource) -> PublicResourceRead:
    return PublicResourceRead(
        id=resource.id,
        type=resource.type,
        nom_original=resource.nom_original,
        taille=resource.taille,
        mime=resource.mime,
    )


def _course_read(
    course: Course, subjects: list[str], education_levels: list[str], block_count: int
) -> PublicCourseRead:
    return PublicCourseRead(
        id=course.id,
        titre=course.titre,
        description=course.description,
        subjects=subjects,
        education_levels=education_levels,
        block_count=block_count,
        preview_settings=course.preview_settings,
        updated_at=course.updated_at,
    )


async def get_public_course(
    db: AsyncSession, course_id: uuid.UUID, token: str | None
) -> Course:
    """Autorise l'accès élève à un cours désigné par son id (voir module).

    Ordre des execute : 1) cours ; puis UNIQUEMENT si le cours est
    ``prive`` et qu'un token est fourni : 2) lien (scopé à CE cours —
    un token du cours A n'ouvre jamais le cours B).
    """
    course = (
        (await db.execute(select(Course).where(Course.id == course_id)))
        .scalars()
        .one_or_none()
    )
    if course is None or course.visibilite == VISIBILITE_EN_COURS:
        raise _introuvable()
    if course.visibilite == VISIBILITE_PUBLIC:
        return course
    if token is None:
        raise _introuvable()
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
    if not _lien_valide(link):
        raise _introuvable()
    return course


async def get_course_for_token(db: AsyncSession, token: str) -> Course:
    """Résout un lien de partage vers son cours (entrée ``/shared/{token}``).

    Ordre des execute : 1) lien par token, 2) cours du lien. Un lien valide
    sur un cours ``public`` reste valide (le prof a élargi l'accès) ; un
    cours ``en_cours`` est introuvable même avec un lien valide.
    """
    link = (
        (await db.execute(select(ShareLink).where(ShareLink.token == token)))
        .scalars()
        .one_or_none()
    )
    if not _lien_valide(link):
        raise _introuvable()
    course = (
        (await db.execute(select(Course).where(Course.id == link.course_id)))
        .scalars()
        .one_or_none()
    )
    if course is None or course.visibilite == VISIBILITE_EN_COURS:
        raise _introuvable()
    return course


async def course_detail_public(db: AsyncSession, course: Course) -> PublicCourseDetailRead:
    """Détail complet filtré d'un cours déjà autorisé.

    Ordre des execute : 1) noms de matières, 2) noms de niveaux, 3) blocs
    (tri stable ``position, id``), 4) ressources ``disponible`` (tri
    ``created_at desc, id``, miroir de la liste prof).
    """
    subjects = list(
        (
            await db.execute(
                select(Subject.nom)
                .select_from(
                    course_subjects.join(
                        Subject, Subject.id == course_subjects.c.subject_id
                    )
                )
                .where(course_subjects.c.course_id == course.id)
                .order_by(Subject.nom)
            )
        )
        .scalars()
        .all()
    )
    education_levels = list(
        (
            await db.execute(
                select(EducationLevel.nom)
                .select_from(
                    course_education_levels.join(
                        EducationLevel,
                        EducationLevel.id
                        == course_education_levels.c.education_level_id,
                    )
                )
                .where(course_education_levels.c.course_id == course.id)
                .order_by(EducationLevel.nom)
            )
        )
        .scalars()
        .all()
    )
    blocks = (
        (
            await db.execute(
                select(Block)
                .where(Block.course_id == course.id)
                .order_by(Block.position, Block.id)
            )
        )
        .scalars()
        .all()
    )
    resources = (
        (
            await db.execute(
                select(Resource)
                .where(
                    Resource.course_id == course.id,
                    Resource.statut == STATUT_DISPONIBLE,
                )
                .order_by(Resource.created_at.desc(), Resource.id)
            )
        )
        .scalars()
        .all()
    )
    base = _course_read(course, subjects, education_levels, len(blocks))
    return PublicCourseDetailRead(
        **base.model_dump(),
        blocks=[_block_read(b) for b in blocks],
        resources=[_resource_read(r) for r in resources],
    )


async def presign_download_public(
    db: AsyncSession,
    course: Course,
    resource_id: uuid.UUID,
    storage: Storage,
    *,
    inline: bool = False,
) -> PublicDownloadRead:
    """URL présignée de lecture d'une ressource d'un cours déjà autorisé.

    Ordre des execute : 1) ressource (scopée cours). 409 si la ressource
    n'est pas ``disponible`` (miroir exact du régime prof). Le bucket n'est
    jamais public : c'est la présignature qui matérialise l'accès (§5.6).
    """
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
        raise _introuvable()
    if resource.statut != STATUT_DISPONIBLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ressource non disponible (upload non confirmé)",
        )
    download_url = storage.presign_get(
        resource.s3_key, resource.nom_original, inline=inline
    )
    return PublicDownloadRead(
        download_url=download_url, expires_in=settings.S3_PRESIGN_GET_TTL
    )


async def get_module_public(
    db: AsyncSession, course: Course, module_id: uuid.UUID
) -> PublicModuleRead:
    """Code d'un module d'un cours déjà autorisé (exécution sandbox front).

    Ordre des execute : 1) module (scopé cours).
    """
    module = (
        (
            await db.execute(
                select(Module).where(
                    Module.id == module_id, Module.course_id == course.id
                )
            )
        )
        .scalars()
        .one_or_none()
    )
    if module is None:
        raise _introuvable()
    return PublicModuleRead(
        id=module.id, titre=module.titre, html=module.html, css=module.css, js=module.js
    )


async def list_public_courses_by_professor(
    db: AsyncSession, user_id: uuid.UUID
) -> PublicProfessorRead:
    """Catalogue public d'un prof : ses cours ``public``, du plus récent au
    plus ancien.

    Ordre des execute : 1) utilisateur, 2) cours publics ; puis, s'il y en
    a : 3) noms de matières, 4) noms de niveaux, 5) comptes de blocs.
    Utilisateur inconnu ou sans cours public → même réponse (liste vide,
    ``nom_public`` éventuel) : pas d'oracle d'existence d'un compte.
    """
    user = (
        (await db.execute(select(User).where(User.id == user_id)))
        .scalars()
        .one_or_none()
    )
    courses = (
        (
            await db.execute(
                select(Course)
                .where(
                    Course.owner_id == user_id,
                    Course.visibilite == VISIBILITE_PUBLIC,
                )
                .order_by(Course.updated_at.desc(), Course.id)
            )
        )
        .scalars()
        .all()
    )
    nom_public = user.nom_public if user is not None else None
    if not courses:
        return PublicProfessorRead(nom_public=nom_public, courses=[])
    course_ids = [c.id for c in courses]

    matieres: dict[uuid.UUID, list[str]] = {c.id: [] for c in courses}
    lignes_matieres = (
        await db.execute(
            select(course_subjects.c.course_id, Subject.nom)
            .select_from(
                course_subjects.join(Subject, Subject.id == course_subjects.c.subject_id)
            )
            .where(course_subjects.c.course_id.in_(course_ids))
            .order_by(course_subjects.c.course_id, Subject.nom)
        )
    ).all()
    for course_id, nom in lignes_matieres:
        matieres[course_id].append(nom)

    niveaux: dict[uuid.UUID, list[str]] = {c.id: [] for c in courses}
    lignes_niveaux = (
        await db.execute(
            select(course_education_levels.c.course_id, EducationLevel.nom)
            .select_from(
                course_education_levels.join(
                    EducationLevel,
                    EducationLevel.id == course_education_levels.c.education_level_id,
                )
            )
            .where(course_education_levels.c.course_id.in_(course_ids))
            .order_by(course_education_levels.c.course_id, EducationLevel.nom)
        )
    ).all()
    for course_id, nom in lignes_niveaux:
        niveaux[course_id].append(nom)

    comptes = dict(
        (
            await db.execute(
                select(Block.course_id, func.count())
                .where(Block.course_id.in_(course_ids))
                .group_by(Block.course_id)
            )
        ).all()
    )
    return PublicProfessorRead(
        nom_public=nom_public,
        courses=[
            _course_read(c, matieres[c.id], niveaux[c.id], comptes.get(c.id, 0))
            for c in courses
        ],
    )
