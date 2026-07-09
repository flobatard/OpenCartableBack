import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.storage import Storage, get_storage
from app.courses.schemas import BlockRead
from app.resources import service
from app.resources.schemas import (
    ResourceConfirm,
    ResourceCreate,
    ResourceDownload,
    ResourcePresign,
)
from app.users import service as users_service

# Ressources S3 d'un cours : upload direct navigateur→S3 par URL présignée.
# Auth par paramètre (comme courses) : chaque handler résout l'owner et scope.
router = APIRouter(tags=["resources"])


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
    response_model=BlockRead,
    status_code=status.HTTP_201_CREATED,
)
async def confirm_resource(
    course_id: uuid.UUID,
    resource_id: uuid.UUID,
    payload: ResourceConfirm,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> BlockRead:
    """Confirme l'upload (HEAD S3) et matérialise le bloc ressource du cours."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.confirm_upload(db, user, course_id, resource_id, payload, storage)


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
