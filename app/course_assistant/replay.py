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
- arguments chaîne longs des appels d'outils **élidés** (au-delà de
  :data:`REPLAY_ARG_CHARS`) : le contenu d'une proposition d'édition passée
  (markdown, code) n'est pas rejoué — l'état courant de la cible est dans le
  contexte du tour. Élidé et non abrégé : rejouer la TÊTE d'un contenu suivie
  d'un marqueur de troncature invite le modèle à la recopier telle quelle au
  tour suivant, marqueur compris, ou à écrire ses propositions en texte plutôt
  qu'en appel d'outil (cf. :data:`ELIDED_ARGUMENT`).

Seule exception aux abréviations et élisions : les réponses du professeur aux questions
de l'assistant (résultats d'``ask_questions``, bornés par les plafonds du
tool) restent entières — elles cadrent toute la suite de la conversation.

Les rounds d'outils issus d'un AUTRE provider — les formats d'id de tool
call ne sont pas interchangeables — ou incomplets (un appel au moins sans
résultat persisté — erreur mid-round, attente HITL abandonnée : un
``tool_call`` non apparié ferait un 400) sont repliés en texte.
"""

import json
from collections.abc import Sequence

from app.core.ai import AIToolCall, ChatMessage
from app.course_assistant.questions import ASK_QUESTIONS
from app.models.ai_message import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER

# Fenêtre de replay (messages persistés) : seuil de troncature et taille
# conservée une fois le seuil dépassé.
REPLAY_MESSAGE_LIMIT = 30
REPLAY_MESSAGE_KEEP = 20
# Plafonds (caractères) d'un résultat d'outil rejoué nativement et d'un
# résultat replié en texte ; seuil au-delà duquel un argument chaîne d'appel
# d'outil est élidé (en deçà — référence, résumé — il passe tel quel).
REPLAY_TOOL_RESULT_CHARS = 1_500
REPLAY_ARG_CHARS = 300
FOLDED_TOOL_RESULT_CHARS = 500

TRUNCATED_HISTORY_NOTICE = (
    "Note : la conversation est longue, seuls ses derniers messages sont rejoués."
)

# Remplace un argument chaîne long au replay. Aucune tête de contenu : rejouée,
# le modèle la recopie au tour suivant (marqueur de troncature compris) au lieu
# de repartir de l'état courant de la cible, et une proposition d'édition sort
# alors amputée — ou rédigée en texte.
ELIDED_ARGUMENT = (
    "[contenu non rejoué ({length} caractères) — repartir de l'état courant "
    "de la cible, donné dans le message du tour]"
)


def abridge(text: str, cap: int, *, what: str) -> str:
    """``text`` s'il tient dans ``cap`` caractères, sinon sa tête suivie d'un
    marqueur explicite (nature ``what`` et longueur d'origine)."""
    if len(text) <= cap:
        return text
    return f"{text[:cap]}… [{what} abrégé au replay : {len(text)} caractères au total]"


def elide(value: str) -> str:
    """``value`` s'il tient dans :data:`REPLAY_ARG_CHARS` caractères, sinon
    :data:`ELIDED_ARGUMENT` — sa tête n'est JAMAIS rejouée (docstring du
    module)."""
    if len(value) <= REPLAY_ARG_CHARS:
        return value
    return ELIDED_ARGUMENT.format(length=len(value))


def _replayed_arguments(arguments: dict) -> dict:
    """Arguments d'un appel d'outil, chaînes longues élidées (les références
    et résumés courts passent tels quels)."""
    return {
        key: elide(value) if isinstance(value, str) else value for key, value in arguments.items()
    }


def _fold_tool_round(assistant_row, tool_rows) -> ChatMessage:
    """Replie en texte un round d'outils issu d'un autre provider, ou
    incomplet (appel sans résultat : rejoué sans issue)."""
    parts = [assistant_row.content] if assistant_row.content else []
    results_by_id = {t.tool_call_id: t for t in tool_rows}
    for call in assistant_row.tool_calls or []:
        name = call.get("name", "?")
        args = json.dumps(_replayed_arguments(call.get("arguments") or {}), ensure_ascii=False)
        result = results_by_id.get(call.get("id"))
        outcome = ""
        if result is not None:
            snippet = result.content
            if name != ASK_QUESTIONS and len(result.content) > FOLDED_TOOL_RESULT_CHARS:
                snippet = result.content[:FOLDED_TOOL_RESULT_CHARS] + "…"
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
    sont rejoués avec résultats abrégés et arguments longs élidés (docstring
    du module).
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
            # interchangeables), ou round incomplet — un appel au moins sans
            # résultat persisté (erreur mid-round, attente HITL abandonnée) :
            # un tool_call non apparié ferait un 400.
            answered = {t.tool_call_id for t in tool_rows}
            incomplete = any(call.get("id") not in answered for call in row.tool_calls)
            if (row.provider and row.provider != current_provider) or incomplete:
                messages.append(_fold_tool_round(row, tool_rows))
            else:
                names_by_id = {call.get("id"): call.get("name") for call in row.tool_calls}
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=row.content,
                        tool_calls=[
                            AIToolCall(
                                id=call.get("id") or "",
                                name=call.get("name", ""),
                                arguments=_replayed_arguments(call.get("arguments") or {}),
                            )
                            for call in row.tool_calls
                        ],
                    )
                )
                messages.extend(
                    ChatMessage(
                        role="tool",
                        content=(
                            t.content
                            if names_by_id.get(t.tool_call_id) == ASK_QUESTIONS
                            else abridge(t.content, REPLAY_TOOL_RESULT_CHARS, what="résultat")
                        ),
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
