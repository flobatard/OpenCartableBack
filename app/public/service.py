"""Lectures du régime d'accès élève : détail filtré, presign, catalogue.

L'autorisation (visibilité + token de partage, 404 uniforme) vit dans
:mod:`app.public.access` ; ce module ne fait que lire un cours déjà autorisé
et n'expose JAMAIS une donnée réservée au prof (règle d'or de ``schemas.py`` :
pas de ``expected_answer``, de ``s3_key``, d'``owner_id`` ni d'e-mail — le
content des blocs ``exercise`` est RECONSTRUIT par :func:`public_content`).
Tout est en lecture seule : aucune fonction ne commit. L'ordre des ``execute``
de chaque fonction est un contrat des tests (fausse session FIFO). Seule
exception au 404 uniforme : presign d'une ressource ``pending`` → 409 (on est
alors déjà autorisé sur le cours, aucune fuite).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import Storage
from app.courses.queries import block_counts_by_course, taxonomy_names_by_course
from app.models.block import TYPE_EXERCISE, Block
from app.models.course import (
    VISIBILITY_PUBLIC,
    Course,
    course_education_levels,
    course_subjects,
)
from app.models.education_level import EducationLevel
from app.models.module import Module
from app.models.resource import STATUS_AVAILABLE, Resource
from app.models.subject import Subject
from app.models.user import User
from app.public.access import course_not_found
from app.public.schemas import (
    PublicBlockRead,
    PublicCourseDetailRead,
    PublicCourseRead,
    PublicDownloadRead,
    PublicModuleRead,
    PublicModuleSummary,
    PublicProfessorRead,
    PublicResourceRead,
)
from app.resources.service import download_url_for
from app.users.service import avatar_url_for


def public_content(type_: str, content: dict) -> dict:
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
        content=public_content(block.type, block.content),
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


async def course_detail_public(db: AsyncSession, course: Course) -> PublicCourseDetailRead:
    """Détail complet filtré d'un cours déjà autorisé.

    Ordre des execute : 1) noms de matières, 2) noms de niveaux, 3) blocs
    (tri stable ``position, id``), 4) ressources ``available`` (tri
    ``created_at desc, id``, miroir de la liste prof), 5) modules (même tri
    que la liste prof — titres seuls, le code reste servi par
    ``get_public_module``).
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
    modules = (
        (
            await db.execute(
                select(Module)
                .where(Module.course_id == course.id)
                .order_by(Module.created_at.desc(), Module.id)
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
        modules=[PublicModuleSummary(id=m.id, title=m.title) for m in modules],
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

    Ordre des execute : 1) ressource (scopée cours) ; 409 si elle n'est pas
    ``available`` (règle partagée avec le régime prof). Le bucket n'est
    jamais public : c'est la présignature qui matérialise l'accès.
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
        raise course_not_found()
    download_url, expires_in = download_url_for(resource, storage, inline=inline)
    return PublicDownloadRead(download_url=download_url, expires_in=expires_in)


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
        raise course_not_found()
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

    subjects, levels = await taxonomy_names_by_course(db, course_ids)
    counts = await block_counts_by_course(db, course_ids)
    return PublicProfessorRead(
        public_name=public_name,
        avatar_url=avatar_url,
        courses=[
            _course_read(c, subjects[c.id], levels[c.id], counts.get(c.id, 0))
            for c in courses
        ],
    )
