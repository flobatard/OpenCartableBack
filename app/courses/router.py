import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.courses import service
from app.courses.schemas import (
    BlockCreate,
    BlockOrderUpdate,
    BlockRead,
    BlockUpdate,
    CourseCreate,
    CourseDetailRead,
    CourseRead,
)
from app.users import service as users_service

# Auth par paramètre (pas en dependencies= du router) : chaque handler résout
# la ligne users du prof (owner_id) pour scoper les cours à leur propriétaire.
router = APIRouter(tags=["courses"])


@router.get("/courses", response_model=list[CourseRead])
async def list_my_courses(
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CourseRead]:
    """Cours du prof courant, du plus récemment modifié au plus ancien."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.list_courses(db, user)


@router.post("/courses", response_model=CourseRead, status_code=status.HTTP_201_CREATED)
async def create_course(
    payload: CourseCreate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CourseRead:
    """Crée un cours (titre, description, classement matières/niveaux)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.create_course(db, user, payload)


@router.get("/courses/{course_id}", response_model=CourseDetailRead)
async def read_course(
    course_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CourseDetailRead:
    """Détail d'un cours avec ses blocs ordonnés."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.get_course_detail(db, user, course_id)


@router.post(
    "/courses/{course_id}/blocks",
    response_model=BlockRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_block(
    course_id: uuid.UUID,
    payload: BlockCreate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BlockRead:
    """Ajoute un bloc vide en fin de cours (son contenu s'éditera plus tard)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.add_block(db, user, course_id, payload)


# À garder déclaré avant un futur PUT /courses/{course_id}/blocks/{block_id} :
# « order » doit matcher le segment littéral, pas un id de bloc.
@router.put("/courses/{course_id}/blocks/order", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_blocks(
    course_id: uuid.UUID,
    payload: BlockOrderUpdate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Réécrit l'ordre complet des blocs du cours (positions 0..n-1)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    await service.reorder_blocks(db, user, course_id, payload)


@router.patch("/courses/{course_id}/blocks/{block_id}", response_model=BlockRead)
async def update_block(
    course_id: uuid.UUID,
    block_id: uuid.UUID,
    payload: BlockUpdate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> BlockRead:
    """Édite un bloc : titre/description (tous types) et/ou contenu texte (markdown)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.update_block(db, user, course_id, block_id, payload)


@router.delete(
    "/courses/{course_id}/blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_block(
    course_id: uuid.UUID,
    block_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Supprime un bloc du cours."""
    user = await users_service.get_or_create_by_sub(db, auth)
    await service.delete_block(db, user, course_id, block_id)
