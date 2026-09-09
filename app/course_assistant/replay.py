"""Replay de l'historique persisté d'une conversation vers le modèle (pur).

Trois économies, toutes **déterministes** (la même ligne donne le même
message d'un tour à l'autre : le préfixe rejoué reste stable pour le cache de
prompt du provider) :

- fenêtre à hystérésis : au-delà de :data:`REPLAY_MESSAGE_LIMIT` messages,
  seuls les :data:`REPLAY_MESSAGE_KEEP` derniers sont rejoués — la fenêtre ne
  glisse pas à chaque tour, elle saute ; troncature aux frontières de round
  (jamais un tour ``tool`` orphelin en tête) ;
- résultats d'outils **abrégés** (:data:`REPLAY_TOOL_RESULT_CHARS`) : un bloc,
  un PDF ou un module lu à un tour précédent n'est jamais renvoyé en entier —
  le modèle relit au besoin ;
- arguments chaîne des appels d'outils **abrégés** (:data:`REPLAY_ARG_CHARS`) :
  le contenu intégral d'une proposition d'édition passée (markdown, code)
  n'est pas rejoué — l'état courant de la cible est dans le contexte du tour.

Les rounds d'outils issus d'un AUTRE provider — les formats d'id de tool
call ne sont pas interchangeables — ou incomplets (résultats jamais
persistés : des ``tool_calls`` non appariés feraient un 400) sont repliés en
texte.
"""

import json
from collections.abc import Sequence

from app.core.ai import AIToolCall, ChatMessage
from app.models.ai_message import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER

# Fenêtre de replay (messages persistés) : seuil de troncature et taille
# conservée une fois le seuil dépassé.
REPLAY_MESSAGE_LIMIT = 30
REPLAY_MESSAGE_KEEP = 20
# Plafonds (caractères) d'un résultat d'outil rejoué nativement, d'un
# argument chaîne d'appel d'outil, et d'un résultat replié en texte.
REPLAY_TOOL_RESULT_CHARS = 1_500
REPLAY_ARG_CHARS = 300
FOLDED_TOOL_RESULT_CHARS = 500

TRUNCATED_HISTORY_NOTICE = (
    "Note : la conversation est longue, seuls ses derniers messages sont rejoués."
)


def abridge(text: str, cap: int, *, what: str) -> str:
    """``text`` s'il tient dans ``cap`` caractères, sinon sa tête suivie d'un
    marqueur explicite (nature ``what`` et longueur d'origine)."""
    if len(text) <= cap:
        return text
    return f"{text[:cap]}… [{what} abrégé au replay : {len(text)} caractères au total]"


def _abridged_arguments(arguments: dict) -> dict:
    """Arguments d'un appel d'outil, chaînes longues abrégées (les références
    et résumés courts passent tels quels)."""
    return {
        key: abridge(value, REPLAY_ARG_CHARS, what="argument") if isinstance(value, str) else value
        for key, value in arguments.items()
    }


def _fold_tool_round(assistant_row, tool_rows) -> ChatMessage:
    """Replie en texte un round d'outils issu d'un autre provider."""
    parts = [assistant_row.content] if assistant_row.content else []
    results_by_id = {t.tool_call_id: t for t in tool_rows}
    for call in assistant_row.tool_calls or []:
        name = call.get("name", "?")
        args = json.dumps(_abridged_arguments(call.get("arguments") or {}), ensure_ascii=False)
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
    rows: Sequence,
    current_provider: str,
    *,
    limit: int = REPLAY_MESSAGE_LIMIT,
    keep: int = REPLAY_MESSAGE_KEEP,
) -> tuple[list[ChatMessage], bool]:
    """Historique à rejouer au modèle depuis les lignes ``ai_messages`` triées.

    Retourne ``(messages, truncated)``. Au-delà de ``limit`` messages, seuls
    les ``keep`` derniers sont rejoués (``keep`` est borné par ``limit``),
    **sans couper un round** : les tours ``tool`` orphelins de tête (leur
    assistant est hors fenêtre) sont écartés. Les rounds d'outils générés par
    un AUTRE provider que ``current_provider`` sont repliés en texte
    (:func:`_fold_tool_round`) au lieu d'être rejoués nativement ; les autres
    sont rejoués avec résultats et arguments abrégés (docstring du module).
    """
    truncated = len(rows) > limit
    window = list(rows[-min(keep, limit) :]) if truncated else list(rows)
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
                                arguments=_abridged_arguments(call.get("arguments") or {}),
                            )
                            for call in row.tool_calls
                        ],
                    )
                )
                messages.extend(
                    ChatMessage(
                        role="tool",
                        content=abridge(t.content, REPLAY_TOOL_RESULT_CHARS, what="résultat"),
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
