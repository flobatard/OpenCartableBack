from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.storage import Storage, get_storage
from app.users import service
from app.users.schemas import AvatarCreate, AvatarPresign, ProfileUpdate, UserProfileRead

# Auth par paramètre (pas en dependencies= du router) : le service a besoin
# de l'AuthenticatedUser pour résoudre/provisionner la ligne users.
router = APIRouter(tags=["users"])


@router.get("/users/me", response_model=UserProfileRead)
async def read_my_profile(
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> UserProfileRead:
    """Profil de l'utilisateur courant ; crée la ligne au premier appel."""
    user = await service.get_or_create_by_sub(db, auth)
    return await service.read_profile(db, user, storage)


@router.put("/users/me/profile", response_model=UserProfileRead)
async def update_my_profile(
    payload: ProfileUpdate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> UserProfileRead:
    """Met à jour le profil (onboarding initial ou édition ultérieure) — remplacement complet."""
    user = await service.get_or_create_by_sub(db, auth)
    return await service.update_profile(db, user, payload, storage)


@router.post(
    "/users/me/avatar", response_model=AvatarPresign, status_code=status.HTTP_201_CREATED
)
async def presign_my_avatar(
    payload: AvatarCreate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> AvatarPresign:
    """Déclare l'upload de la photo de profil (URL présignée PUT, motif ressources)."""
    user = await service.get_or_create_by_sub(db, auth)
    return await service.presign_avatar(db, user, payload, storage)


@router.post("/users/me/avatar/confirm", response_model=UserProfileRead)
async def confirm_my_avatar(
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> UserProfileRead:
    """Confirme l'upload (HEAD S3) ; renvoie le profil complet, avatar_url posée."""
    user = await service.get_or_create_by_sub(db, auth)
    return await service.confirm_avatar(db, user, storage)


@router.delete("/users/me/avatar", response_model=UserProfileRead)
async def delete_my_avatar(
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> UserProfileRead:
    """Supprime la photo de profil ; renvoie le profil complet (avatar_url nulle)."""
    user = await service.get_or_create_by_sub(db, auth)
    return await service.delete_avatar(db, user, storage)
