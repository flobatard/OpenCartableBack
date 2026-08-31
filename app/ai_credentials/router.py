"""Routes du credential IA de l'utilisateur (motif app/users/router.py).

Ressource propre à l'utilisateur courant : GET répond toujours 200 (tout
null si non configuré), jamais 404. La clé API n'est jamais ré-émise.
"""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_credentials import service
from app.ai_credentials.schemas import (
    AIConnectionTestIn,
    AIConnectionTestRead,
    AICredentialsRead,
    AICredentialsUpdate,
    AIModelListIn,
    AIModelListRead,
)
from app.core.ai import AIClient, get_ai_client
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.users import service as users_service

router = APIRouter(tags=["ai-credentials"])


@router.get("/users/me/ai-credentials", response_model=AICredentialsRead)
async def read_my_ai_credentials(
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AICredentialsRead:
    """Credential IA de l'utilisateur courant (sans la clé, jamais)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.read_credentials(db, user)


@router.put("/users/me/ai-credentials", response_model=AICredentialsRead)
async def update_my_ai_credentials(
    payload: AICredentialsUpdate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AICredentialsRead:
    """Enregistre le credential ; ``api_key`` omise = clé existante conservée."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.update_credentials(db, user, payload)


@router.post("/users/me/ai-credentials/test", response_model=AIConnectionTestRead)
async def test_my_ai_credentials(
    payload: AIConnectionTestIn,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ai: AIClient = Depends(get_ai_client),
) -> AIConnectionTestRead:
    """Teste la config du formulaire par un mini-appel provider réel.

    ``api_key`` omise = tester avec la clé déjà enregistrée (sémantique du
    PUT). BYO token intégral : jamais de fallback ``AI_*``, jamais de quota.
    """
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.test_connection(user, payload, ai)


@router.post("/users/me/ai-credentials/models", response_model=AIModelListRead)
async def list_my_ai_provider_models(
    payload: AIModelListIn,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AIModelListRead:
    """Modèles proposés par le provider — POST : la clé voyage en body, jamais
    en query. Même sémantique de clé que le test ; jamais de quota."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.list_provider_models(user, payload)


@router.delete("/users/me/ai-credentials", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_ai_credentials(
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Supprime tout le credential (seule façon d'effacer la clé)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    await service.delete_credentials(db, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
