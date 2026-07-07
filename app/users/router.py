from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.users import service
from app.users.schemas import OnboardingSubmit, UserProfileRead

# Auth par paramètre (pas en dependencies= du router) : le service a besoin
# de l'AuthenticatedUser pour résoudre/provisionner la ligne users.
router = APIRouter(tags=["users"])


@router.get("/users/me", response_model=UserProfileRead)
async def read_my_profile(
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserProfileRead:
    """Profil de l'utilisateur courant ; crée la ligne au premier appel."""
    user = await service.get_or_create_by_sub(db, auth)
    return await service.read_profile(db, user)


@router.put("/users/me/onboarding", response_model=UserProfileRead)
async def submit_onboarding(
    payload: OnboardingSubmit,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserProfileRead:
    """Soumet (ou re-soumet) l'onboarding — remplacement complet du profil."""
    user = await service.get_or_create_by_sub(db, auth)
    return await service.complete_onboarding(db, user, payload)
