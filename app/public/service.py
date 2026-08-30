"""Régime d'accès élève (J2) : autorisation par visibilité + token de partage.

C'est la **seconde dépendance d'autorisation** actée au cadrage (§5.1),
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
(« introuvable, jamais interdit »). Seule exception : presign d'une
ressource ``pending`` → 409, miroir exact du régime prof (on est alors
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
from app.models.block import TYPE_EXERCISE, Block
from app.models.course import (
    VISIBILITY_DRAFT,
    VISIBILITY_PUBLIC,
    Course,
    course_education_levels,
    course_subjects,
)
from app.models.education_level import EducationLevel
from app.models.module import Module
from app.models.resource import STATUS_AVAILABLE, Resource
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
from app.users.service import avatar_url_for


def _not_found() -> HTTPException:
    # Détail unique et volontairement vague : ne pas distinguer « cours
    # inexistant », « lien révoqué », « lien expiré », « cours dépublié ».
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Cours introuvable"
    )


def _link_valid(link: ShareLink | None) -> bool:
    """Un lien ouvre l'accès s'il existe, n'est pas révoqué et n'a pas expiré."""
    return (
        link is not None
        and not link.revoked
        and link.expires_at > datetime.now(UTC)
    )


def _public_content(type_: str, content: dict) -> dict:
    """Content JSONB servi aux élèves — NOUVEAU dict, jamais l'original.

    Pour un bloc ``exercise``, reconstruction explicite sans les
    ``expected_answer`` (corrigé du prof, jamais servi — contrat de
    block.py). Les autres types sont copiés tels quels.
    """
    if type_ != TYPE_EXERCISE:
        return dict(content)
    return {
        "statement": content.get("statement", ""),
        "questions": [
            {
                "id": q.get("id"),
                "statement": q.get("statement", ""),
                "type": q.get("type"),
            }
            for q in content.get("questions", [])
            if isinstance(q, dict)
        ],
    }


def _block_read(block: Block) -> PublicBlockRead:
    return PublicBlockRead(
        id=block.id,
        position=block.position,
        type=block.type,
        title=block.title,
        description=block.description,
        content=_public_content(block.type, block.content),
        resource_id=block.resource_id,
        module_id=block.module_id,
    )


def _resource_read(resource: Resource) -> PublicResourceRead:
    return PublicResourceRead(
        id=resource.id,
        type=resource.type,
        original_name=resource.original_name,
        size=resource.size,
        mime=resource.mime,
    )


def _course_read(
    course: Course, subjects: list[str], education_levels: list[str], block_count: int
) -> PublicCourseRead:
    return PublicCourseRead(
        id=course.id,
        title=course.title,
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
    ``private`` et qu'un token est fourni : 2) lien (scopé à CE cours —
    un token du cours A n'ouvre jamais le cours B).
    """
    course = (
        (await db.execute(select(Course).where(Course.id == course_id)))
        .scalars()
        .one_or_none()
    )
    if course is None or course.visibility == VISIBILITY_DRAFT:
        raise _not_found()
    if course.visibility == VISIBILITY_PUBLIC:
        return course
    if token is None:
        raise _not_found()
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
        raise _not_found()
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
        raise _not_found()
    course = (
        (await db.execute(select(Course).where(Course.id == link.course_id)))
        .scalars()
        .one_or_none()
    )
    if course is None or course.visibility == VISIBILITY_DRAFT:
        raise _not_found()
    return course


async def course_detail_public(db: AsyncSession, course: Course) -> PublicCourseDetailRead:
    """Détail complet filtré d'un cours déjà autorisé.

    Ordre des execute : 1) noms de matières, 2) noms de niveaux, 3) blocs
    (tri stable ``position, id``), 4) ressources ``available`` (tri
    ``created_at desc, id``, miroir de la liste prof).
    """
    subjects = list(
        (
            await db.execute(
                select(Subject.name)
                .select_from(
                    course_subjects.join(
                        Subject, Subject.id == course_subjects.c.subject_id
                    )
                )
                .where(course_subjects.c.course_id == course.id)
                .order_by(Subject.name)
            )
        )
        .scalars()
        .all()
    )
    education_levels = list(
        (
            await db.execute(
                select(EducationLevel.name)
                .select_from(
                    course_education_levels.join(
                        EducationLevel,
                        EducationLevel.id
                        == course_education_levels.c.education_level_id,
                    )
                )
                .where(course_education_levels.c.course_id == course.id)
                .order_by(EducationLevel.name)
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
                    Resource.status == STATUS_AVAILABLE,
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
    n'est pas ``available`` (miroir exact du régime prof). Le bucket n'est
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
        raise _not_found()
    if resource.status != STATUS_AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ressource non disponible (upload non confirmé)",
        )
    download_url = storage.presign_get(
        resource.s3_key, resource.original_name, inline=inline
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
        raise _not_found()
    return PublicModuleRead(
        id=module.id, title=module.title, html=module.html, css=module.css, js=module.js
    )


async def list_public_courses_by_professor(
    db: AsyncSession, user_id: uuid.UUID, storage: Storage
) -> PublicProfessorRead:
    """Catalogue public d'un prof : ses cours ``public``, du plus récent au
    plus ancien.

    Ordre des execute : 1) utilisateur, 2) cours publics ; puis, s'il y en
    a : 3) noms de matières, 4) noms de niveaux, 5) comptes de blocs.
    Utilisateur inconnu ou sans cours public → même réponse (liste vide,
    ``public_name``/``avatar_url`` éventuels) : pas d'oracle d'existence
    d'un compte. L'``avatar_url`` est présignée localement (aucune I/O).
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
                    Course.visibility == VISIBILITY_PUBLIC,
                )
                .order_by(Course.updated_at.desc(), Course.id)
            )
        )
        .scalars()
        .all()
    )
    public_name = user.public_name if user is not None else None
    avatar_url = (
        avatar_url_for(user.avatar_s3_key, user.avatar_status, storage)
        if user is not None
        else None
    )
    if not courses:
        return PublicProfessorRead(
            public_name=public_name, avatar_url=avatar_url, courses=[]
        )
    course_ids = [c.id for c in courses]

    subjects: dict[uuid.UUID, list[str]] = {c.id: [] for c in courses}
    subject_rows = (
        await db.execute(
            select(course_subjects.c.course_id, Subject.name)
            .select_from(
                course_subjects.join(Subject, Subject.id == course_subjects.c.subject_id)
            )
            .where(course_subjects.c.course_id.in_(course_ids))
            .order_by(course_subjects.c.course_id, Subject.name)
        )
    ).all()
    for course_id, name in subject_rows:
        subjects[course_id].append(name)

    levels: dict[uuid.UUID, list[str]] = {c.id: [] for c in courses}
    level_rows = (
        await db.execute(
            select(course_education_levels.c.course_id, EducationLevel.name)
            .select_from(
                course_education_levels.join(
                    EducationLevel,
                    EducationLevel.id == course_education_levels.c.education_level_id,
                )
            )
            .where(course_education_levels.c.course_id.in_(course_ids))
            .order_by(course_education_levels.c.course_id, EducationLevel.name)
        )
    ).all()
    for course_id, name in level_rows:
        levels[course_id].append(name)

    counts = dict(
        (
            await db.execute(
                select(Block.course_id, func.count())
                .where(Block.course_id.in_(course_ids))
                .group_by(Block.course_id)
            )
        ).all()
    )
    return PublicProfessorRead(
        public_name=public_name,
        avatar_url=avatar_url,
        courses=[
            _course_read(c, subjects[c.id], levels[c.id], counts.get(c.id, 0))
            for c in courses
        ],
    )
