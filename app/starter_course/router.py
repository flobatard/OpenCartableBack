"""Route de rattrapage du cours d'exemple.

Montée AVANT ``app/courses/router.py`` dans ``create_app()`` : le littéral
``POST /courses/starter`` doit primer sur un futur ``POST /courses/{course_id}``
(même contrainte que ``/courses/import``). Auth par paramètre (motif
``courses``) : le handler résout la ligne ``users`` du prof.

Aucune dépendance ``Storage`` : le manifeste du cours d'exemple ne porte pas
de ressource binaire (cf. :mod:`app.starter_course.service`).
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.courses.schemas import CourseRead
from app.starter_course import service
from app.users import service as users_service

router = APIRouter(tags=["starter-course"])


@router.post(
    "/courses/starter",
    response_model=CourseRead,
    status_code=status.HTTP_201_CREATED,
)
async def load_starter_course(
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CourseRead:
    """Dépose le cours d'exemple en brouillon (toujours un nouveau cours).

    Rattrapage du seed automatique de l'onboarding, exposé par « Mes cours »
    (bouton de l'état vide, entrée discrète sous la liste). Pas de
    déduplication, miroir de ``POST /courses/import`` : un prof qui veut
    relire l'exemple après avoir modifié le premier ne doit pas se heurter à
    un 409.
    """
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.create_starter_course(db, user)
