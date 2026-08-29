"""Routes de smoke-test du client IA générique (BYO token).

Preuve de bout en bout de la brique :mod:`app.core.ai` et **référence
d'intégration SSE** pour les futures features (J5). Facilement supprimable :
retirer le mount dans ``create_app()`` et ce package.

Les deux routes sont protégées prof (JWT). Le streaming est un **POST** servi
en ``text/event-stream`` : le front le consommera via ``fetch`` +
``ReadableStream`` (``EventSource`` ne sait ni POSTer ni porter un Bearer).
``X-Accel-Buffering: no`` demande au nginx d'infra (hors repo) de ne pas
bufferiser le flux ; son ``proxy_read_timeout`` doit couvrir la durée d'une
génération.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.ai import service
from app.ai.schemas import ChatRequest, ChatResponse
from app.core.ai import AIClient, get_ai_client
from app.core.auth import AuthenticatedUser, get_current_user

router = APIRouter(prefix="/ai", tags=["ai"])

_SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
}


@router.post("/chat", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    auth: AuthenticatedUser = Depends(get_current_user),
    client: AIClient = Depends(get_ai_client),
) -> ChatResponse:
    """Appel classique : réponse complète en une fois."""
    return await service.chat(client, payload, auth.sub)


@router.post("/chat/stream")
async def chat_stream(
    payload: ChatRequest,
    auth: AuthenticatedUser = Depends(get_current_user),
    client: AIClient = Depends(get_ai_client),
) -> StreamingResponse:
    """Appel streamé : événements SSE ``token``/``done``/``error`` (contrat
    documenté dans :mod:`app.ai.service`). Une config invalide échoue en 4xx
    AVANT le début du flux (validation eager de ``AIClient.stream``)."""
    events = service.sse_stream(client, payload, auth.sub)
    return StreamingResponse(events, media_type="text/event-stream", headers=_SSE_HEADERS)
