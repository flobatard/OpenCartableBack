"""Encodage SSE d'un tour d'agent — boucle partagée par l'assistant de cours
et le tuteur d'exercice.

Les deux flux relaient les mêmes événements (contrat de :mod:`app.core.sse`,
extension agent) et ne diffèrent que par **ce qu'ils persistent** et le payload
de ``done`` : cette partie variable est un :class:`TurnSink` ; l'encodeur tient
la boucle, la réécriture des citations, l'ordre d'émission et le remboursement
du quota.

Ordre d'émission garanti (contrat des tests) :

- ``token`` : le delta passe par le :class:`~app.course_assistant.refs.CitationRewriter`
  (citations ``oc-block:B3`` réécrites en UUID) ; le texte prêt part en ``token``
  et est remis au sink — texte streamé = texte persisté ;
- avant tout autre événement (sauf ``thinking``), le texte retenu par le
  rewriter est flushé et émis d'abord ;
- ``tool_call`` : args relayés tels que le sink les rend (réécrits ou non) ;
- ``tool_result`` : extrait borné (:data:`TOOL_RESULT_EXCERPT_CHARS`) + longueur,
  jamais le contenu complet ;
- ``interrupt`` : si le sink rend un payload, l'événement est émis et le flux
  SE FERME sans ``done`` ; ``None`` = ignoré, le flux continue ;
- ``done`` : le sink persiste et fournit le payload ;
- ``HTTPException`` mid-stream (200 déjà parti) : remboursement du quota ssi
  aucun token n'est encore sorti, texte retenu flushé, persistance best-effort
  du partiel (``sink.failed``), puis ``error`` portant le status du mapping de
  :mod:`app.core.ai.errors`.

La session ``db`` reste utilisable pendant le flux : les dépendances ``yield``
de FastAPI ne sont refermées qu'après l'envoi complet.
"""

from collections.abc import AsyncIterator
from typing import Any, Protocol

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_credentials.service import QuotaTicket, refund_default_quota
from app.core.ai import AIStreamEvent, AIToolCall
from app.core.sse import sse_event
from app.course_assistant.refs import CitationRewriter, CourseRefs

# Extrait d'un résultat d'outil relayé sur le flux (le contenu complet, lui,
# n'est servi que par le détail de conversation) — même valeur côté front.
TOOL_RESULT_EXCERPT_CHARS = 400


class TurnSink(Protocol):
    """La partie variable d'un tour : accumulation et persistance."""

    def text(self, delta: str) -> None:
        """Texte prêt à partir (citations résolues), à accumuler."""

    def tool_call(self, call: AIToolCall) -> dict[str, Any]:
        """Enregistre l'appel ; rend les args à relayer sur le flux."""

    def tool_result(self, event: AIStreamEvent) -> None:
        """Enregistre le résultat complet (le flux n'en porte qu'un extrait)."""

    async def interrupt(self, event: AIStreamEvent) -> dict[str, Any] | None:
        """Payload de l'événement ``interrupt``, ou ``None`` pour l'ignorer."""

    async def done(self, usage: dict[str, Any] | None) -> dict[str, Any]:
        """Persiste le tour ; rend le payload de ``done``."""

    async def failed(self) -> None:
        """Persiste le partiel après une erreur mid-stream (best-effort)."""


async def encode_turn(
    events: AsyncIterator[AIStreamEvent],
    *,
    db: AsyncSession,
    refs: CourseRefs,
    ticket: QuotaTicket | None,
    sink: TurnSink,
) -> AsyncIterator[str]:
    """Le flux SSE d'un tour (docstring du module)."""
    rewriter = CitationRewriter(refs)
    tokens_emitted = False

    def _ready(text: str) -> str | None:
        """Texte prêt (citations résolues) : remis au sink, rendu en ``token``."""
        if not text:
            return None
        sink.text(text)
        return sse_event("token", {"delta": text})

    try:
        async for event in events:
            if event.type == "token":
                tokens_emitted = True
                sse = _ready(rewriter.feed(event.delta))
                if sse is not None:
                    yield sse
                continue
            if event.type == "thinking":
                yield sse_event("thinking", {"delta": event.delta})
                continue
            # Tout autre événement : le texte retenu par le rewriter part d'abord
            # (ordre d'affichage côté front).
            held = _ready(rewriter.flush())
            if held is not None:
                yield held
            if event.type == "tool_call":
                call = event.tool_call
                arguments = sink.tool_call(call)
                yield sse_event(
                    "tool_call", {"id": call.id, "name": call.name, "args": arguments}
                )
            elif event.type == "tool_result":
                sink.tool_result(event)
                yield sse_event(
                    "tool_result",
                    {
                        "id": event.tool_call.id,
                        "name": event.tool_call.name,
                        "is_error": bool(event.tool_result_error),
                        "excerpt": event.delta[:TOOL_RESULT_EXCERPT_CHARS],
                        "length": len(event.delta),
                    },
                )
            elif event.type == "interrupt":
                payload = await sink.interrupt(event)
                if payload is not None:
                    yield sse_event("interrupt", payload)
                    return
            else:  # done
                usage = event.usage.model_dump() if event.usage else None
                yield sse_event("done", await sink.done(usage))
    except HTTPException as exc:
        if ticket is not None and not tokens_emitted:
            await refund_default_quota(db, ticket)
        held = _ready(rewriter.flush())
        if held is not None:
            yield held
        try:
            await sink.failed()
        except Exception:  # noqa: BLE001 — best-effort : ne jamais masquer l'erreur provider
            pass
        yield sse_event("error", {"status": exc.status_code, "detail": exc.detail})
