"""Conversions entre les types neutres du package et les messages LangChain.

Seul point de contact ``ChatMessage`` ↔ ``BaseMessage`` (imports langchain
paresseux, motif du package) ; le découpage des chunks de modèle en
événements ``thinking``/``token`` et la normalisation de l'usage vivent ici.
"""

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

from app.core.ai.types import AIStreamEvent, AIUsage, ChatMessage

if TYPE_CHECKING:  # uniquement pour les annotations — jamais importé au runtime
    from langchain_core.messages import AIMessageChunk, BaseMessage


def to_langchain_messages(messages: Sequence[ChatMessage]) -> list["BaseMessage"]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    converted: list[BaseMessage] = []
    for m in messages:
        if m.role == "tool":
            converted.append(
                ToolMessage(
                    content=m.content,
                    tool_call_id=m.tool_call_id or "",
                    status="error" if m.is_error else "success",
                )
            )
        elif m.role == "assistant" and m.tool_calls:
            converted.append(
                AIMessage(
                    content=m.content,
                    tool_calls=[
                        {"name": tc.name, "args": tc.arguments, "id": tc.id or None}
                        for tc in m.tool_calls
                    ],
                )
            )
        else:
            lc_classes = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
            converted.append(lc_classes[m.role](content=m.content))
    return converted


def delta_events(chunk: "AIMessageChunk") -> Iterator[AIStreamEvent]:
    """Événements ``thinking``/``token`` portés par un chunk de modèle.

    Les blocs « reasoning » sont lus via ``content_blocks`` (représentation
    standardisée de langchain-core 1.x, tous providers) ; ``chunk.text`` ne
    contient jamais le raisonnement.
    """
    for block in chunk.content_blocks:
        if block.get("type") == "reasoning":
            reasoning = block.get("reasoning", "")
            if reasoning:
                yield AIStreamEvent(type="thinking", delta=reasoning)
    if chunk.text:
        yield AIStreamEvent(type="token", delta=chunk.text)


def to_usage(usage_metadata: dict[str, Any] | None) -> AIUsage | None:
    """Normalise l'``usage_metadata`` LangChain (absent chez certains providers)."""
    if not usage_metadata:
        return None
    return AIUsage(
        input_tokens=usage_metadata.get("input_tokens"),
        output_tokens=usage_metadata.get("output_tokens"),
    )
