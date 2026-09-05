"""Lectures du domaine cours partagées entre plusieurs paquets.

Chaque fonction documente le nombre et l'ordre de ses ``execute`` : c'est le
contrat des fausses sessions FIFO des tests d'API.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http import not_found
from app.models.course import Course
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
