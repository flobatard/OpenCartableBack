"""Cours du prof : liste, création, détail, édition, suppression et réglages.

La structure des blocs vit dans :mod:`app.courses.blocks`, les lectures
partagées avec d'autres paquets dans :mod:`app.courses.queries`. Tout est
scopé au propriétaire (``owner_id``) : un cours d'autrui est introuvable
(404), jamais interdit (403) — on ne divulgue pas son existence. L'ordre des
``execute`` de chaque fonction est un contrat des tests (fausse session FIFO).
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import touch
from app.core.http import invalid
from app.core.storage import Storage
from app.courses.blocks import block_read
from app.courses.queries import (
    block_counts_by_course,
    get_owned_course,
    taxonomy_ids_by_course,
)
from app.courses.schemas import (
    CourseCreate,
    CourseDetailRead,
    CourseMetaRead,
    CourseRead,
    CourseUpdate,
    PreviewSettings,
    VisibilityUpdate,
)
from app.models.block import Block
from app.models.course import (
    VISIBILITY_DRAFT,
    Course,
    course_education_levels,
    course_subjects,
)
from app.models.education_level import EducationLevel
from app.models.resource import Resource
from app.models.subject import Subject
from app.models.user import User


def _dedupe(ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """Dédoublonne en préservant l'ordre de première apparition."""
    return list(dict.fromkeys(ids))


async def _check_subjects(db: AsyncSession, subject_ids: list[uuid.UUID]) -> None:
    """Un ``execute`` : refuse (422) les matières absentes de la taxonomie."""
    known = set(
        (await db.execute(select(Subject.id).where(Subject.id.in_(subject_ids)))).scalars().all()
    )
    unknown = set(subject_ids) - known
    if unknown:
        raise invalid(f"Matières inconnues : {sorted(map(str, unknown))}")


async def _check_education_levels(db: AsyncSession, level_ids: list[uuid.UUID]) -> None:
    """Un ``execute`` : refuse (422) les niveaux absents de la taxonomie."""
    known = set(
        (await db.execute(select(EducationLevel.id).where(EducationLevel.id.in_(level_ids))))
        .scalars()
        .all()
    )
    unknown = set(level_ids) - known
    if unknown:
        raise invalid(f"Niveaux d'étude inconnus : {sorted(map(str, unknown))}")


def _course_read(
    course: Course,
    subject_ids: list[uuid.UUID],
    education_level_ids: list[uuid.UUID],
    block_count: int,
) -> CourseRead:
    return CourseRead(
        id=course.id,
        title=course.title,
        description=course.description,
        subject_ids=subject_ids,
        education_level_ids=education_level_ids,
        block_count=block_count,
        preview_settings=course.preview_settings,
        visibility=course.visibility,
        created_at=course.created_at,
        updated_at=course.updated_at,
    )


async def list_courses(db: AsyncSession, user: User) -> list[CourseRead]:
    """Cours du prof, du plus récemment modifié au plus ancien.

    Ordre des execute : 1) cours ; puis, s'il y en a : 2) matières,
    3) niveaux, 4) comptes de blocs. Sans cours, court-circuit après 1).
    """
    courses = (
        (
            await db.execute(
                select(Course)
                .where(Course.owner_id == user.id)
                .order_by(Course.updated_at.desc(), Course.id)
            )
        )
        .scalars()
        .all()
    )
    if not courses:
        return []
    course_ids = [c.id for c in courses]

    subjects, levels = await taxonomy_ids_by_course(db, course_ids)
    counts = await block_counts_by_course(db, course_ids)

    return [_course_read(c, subjects[c.id], levels[c.id], counts.get(c.id, 0)) for c in courses]


async def create_course(db: AsyncSession, user: User, payload: CourseCreate) -> CourseRead:
    """Crée un cours et son classement matières/niveaux.

    Ordre des execute : 1) lookup matières, 2) lookup niveaux (toujours
    exécutés, même sur listes vides, pour garder un ordre FIFO constant),
    3) insert cours (RETURNING des timestamps server_default), puis si non
    vides : 4) insert course_subjects, 5) insert course_education_levels.
    """
    subject_ids = _dedupe(payload.subject_ids)
    education_level_ids = _dedupe(payload.education_level_ids)

    await _check_subjects(db, subject_ids)
    await _check_education_levels(db, education_level_ids)

    course_id = uuid.uuid4()
    created_at, updated_at = (
        await db.execute(
            insert(Course)
            .values(
                id=course_id,
                owner_id=user.id,
                title=payload.title,
                description=payload.description,
            )
            .returning(Course.created_at, Course.updated_at)
        )
    ).one()
    if subject_ids:
        await db.execute(
            course_subjects.insert(),
            [{"course_id": course_id, "subject_id": subject_id} for subject_id in subject_ids],
        )
    if education_level_ids:
        await db.execute(
            course_education_levels.insert(),
            [
                {"course_id": course_id, "education_level_id": level_id}
                for level_id in education_level_ids
            ],
        )
    await db.commit()

    return CourseRead(
        id=course_id,
        title=payload.title,
        description=payload.description,
        subject_ids=subject_ids,
        education_level_ids=education_level_ids,
        block_count=0,
        preview_settings={},
        visibility=VISIBILITY_DRAFT,
        created_at=created_at,
        updated_at=updated_at,
    )


