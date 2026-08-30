"""Service de la route de smoke-test IA : conversions + encodage SSE.

Le format SSE défini ici est le **contrat de référence** pour les futures
features streamées (J5) :

.. code-block:: text

    event: token
    data: {"delta": "…"}

    event: done
    data: {"usage": {"input_tokens": 12, "output_tokens": 87}}

    event: error
    data: {"status": 503, "detail": "Fournisseur IA injoignable"}

JSON compact ``ensure_ascii=False``, chaque événement terminé par ``\\n\\n``,
flux clos après ``done`` ou ``error``. Rationale de l'événement ``error`` : une
erreur survenue APRÈS le premier octet ne peut plus changer le status HTTP
(déjà parti en 200) — l'événement porte donc le status « qu'aurait eu » la
requête (celui du mapping de :mod:`app.core.ai.errors`).
"""

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.schemas import AIConfigIn, ChatRequest, ChatResponse, ChatUsageRead
from app.ai_credentials.service import effective_config, refund_default_quota
from app.core.ai import AIClient, AIRequestConfig, ChatMessage
from app.core.auth import AuthenticatedUser

_TRACE_NAME = "smoke-chat"


def _to_core_config(config: AIConfigIn | None) -> AIRequestConfig | None:
    if config is None:
        return None
    return AIRequestConfig(**config.model_dump())


def _to_core_messages(payload: ChatRequest) -> list[ChatMessage]:
    return [ChatMessage(role=m.role, content=m.content) for m in payload.messages]


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def chat(
    client: AIClient, db: AsyncSession, payload: ChatRequest, auth: AuthenticatedUser
) -> ChatResponse:
    """Appel classique — ``auth.sub`` (jamais l'e-mail) part en trace Langfuse.

    La config passe par la cascade ``effective_config`` (config explicite >
    credential utilisateur chiffré > None → fallback serveur d'AIClient).
    Le quota de l'IA par défaut, réservé par la cascade, est REMBOURSÉ si
    l'appel provider échoue (ticket) : un échec est net-zéro.
    """
    config, ticket = await effective_config(db, auth, _to_core_config(payload.config))
    try:
        completion = await client.complete(
            _to_core_messages(payload),
            config,
            trace_name=_TRACE_NAME,
            user_id=auth.sub,
        )
    except Exception:
        if ticket is not None:
            await refund_default_quota(db, ticket)
        raise
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
    que la route ne retourne la ``StreamingResponse``. Remboursement du
    quota de l'IA par défaut (ticket de la cascade) : sur une erreur eager,
    et sur une erreur mid-stream survenue AVANT le premier token — un flux
    qui a déjà produit du contenu reste compté (décision actée).
    """
    config, ticket = await effective_config(db, auth, _to_core_config(payload.config))
    try:
        events = client.stream(
            _to_core_messages(payload),
            config,
            trace_name=_TRACE_NAME,
            user_id=auth.sub,
        )
    except Exception:
        if ticket is not None:
            await refund_default_quota(db, ticket)
        raise

    async def _encode() -> AsyncIterator[str]:
        # La session `db` reste utilisable ici : les dépendances yield de
        # FastAPI ne sont refermées qu'après l'envoi complet du flux.
        tokens_emitted = False
        try:
            async for event in events:
                if event.type == "token":
                    tokens_emitted = True
                    yield _sse_event("token", {"delta": event.delta})
                else:  # done
                    usage = event.usage.model_dump() if event.usage else None
                    yield _sse_event("done", {"usage": usage})
        except HTTPException as exc:
            # Trop tard pour changer le status HTTP : l'erreur devient un
            # événement SSE portant le status du mapping app/core/ai/errors.py.
            if ticket is not None and not tokens_emitted:
                await refund_default_quota(db, ticket)
            yield _sse_event("error", {"status": exc.status_code, "detail": exc.detail})

    return _encode()
