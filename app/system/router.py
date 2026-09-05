"""Routes système : sonde de santé publique et identité du token.

``GET /health`` est le healthcheck des composes (aucune dépendance, aucun
JWT). ``GET /me`` est la route protégée de référence : elle renvoie ce que
:func:`app.core.auth.get_current_user` a validé, sans toucher la base — le
compte applicatif, lui, vit sous ``/users/me`` (:mod:`app.users`).
"""

from fastapi import APIRouter, Depends

from app.core.auth import AuthenticatedUser, get_current_user
from app.system.schemas import MeRead

router = APIRouter()


@router.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/me", response_model=MeRead, tags=["auth"])
async def read_me(user: AuthenticatedUser = Depends(get_current_user)) -> MeRead:
    return MeRead(
        sub=user.sub,
        email=user.email,
        roles=sorted(user.roles),
        claims=user.claims,
    )
