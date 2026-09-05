"""Routes de smoke-test du client IA générique (BYO token), protégées prof.

Preuve de bout en bout de la brique :mod:`app.core.ai` (appel classique et
flux SSE) et banc d'essai de la cascade config × quota. Supprimable : retirer
le mount dans ``create_app()`` et ce package, après avoir porté les tests de
cascade de ``tests/test_ai_api.py`` au niveau service.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import service
from app.ai.schemas import ChatRequest, ChatResponse
from app.core.ai import AIClient, get_ai_client
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.sse import sse_response

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    client: AIClient = Depends(get_ai_client),
) -> ChatResponse:
    """Appel classique : réponse complète en une fois."""
    return await service.chat(client, db, payload, auth)


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    client: AIClient = Depends(get_ai_client),
) -> StreamingResponse:
    """Appel streamé : événements SSE ``token``/``thinking``/``done``/``error``
    (contrat dans :mod:`app.core.sse`). Une config invalide ou un credential
    illisible échoue en 4xx/503 AVANT le début du flux."""
    events = await service.sse_stream(client, db, payload, auth)
    return sse_response(events)
