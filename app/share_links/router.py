import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.share_links import service
from app.share_links.schemas import ShareLinkCreate, ShareLinkRead
from app.users import service as users_service

# Gestion PROF des liens de partage (JWT requis) ; la consommation élève des
# tokens vit dans app/public/ (régime d'autorisation distinct, sans Zitadel).
# Auth par paramètre (comme courses) : chaque handler résout l'owner et scope.
router = APIRouter(tags=["share-links"])


@router.get("/courses/{course_id}/share-links", response_model=list[ShareLinkRead])
async def list_share_links(
    course_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ShareLinkRead]:
    """Liens de partage du cours, du plus récent au plus ancien, révoqués inclus."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.list_share_links(db, user, course_id)


@router.post(
    "/courses/{course_id}/share-links",
    response_model=ShareLinkRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_share_link(
    course_id: uuid.UUID,
    payload: ShareLinkCreate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShareLinkRead:
    """Crée un lien de partage élève (token opaque, expiration à 9 mois)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.create_share_link(db, user, course_id, payload)


@router.delete(
    "/courses/{course_id}/share-links/{link_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_share_link(
    course_id: uuid.UUID,
    link_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Révoque un lien (soft : il reste listé, mais n'ouvre plus le cours)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    await service.revoke_share_link(db, user, course_id, link_id)
