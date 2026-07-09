import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.storage import Storage, get_storage
from app.resources import service
from app.resources.schemas import (
    ResourceCreate,
    ResourceDownload,
    ResourcePresign,
    ResourceRead,
    ResourceUpdate,
)
from app.users import service as users_service

# Bibliothèque de ressources S3 d'un cours (indépendante des blocs) : CRUD +
# upload direct navigateur→S3 par URL présignée.
# Auth par paramètre (comme courses) : chaque handler résout l'owner et scope.
router = APIRouter(tags=["resources"])


@router.get("/courses/{course_id}/resources", response_model=list[ResourceRead])
async def list_resources(
    course_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ResourceRead]:
    """Bibliothèque du cours, de la plus récente à la plus ancienne."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.list_resources(db, user, course_id)


@router.post(
    "/courses/{course_id}/resources",
    response_model=ResourcePresign,
    status_code=status.HTTP_201_CREATED,
)
async def create_resource(
    course_id: uuid.UUID,
    payload: ResourceCreate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> ResourcePresign:
    """Déclare un fichier et renvoie une URL présignée pour l'uploader sur S3."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.presign_upload(db, user, course_id, payload, storage)


@router.post(
    "/courses/{course_id}/resources/{resource_id}/confirm",
    response_model=ResourceRead,
)
async def confirm_resource(
    course_id: uuid.UUID,
    resource_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> ResourceRead:
    """Confirme l'upload (HEAD S3) : la ressource devient disponible. Sans body."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.confirm_upload(db, user, course_id, resource_id, storage)


@router.patch(
    "/courses/{course_id}/resources/{resource_id}",
    response_model=ResourceRead,
)
async def update_resource(
    course_id: uuid.UUID,
    resource_id: uuid.UUID,
    payload: ResourceUpdate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ResourceRead:
    """Renomme une ressource (nom affiché seulement, la clé S3 reste figée)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.update_resource(db, user, course_id, resource_id, payload)


@router.delete(
    "/courses/{course_id}/resources/{resource_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_resource(
    course_id: uuid.UUID,
    resource_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> None:
    """Supprime une ressource, son objet S3 et les blocs document qui la pointaient."""
    user = await users_service.get_or_create_by_sub(db, auth)
    await service.delete_resource(db, user, course_id, resource_id, storage)


@router.get(
    "/courses/{course_id}/resources/{resource_id}/download",
    response_model=ResourceDownload,
)
async def download_resource(
    course_id: uuid.UUID,
    resource_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> ResourceDownload:
    """Renvoie une URL présignée (TTL court) pour lire/télécharger la ressource."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.presign_download(db, user, course_id, resource_id, storage)
