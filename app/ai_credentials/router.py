"""Routes des configurations IA nommées de l'utilisateur (motif app/users/router.py).

Collection propre à l'utilisateur courant : GET répond toujours 200 (liste
vide si rien n'est configuré), jamais 404 ; toute route mutante renvoie
l'enveloppe complète. La clé API n'est jamais ré-émise. ``PUT /active`` est
déclaré AVANT ``/{config_id}`` : sinon « active » serait pris pour un UUID.
"""

import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_credentials import service
from app.ai_credentials.schemas import (
    ActiveConfigurationIn,
    AIConfigurationIn,
    AIConnectionTestIn,
    AIConnectionTestRead,
    AICredentialsRead,
    AIModelListIn,
    AIModelListRead,
    ReasoningOptionsIn,
    ReasoningOptionsRead,
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
    """Configurations IA de l'utilisateur courant (sans les clés, jamais)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.read_credentials(db, user)


@router.post(
    "/users/me/ai-credentials",
    response_model=AICredentialsRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_my_ai_configuration(
    payload: AIConfigurationIn,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AICredentialsRead:
    """Crée une configuration nommée et l'active."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.create_configuration(db, user, payload)


@router.put("/users/me/ai-credentials/active", response_model=AICredentialsRead)
async def set_my_active_ai_configuration(
    payload: ActiveConfigurationIn,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AICredentialsRead:
    """Bascule sur une configuration (``id``) ou sur l'IA par défaut (``null``)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.set_active_configuration(db, user, payload.id)


@router.put("/users/me/ai-credentials/{config_id}", response_model=AICredentialsRead)
async def update_my_ai_configuration(
    config_id: uuid.UUID,
    payload: AIConfigurationIn,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AICredentialsRead:
    """Remplace une configuration ; ``api_key`` omise = clé existante conservée."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.update_configuration(db, user, config_id, payload)


@router.delete("/users/me/ai-credentials/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_ai_configuration(
    config_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Supprime une configuration (seule façon d'effacer sa clé) ; supprimer
    l'active ramène à l'IA par défaut."""
    user = await users_service.get_or_create_by_sub(db, auth)
    await service.delete_configuration(db, user, config_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/users/me/ai-credentials/test", response_model=AIConnectionTestRead)
async def test_my_ai_credentials(
    payload: AIConnectionTestIn,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    ai: AIClient = Depends(get_ai_client),
) -> AIConnectionTestRead:
    """Teste la config du formulaire par un mini-appel provider réel.

    ``api_key`` omise + ``config_id`` = tester avec la clé déjà enregistrée
    de cette configuration. BYO token intégral : jamais de fallback ``AI_*``,
    jamais de quota.
    """
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.test_connection(db, user, payload, ai)


@router.post("/users/me/ai-credentials/models", response_model=AIModelListRead)
async def list_my_ai_provider_models(
    payload: AIModelListIn,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AIModelListRead:
    """Modèles proposés par le provider — POST : la clé voyage en body, jamais
    en query. Même sémantique de clé que le test ; jamais de quota."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.list_provider_models(db, user, payload)


@router.post("/users/me/ai-credentials/reasoning-options", response_model=ReasoningOptionsRead)
async def read_my_reasoning_options(
    payload: ReasoningOptionsIn,
    auth: AuthenticatedUser = Depends(get_current_user),
) -> ReasoningOptionsRead:
    """Options de raisonnement (bascule, niveaux natifs) à proposer pour un
    couple (provider, modèle) saisi dans le formulaire — catalogue pur : ni
    lecture DB, ni appel provider, ni quota."""
    return service.read_reasoning_options(payload)
