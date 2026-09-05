"""Lectures du domaine cours partagées entre plusieurs paquets.

Chaque fonction documente le nombre et l'ordre de ses ``execute`` : c'est le
contrat des fausses sessions FIFO des tests d'API. Les lectures batchées
gardent la forme de leurs lignes (``(course_id, valeur)``) : les tests les
servent telles quelles.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http import not_found
from app.models.block import Block
from app.models.course import Course, course_education_levels, course_subjects
from app.models.education_level import EducationLevel
from app.models.subject import Subject
from app.models.user import User


async def get_owned_course(db: AsyncSession, user: User, course_id: uuid.UUID) -> Course:
    """Le cours du prof, ou 404 s'il n'existe pas ou appartient à autrui.

    Un seul ``execute`` (``id`` + ``owner_id``) : un cours d'autrui est
    introuvable, jamais interdit — on ne divulgue pas son existence. L'instance
    ORM chargée sert ensuite au bump d'``updated_at`` des mutations.
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
        raise not_found("Cours introuvable")
    return course


def _grouped(course_ids: Sequence[uuid.UUID], rows) -> dict[uuid.UUID, list]:
    grouped: dict[uuid.UUID, list] = {course_id: [] for course_id in course_ids}
    for course_id, value in rows:
        grouped[course_id].append(value)
    return grouped


async def taxonomy_names_by_course(
    db: AsyncSession, course_ids: Sequence[uuid.UUID]
) -> tuple[dict[uuid.UUID, list[str]], dict[uuid.UUID, list[str]]]:
    """Noms de matières puis de niveaux d'étude par cours (tri alphabétique).

    Deux ``execute``, dans cet ordre : matières, niveaux. Sert les cartes des
    régimes publics (catalogue d'un prof, recherche).
    """
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
    return _grouped(course_ids, subject_rows), _grouped(course_ids, level_rows)


async def taxonomy_ids_by_course(
    db: AsyncSession, course_ids: Sequence[uuid.UUID]
) -> tuple[dict[uuid.UUID, list[uuid.UUID]], dict[uuid.UUID, list[uuid.UUID]]]:
    """Ids de matières puis de niveaux d'étude par cours (variante prof, sans
    jointure). Deux ``execute``, dans cet ordre : matières, niveaux."""
    subject_rows = (
        await db.execute(
            select(course_subjects.c.course_id, course_subjects.c.subject_id)
            .where(course_subjects.c.course_id.in_(course_ids))
            .order_by(course_subjects.c.course_id, course_subjects.c.subject_id)
        )
    ).all()
    level_rows = (
        await db.execute(
            select(
                course_education_levels.c.course_id,
                course_education_levels.c.education_level_id,
            )
            .where(course_education_levels.c.course_id.in_(course_ids))
            .order_by(
                course_education_levels.c.course_id,
                course_education_levels.c.education_level_id,
            )
        )
    ).all()
    return _grouped(course_ids, subject_rows), _grouped(course_ids, level_rows)


async def block_counts_by_course(
    db: AsyncSession, course_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Nombre de blocs par cours (un ``execute`` ; cours sans bloc absent)."""
    return dict(
        (
            await db.execute(
                select(Block.course_id, func.count())
                .where(Block.course_id.in_(course_ids))
                .group_by(Block.course_id)
            )
        ).all()
    )
