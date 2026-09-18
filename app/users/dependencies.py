"""Dépendances FastAPI adossées au compte en base, et non au seul JWT.

``get_current_user`` (:mod:`app.core.auth`) ne touche jamais la base
(décision 1) : ce qui dépend d'une donnée du compte — ici le rôle de
plateforme — passe par la résolution ``sub → users`` de ce paquet.
"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.http import forbidden
from app.models.user import PLATFORM_ROLE_SUPER_ADMIN, User
from app.users.service import get_or_create_by_sub


async def require_super_admin(
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Le compte courant s'il est super admin, sinon **403**.

    Un token absent ou invalide est rejeté avant, en 401, par
    ``get_current_user`` : le 403 ne sanctionne que le rôle. Ordre des
    execute : ceux de :func:`~app.users.service.get_or_create_by_sub` (insert,
    select), rien de plus. Déclarée sur un router *et* en paramètre d'une de
    ses routes, elle ne s'exécute qu'une fois par requête (cache de
    dépendances de FastAPI).
    """
    user = await get_or_create_by_sub(db, auth)
    if user.platform_role != PLATFORM_ROLE_SUPER_ADMIN:
        raise forbidden("Accès réservé aux super administrateurs")
    return user