async def get_course_detail(
    db: AsyncSession, user: User, course_id: uuid.UUID
) -> CourseDetailRead:
    """Détail d'un cours avec ses blocs ordonnés.

    Ordre des execute : 1) cours (contrôle de propriété), 2) matières,
    3) niveaux, 4) blocs (tri stable ``position, id``).
    """
    course = await get_owned_course(db, user, course_id)
    subject_ids = list(
        (
            await db.execute(
                select(course_subjects.c.subject_id)
                .where(course_subjects.c.course_id == course.id)
                .order_by(course_subjects.c.subject_id)
            )
        )
        .scalars()
        .all()
    )
    education_level_ids = list(
        (
            await db.execute(
                select(course_education_levels.c.education_level_id)
                .where(course_education_levels.c.course_id == course.id)
                .order_by(course_education_levels.c.education_level_id)
            )
        )
        .scalars()
        .all()
    )
    blocks = (
        (
            await db.execute(
                select(Block).where(Block.course_id == course.id).order_by(Block.position, Block.id)
            )
        )
        .scalars()
        .all()
    )
    base = _course_read(course, subject_ids, education_level_ids, len(blocks))
    return CourseDetailRead(**base.model_dump(), blocks=[block_read(b) for b in blocks])


async def update_course(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: CourseUpdate
) -> CourseMetaRead:
    """Édite un cours du prof — titre, description, classement (404 si autrui).

    PATCH partiel : seuls les champs présents dans le payload sont appliqués
    (``model_fields_set``) — une ``description`` à ``null`` l'efface, un champ
    absent ne touche à rien. Ordre des execute : 1) cours (contrôle de
    propriété) ; puis, seulement pour les listes fournies : 2) lookup matières,
    3) lookup niveaux — les DELETE/INSERT des tables de liaison ne consomment
    pas la file. Les lookups passent AVANT toute écriture : un id inconnu est
    un 422 qui n'a rien réécrit.

    Le classement se remplace en entier (delete puis insert du cours) : c'est
    la sémantique du payload, et les tables de liaison n'ont pas de
    qualificatif à préserver. Le cours est « touché » (updated_at) pour
    remonter dans la liste, et la réponse est construite AVANT le commit.
    ``search_vector`` se réindexe seul (trigger côté base), rien à écrire ici.
    """
    course = await get_owned_course(db, user, course_id)
    fields = payload.model_fields_set

    subject_ids = _dedupe(payload.subject_ids or []) if "subject_ids" in fields else None
    level_ids = (
        _dedupe(payload.education_level_ids or [])
        if "education_level_ids" in fields
        else None
    )
    if subject_ids is not None:
        await _check_subjects(db, subject_ids)
    if level_ids is not None:
        await _check_education_levels(db, level_ids)

    if "title" in fields:
        course.title = payload.title
    if "description" in fields:
        course.description = payload.description
    if subject_ids is not None:
        await db.execute(delete(course_subjects).where(course_subjects.c.course_id == course.id))
        if subject_ids:
            await db.execute(
                course_subjects.insert(),
                [{"course_id": course.id, "subject_id": sid} for sid in subject_ids],
            )
    if level_ids is not None:
        await db.execute(
            delete(course_education_levels).where(
                course_education_levels.c.course_id == course.id
            )
        )
        if level_ids:
            await db.execute(
                course_education_levels.insert(),
                [{"course_id": course.id, "education_level_id": lid} for lid in level_ids],
            )
    touch(course)
    updated = CourseMetaRead(
        id=course.id,
        title=course.title,
        description=course.description,
        subject_ids=subject_ids,
        education_level_ids=level_ids,
        updated_at=course.updated_at,
    )
    await db.commit()
    return updated


async def delete_course(
    db: AsyncSession, user: User, course_id: uuid.UUID, storage: Storage
) -> None:
    """Supprime un cours du prof ; 404 s'il n'existe pas ou appartient à autrui.

    Ordre des execute : 1) cours (contrôle de propriété), 2) clés S3 des
    ressources du cours, 3) delete. Les blocs, ressources et lignes de classement
    (course_subjects/course_education_levels) partent en cascade via les FK
    ``ondelete=CASCADE`` ; les objets S3 (hors cascade DB) sont supprimés après
    le commit, pour ne pas laisser d'orphelins dans le bucket.
    """
    course = await get_owned_course(db, user, course_id)
    s3_keys = list(
        (await db.execute(select(Resource.s3_key).where(Resource.course_id == course.id)))
        .scalars()
        .all()
    )
    await db.execute(delete(Course).where(Course.id == course.id))
    await db.commit()
    await storage.delete_many(s3_keys)


async def update_preview_settings(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: PreviewSettings
) -> PreviewSettings:
    """Remplace les réglages de preview d'un cours du prof (404 si autrui).

    Ordre des execute : 1) cours (contrôle de propriété). Le JSONB est remplacé
    par un NOUVEAU dict (mutation d'attribut ORM ; une mutation in-place ne
    serait pas détectée). Le cours est « touché » (updated_at) pour remonter
    dans la liste.
    """
    course = await get_owned_course(db, user, course_id)
    course.preview_settings = payload.model_dump(by_alias=True)  # clés camelCase
    touch(course)
    await db.commit()
    return payload


async def update_visibility(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: VisibilityUpdate
) -> VisibilityUpdate:
    """Change le régime d'accès élève d'un cours du prof (404 si autrui).

    Ordre des execute : 1) cours (contrôle de propriété). Passer en ``draft``
    suspend les liens de partage sans les toucher : la règle vit dans
    :mod:`app.public.access`, vérifiée à chaque accès.
    """
    course = await get_owned_course(db, user, course_id)
    course.visibility = payload.visibility
    touch(course)
    await db.commit()
    return payload
