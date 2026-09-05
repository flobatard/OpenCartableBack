"""Service des routes de smoke-test IA : conversions + encodage SSE minimal.

Contrat SSE et rationale dans :mod:`app.core.sse`. Ce package est supprimable
(retirer le mount dans ``create_app()`` et le package) une fois les tests de
la cascade config × quota de ``tests/test_ai_api.py`` portés au niveau
service — cf. TODO.md.
"""

from collections.abc import AsyncIterator

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.schemas import AIConfigIn, ChatRequest, ChatResponse, ChatUsageRead
from app.ai_credentials.service import effective_config, refund_default_quota, refund_on_error
from app.core.ai import AIClient, AIRequestConfig, ChatMessage
from app.core.auth import AuthenticatedUser
from app.core.sse import sse_event

_TRACE_NAME = "smoke-chat"


def _to_core_config(config: AIConfigIn | None) -> AIRequestConfig | None:
    if config is None:
        return None
    return AIRequestConfig(**config.model_dump())


def _to_core_messages(payload: ChatRequest) -> list[ChatMessage]:
    return [ChatMessage(role=m.role, content=m.content) for m in payload.messages]


async def chat(
    client: AIClient, db: AsyncSession, payload: ChatRequest, auth: AuthenticatedUser
) -> ChatResponse:
    """Appel classique — ``auth.sub`` (jamais l'e-mail) part en trace Langfuse.

    La config passe par la cascade ``effective_config`` (config explicite >
    credential utilisateur chiffré > None → fallback serveur d'AIClient) ; le
    quota de l'IA par défaut réservé par la cascade est remboursé si l'appel
    provider échoue.
    """
    config, ticket = await effective_config(db, auth, _to_core_config(payload.config))
    async with refund_on_error(db, ticket):
        completion = await client.complete(
            _to_core_messages(payload),
            config,
            trace_name=_TRACE_NAME,
            user_id=auth.sub,
        )
    usage = ChatUsageRead(**completion.usage.model_dump()) if completion.usage else None
    return ChatResponse(
        content=completion.content,
        provider=completion.provider,
        model=completion.model,
        usage=usage,
    )


async def sse_stream(
    client: AIClient, db: AsyncSession, payload: ChatRequest, auth: AuthenticatedUser
) -> AsyncIterator[str]:
    """Prépare le flux SSE.

    La cascade ``effective_config`` est résolue puis ``client.stream(...)``
    appelé ICI, hors du generator : leurs erreurs (credential illisible,
    config absente/invalide) remontent en vraies HTTPException 4xx/503 avant
    que la route ne retourne la réponse. Quota de l'IA par défaut : remboursé
    sur erreur eager, et sur erreur mid-stream survenue AVANT le premier token
    — un flux qui a déjà produit du contenu reste compté.
    """
    config, ticket = await effective_config(db, auth, _to_core_config(payload.config))
    async with refund_on_error(db, ticket):
        events = client.stream(
            _to_core_messages(payload),
            config,
            trace_name=_TRACE_NAME,
            user_id=auth.sub,
        )

    async def _encode() -> AsyncIterator[str]:
        # La session `db` reste utilisable ici : les dépendances yield de
        # FastAPI ne sont refermées qu'après l'envoi complet du flux.
        tokens_emitted = False
        try:
            async for event in events:
                if event.type == "token":
                    tokens_emitted = True
                    yield sse_event("token", {"delta": event.delta})
                elif event.type == "thinking":
                    yield sse_event("thinking", {"delta": event.delta})
                elif event.type == "done":
                    usage = event.usage.model_dump() if event.usage else None
                    yield sse_event("done", {"usage": usage})
        except HTTPException as exc:
            if ticket is not None and not tokens_emitted:
                await refund_default_quota(db, ticket)
            yield sse_event("error", {"status": exc.status_code, "detail": exc.detail})

    return _encode()
