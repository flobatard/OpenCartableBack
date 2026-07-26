import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.modules import service
from app.modules.schemas import ModuleCreate, ModuleRead, ModuleSummary, ModuleUpdate
from app.users import service as users_service

# Bibliothèque de modules interactifs HTML/CSS/JS d'un cours (indépendante
# des blocs) : CRUD pur BDD, le code est exécuté en iframe sandbox côté front.
# Auth par paramètre (comme courses) : chaque handler résout l'owner et scope.
router = APIRouter(tags=["modules"])


@router.get("/courses/{course_id}/modules", response_model=list[ModuleSummary])
async def list_modules(
    course_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ModuleSummary]:
    """Modules du cours, du plus récent au plus ancien, sans leur code."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.list_modules(db, user, course_id)


@router.post(
    "/courses/{course_id}/modules",
    response_model=ModuleRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_module(
    course_id: uuid.UUID,
    payload: ModuleCreate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModuleRead:
    """Crée un module (code éventuellement vide, rempli dans l'éditeur)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.create_module(db, user, course_id, payload)


@router.get("/courses/{course_id}/modules/{module_id}", response_model=ModuleRead)
async def get_module(
    course_id: uuid.UUID,
    module_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModuleRead:
    """Détail d'un module, code inclus (éditeur + exécution sandbox)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.get_module(db, user, course_id, module_id)


@router.patch("/courses/{course_id}/modules/{module_id}", response_model=ModuleRead)
async def update_module(
    course_id: uuid.UUID,
    module_id: uuid.UUID,
    payload: ModuleUpdate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ModuleRead:
    """Édition partielle : renommage et/ou sauvegarde du code (autosave)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.update_module(db, user, course_id, module_id, payload)


@router.delete(
    "/courses/{course_id}/modules/{module_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_module(
    course_id: uuid.UUID,
    module_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Supprime un module et les blocs module qui le pointaient (CASCADE)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    await service.delete_module(db, user, course_id, module_id)
