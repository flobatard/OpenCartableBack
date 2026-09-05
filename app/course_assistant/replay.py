"""Replay de l'historique persisté d'une conversation vers le modèle (pur).

Troncature aux frontières de round (jamais un tour ``tool`` orphelin en tête),
et repli en texte des rounds d'outils issus d'un AUTRE provider — les formats
d'id de tool call ne sont pas interchangeables entre providers — ou
incomplets (résultats jamais persistés : des ``tool_calls`` non appariés
feraient un 400).
"""

import json
from collections.abc import Sequence

from app.core.ai import AIToolCall, ChatMessage
from app.models.ai_message import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER

# Nombre max de messages persistés rejoués au modèle, et taille max d'un
# contenu d'outil replié en texte (round issu d'un autre provider).
REPLAY_MESSAGE_LIMIT = 30
FOLDED_TOOL_RESULT_CHARS = 500

TRUNCATED_HISTORY_NOTICE = (
    "\n\nNote : la conversation est longue, seuls ses derniers messages vous "
    "sont rejoués."
)


def _fold_tool_round(assistant_row, tool_rows) -> ChatMessage:
    """Replie en texte un round d'outils issu d'un autre provider."""
    parts = [assistant_row.content] if assistant_row.content else []
    results_by_id = {t.tool_call_id: t for t in tool_rows}
    for call in assistant_row.tool_calls or []:
        name = call.get("name", "?")
        args = json.dumps(call.get("arguments", {}), ensure_ascii=False)
        result = results_by_id.get(call.get("id"))
        outcome = ""
        if result is not None:
            snippet = result.content[:FOLDED_TOOL_RESULT_CHARS]
            if len(result.content) > FOLDED_TOOL_RESULT_CHARS:
                snippet += "…"
            state = "échec" if result.is_error else "résultat"
            outcome = f" → {state} : {snippet}"
        parts.append(f"[Outil {name}({args}){outcome}]")
    return ChatMessage(role="assistant", content="\n\n".join(parts))


def replay_messages(
    rows: Sequence, current_provider: str, *, limit: int = REPLAY_MESSAGE_LIMIT
) -> tuple[list[ChatMessage], bool]:
    """Historique à rejouer au modèle depuis les lignes ``ai_messages`` triées.

    Retourne ``(messages, truncated)``. Troncature aux ``limit`` derniers
    messages **sans couper un round** : les tours ``tool`` orphelins de tête
    (leur assistant est hors fenêtre) sont écartés. Les rounds d'outils générés
    par un AUTRE provider que ``current_provider`` sont repliés en texte
    (:func:`_fold_tool_round`) au lieu d'être rejoués nativement.
    """
    truncated = len(rows) > limit
    window = list(rows[-limit:])
    while window and window[0].role == ROLE_TOOL:
        window.pop(0)

    messages: list[ChatMessage] = []
    i = 0
    while i < len(window):
        row = window[i]
        if row.role == ROLE_USER:
            messages.append(ChatMessage(role="user", content=row.content))
            i += 1
            continue
        if row.role == ROLE_ASSISTANT and row.tool_calls:
            tool_rows = []
            j = i + 1
            while j < len(window) and window[j].role == ROLE_TOOL:
                tool_rows.append(window[j])
                j += 1
            # Repli en texte : round d'un autre provider (ids de tool call non
            # interchangeables), ou round incomplet (résultats jamais persistés
            # — erreur mid-round : des tool_calls non appariés feraient un 400).
            if (row.provider and row.provider != current_provider) or not tool_rows:
                messages.append(_fold_tool_round(row, tool_rows))
            else:
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=row.content,
                        tool_calls=[
                            AIToolCall(
                                id=call.get("id") or "",
                                name=call.get("name", ""),
                                arguments=call.get("arguments") or {},
                            )
                            for call in row.tool_calls
                        ],
                    )
                )
                messages.extend(
                    ChatMessage(
                        role="tool",
                        content=t.content,
                        tool_call_id=t.tool_call_id or "",
                        is_error=t.is_error,
                    )
                    for t in tool_rows
                )
            i = j
            continue
        # Assistant sans tool_calls (ou ligne inattendue) : texte simple.
        messages.append(ChatMessage(role="assistant", content=row.content))
        i += 1
    return messages, truncated
