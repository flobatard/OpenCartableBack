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

from app.ai.schemas import AIConfigIn, ChatRequest, ChatResponse, ChatUsageRead
from app.core.ai import AIClient, AIRequestConfig, ChatMessage

_TRACE_NAME = "smoke-chat"


def _to_core_config(config: AIConfigIn | None) -> AIRequestConfig | None:
    if config is None:
        return None
    return AIRequestConfig(**config.model_dump())


def _to_core_messages(payload: ChatRequest) -> list[ChatMessage]:
    return [ChatMessage(role=m.role, content=m.content) for m in payload.messages]


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def chat(client: AIClient, payload: ChatRequest, user_sub: str) -> ChatResponse:
    """Appel classique — ``user_sub`` (jamais l'e-mail) part en trace Langfuse."""
    completion = await client.complete(
        _to_core_messages(payload),
        _to_core_config(payload.config),
        trace_name=_TRACE_NAME,
        user_id=user_sub,
    )
    usage = ChatUsageRead(**completion.usage.model_dump()) if completion.usage else None
    return ChatResponse(
        content=completion.content,
        provider=completion.provider,
        model=completion.model,
        usage=usage,
    )


def sse_stream(client: AIClient, payload: ChatRequest, user_sub: str) -> AsyncIterator[str]:
    """Prépare le flux SSE.

    ``client.stream(...)`` est appelé ICI, hors du generator : ses erreurs de
    validation (config absente/invalide) remontent en vraies HTTPException 4xx
    avant que la route ne retourne la ``StreamingResponse``.
    """
    events = client.stream(
        _to_core_messages(payload),
        _to_core_config(payload.config),
        trace_name=_TRACE_NAME,
        user_id=user_sub,
    )

    async def _encode() -> AsyncIterator[str]:
        try:
            async for event in events:
                if event.type == "token":
                    yield _sse_event("token", {"delta": event.delta})
                else:  # done
                    usage = event.usage.model_dump() if event.usage else None
                    yield _sse_event("done", {"usage": usage})
        except HTTPException as exc:
            # Trop tard pour changer le status HTTP : l'erreur devient un
            # événement SSE portant le status du mapping app/core/ai/errors.py.
            yield _sse_event("error", {"status": exc.status_code, "detail": exc.detail})

    return _encode()
