"""Délégation d'une édition par l'assistant global à un sous-assistant.

L'assistant du contexte ``course`` n'écrit jamais lui-même. Quand l'**édition
globale** est activée (``allow_edit`` du tour, opt-in du professeur), il
dispose de deux tools **bloquants** — ``edit_block`` (bloc texte ou exercice)
et ``edit_module`` (module interactif) — qui confient UNE cible et des
**consignes** à un **sous-assistant d'édition** : un run agent à part entière,
bâti sur le descripteur d'édition existant de la cible
(:mod:`app.course_assistant.editing` — system prompt, tools de proposition,
gate HITL, réécriture des références), sur son propre thread checkpointé.

Mécanique (driver de :mod:`app.course_assistant.streaming`) : le handler du
tool valide ses arguments puis **fige le run parent**
(:func:`app.course_assistant.hitl.suspend`, genre
:data:`~app.course_assistant.hitl.KIND_DELEGATION`) ; cet interrupt n'est ni
relayé au front ni enregistré au registre — le driver lance le sous-assistant
dans le même flux SSE (ses événements sont tagués ``agent``), chaque
proposition du sous-assistant suit le flux HITL ordinaire (interrupt enregistré
avec le lien vers le parent, revue et décision du professeur) et, quand le
sous-assistant termine, son **compte rendu** (:func:`recap`) devient la valeur
de reprise du parent — donc le résultat du tool ``edit_*``
(:func:`delegation_result`). Le parent ne voit jamais le contenu proposé :
seulement ce qui a été accepté ou rejeté.

Un seul sous-assistant à la fois : le parent est figé pendant toute sa durée,
et la garde des tools bloquants (:mod:`app.core.ai.agent`) ne retient qu'un
``edit_*`` (ou ``ask_questions``) par réponse du modèle — les autres appels
bloquants de la réponse sont retirés avant l'état (jamais relayés, exécutés ni
revus par le modèle, qui les renouvelle un par un). Le nombre de
délégations d'un tour est plafonné par le driver
(:data:`MAX_DELEGATIONS_PER_TURN` — le plafond de rounds du graphe repart à
chaque reprise, il ne borne pas les délégations).

Les tools sont des :class:`~app.course_assistant.editing.base.ProposalTool`
(spec par tour à ``enum`` des cibles éligibles, handler à validation
idempotente — ré-exécuté à la reprise du parent sur l'instantané rechargé —,
réécriture des args à l'émission : id et contexte de la cible, titre affiché ;
le front reçoit de quoi présenter la délégation et charger la cible de la
revue). Helpers purs, sans I/O.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.ai import AIToolCall, AIToolResult, AIToolSpec
from app.course_assistant import hitl
from app.course_assistant.context import teacher_message
from app.course_assistant.editing.base import (
    Handler,
    ProposalTool,
    ref_schema,
    string_arg,
    tool_error,
)
from app.course_assistant.refs import CourseRefs
from app.models.ai_conversation import CONTEXT_BLOCK_EXERCISE, CONTEXT_BLOCK_TEXT, CONTEXT_MODULE
from app.models.block import TYPE_EXERCISE, TYPE_MODULE, TYPE_TEXT

EDIT_BLOCK = "edit_block"
EDIT_MODULE = "edit_module"

# Consignes d'une délégation (validation, jamais un ``maxLength`` de schéma).
MAX_DELEGATION_INSTRUCTIONS_CHARS = 6_000
# Sous-assistants lancés dans un même tour, reprises comprises.
MAX_DELEGATIONS_PER_TURN = 10
# Message final du sous-assistant, abrégé dans le compte rendu du parent.
RECAP_TEXT_CHARS = 2_000

# Contexte d'édition par type de bloc — seul aiguillage type → contexte.
_CONTEXT_BY_BLOCK_TYPE = {TYPE_TEXT: CONTEXT_BLOCK_TEXT, TYPE_EXERCISE: CONTEXT_BLOCK_EXERCISE}

DELEGATED_NOTE = (
    "Consignes transmises par l'assistant du cours au nom du professeur, qui validera "
    "chaque proposition. Elles se suffisent : en cas de doute, poursuivez avec des "
    "hypothèses raisonnables, signalées dans votre réponse."
)
RECAP_HEAD = "Sous-assistant terminé."
RECAP_NO_PROPOSAL = "Aucune proposition n'a été soumise au professeur."
RECAP_PROPOSALS = "Propositions soumises au professeur :"
RECAP_FOOTER = (
    "Ce qui a été accepté est déjà appliqué au cours ; relisez la cible (`read_block` ou "
    "`read_module`) si la suite dépend de son nouvel état."
)
# Texte clôturant le tour quand le plafond est atteint (lu par le professeur,
# motif ``_TOOL_ROUNDS_EXCEEDED_NOTICE`` du client IA) : le run parent est
# abandonné, jamais repris — repris, il relancerait sans fin.
DELEGATIONS_EXCEEDED_NOTICE = (
    f"\n\n*Plafond de {MAX_DELEGATIONS_PER_TURN} sous-assistants atteint pour ce tour — "
    "relancez une demande au tour suivant.*"
)
INTERRUPTED_TEXT = "Sous-assistant interrompu sans compte rendu."

_INSTRUCTIONS_SCHEMA = {
    "type": "string",
    "description": (
        "Consignes d'édition AUTONOMES pour le sous-assistant, en français : quoi "
        "changer et pourquoi, ce qu'il faut préserver, le niveau et le ton attendus — il "
        "ne voit ni cette conversation ni votre analyse, seulement la cible en entier et "
        "le sommaire du cours."
    ),
}


@dataclass(frozen=True)
class DelegationRequest:
    """Ce que le driver lit dans un interrupt de genre ``delegation`` (payload
    de :func:`~app.course_assistant.hitl.suspend`, clés du ``detail``) :
    l'appel parent à reprendre, le contexte d'édition et la cible."""

    call_id: str
    context: str
    target_id: uuid.UUID
    instructions: str

    @classmethod
    def from_interrupt(cls, value: Mapping[str, Any]) -> "DelegationRequest | None":
        """``None`` si le payload n'a pas la forme attendue (défensif : le
        handler valide avant de figer)."""
        context, instructions = value.get("context"), value.get("instructions")
        if not isinstance(context, str) or not isinstance(instructions, str):
            return None
        try:
            target_id = uuid.UUID(str(value.get("target_id")))
        except ValueError:
            return None
        return cls(
            call_id=str(value.get("tool_call_id") or "?"),
            context=context,
            target_id=target_id,
            instructions=instructions,
        )


def context_for_block(block) -> str | None:
    """Contexte d'édition d'un bloc (``block_text``/``block_exercise``) ;
    ``None`` pour un type sans descripteur (document, pointeur de module)."""
    return _CONTEXT_BY_BLOCK_TYPE.get(block.type)


def _editable_block(block) -> bool:
    return context_for_block(block) is not None


def _edit_block_spec(refs: CourseRefs) -> AIToolSpec:
    return AIToolSpec(
        name=EDIT_BLOCK,
        description=(
            "Confie la modification d'un bloc texte ou exercice du cours à un "
            "sous-assistant d'édition, qui soumet ses propositions au professeur (chacune "
            "acceptée ou rejetée par lui), et ATTEND son compte rendu : le résultat de "
            "l'appel dit ce qui a été accepté ou rejeté. Un seul appel edit_* par "
            "réponse : les suivants d'une même réponse sont ignorés — enchaînez-les un "
            "par un, après chaque compte rendu."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target_ref": ref_schema(
                    "Référence du bloc texte ou exercice à modifier, telle qu'indiquée "
                    "dans le sommaire (ex. B3)",
                    [e.ref for e in refs.entries["block"] if _editable_block(e.entity)],
                ),
                "instructions": _INSTRUCTIONS_SCHEMA,
            },
            "required": ["target_ref", "instructions"],
        },
    )


def _edit_module_spec(refs: CourseRefs) -> AIToolSpec:
    return AIToolSpec(
        name=EDIT_MODULE,
        description=(
            "Confie la modification d'un module interactif du cours (HTML, CSS, "
            "JavaScript) à un sous-assistant d'édition, qui soumet ses propositions au "
            "professeur (chacune acceptée ou rejetée par lui), et ATTEND son compte rendu : "
            "le résultat de l'appel dit ce qui a été accepté ou rejeté. Un seul appel "
            "edit_* par réponse : les suivants d'une même réponse sont ignorés — "
            "enchaînez-les un par un, après chaque compte rendu."
        ),
        parameters={
            "type": "object",
            "properties": {
                "target_ref": ref_schema(
                    "Référence du module à modifier, telle qu'indiquée dans le sommaire (ex. M1)",
                    refs.refs("module"),
                ),
                "instructions": _INSTRUCTIONS_SCHEMA,
            },
            "required": ["target_ref", "instructions"],
        },
    )


def _instructions(arguments: dict) -> tuple[str | None, AIToolResult | None]:
    """Consignes bornées et non vides, ou le résultat d'échec à renvoyer."""
    value, failure = string_arg(
        arguments, "instructions", max_chars=MAX_DELEGATION_INSTRUCTIONS_CHARS, required=True
    )
    if failure is not None:
        return None, failure
    assert value is not None  # ``required=True`` sans échec
    if not value.strip():
        return None, tool_error(
            "Paramètre instructions vide : décrivez au sous-assistant la modification "
            "attendue (quoi, pourquoi, ce qu'il faut préserver)."
        )
    return value, None


def _delegate(
    call: AIToolCall, *, context: str, target_id: uuid.UUID, instructions: str
) -> AIToolResult:
    """Fige le run parent (genre ``delegation``) ; à la reprise, la valeur
    rendue est le compte rendu du sous-assistant (:func:`resume_value`)."""
    value = hitl.suspend(
        call,
        kind=hitl.KIND_DELEGATION,
        detail={"context": context, "target_id": str(target_id), "instructions": instructions},
    )
    return delegation_result(value)


def delegation_result(value: object) -> AIToolResult:
    """Résultat du tool ``edit_*`` depuis la valeur de reprise du parent ; une
    valeur malformée est un échec (défensif — le driver construit la valeur)."""
    if isinstance(value, dict) and isinstance(value.get("text"), str) and value["text"]:
        return AIToolResult(content=value["text"], is_error=not bool(value.get("ok")))
    return AIToolResult(content=INTERRUPTED_TEXT, is_error=True)


def _build_edit_block_handler(refs: CourseRefs) -> Handler:
    async def edit_block(call: AIToolCall) -> AIToolResult:
        # Validation AVANT l'interrupt (échec immédiat, aucun run figé) et
        # idempotente : à la reprise du parent, le tool est ré-exécuté sur
        # l'instantané rechargé.
        resolution = refs.resolve(
            "block", call.arguments.get("target_ref"), eligible=_editable_block
        )
        if resolution.entry is None:
            return tool_error(resolution.error or "Bloc introuvable.")
        block = resolution.entry.entity
        context = context_for_block(block)
        if context is None:
            hint = (
                " — pour un module interactif, utilisez edit_module"
                if block.type == TYPE_MODULE
                else ""
            )
            return tool_error(
                f"Le bloc « {resolution.entry.title} » ({block.type}) n'est pas éditable "
                f"par un sous-assistant : seuls les blocs texte et exercice le sont{hint}."
            )
        instructions, failure = _instructions(call.arguments)
        if failure is not None:
            return failure
        assert instructions is not None
        return _delegate(call, context=context, target_id=block.id, instructions=instructions)

    return edit_block


def _build_edit_module_handler(refs: CourseRefs) -> Handler:
    async def edit_module(call: AIToolCall) -> AIToolResult:
        resolution = refs.resolve("module", call.arguments.get("target_ref"))
        if resolution.entry is None:
            return tool_error(resolution.error or "Module introuvable.")
        instructions, failure = _instructions(call.arguments)
        if failure is not None:
            return failure
        assert instructions is not None
        return _delegate(
            call,
            context=CONTEXT_MODULE,
            target_id=resolution.entry.id,
            instructions=instructions,
        )

    return edit_module


def _rewrite_block_args(arguments: dict, refs: CourseRefs) -> dict:
    """Cible résolue ajoutée aux args relayés et persistés — id du bloc,
    contexte d'édition, titre affiché — : le front présente la délégation et
    charge la cible de la revue sans nouvelle résolution. Cible irrésolue ou
    inéligible : args tels quels (le handler répond par une erreur)."""
    resolution = refs.resolve("block", arguments.get("target_ref"))
    if resolution.entry is None:
        return arguments
    context = context_for_block(resolution.entry.entity)
    if context is None:
        return arguments
    return {
        **arguments,
        "block_id": str(resolution.entry.id),
        "context": context,
        "target_title": resolution.entry.title,
    }


def _rewrite_module_args(arguments: dict, refs: CourseRefs) -> dict:
    resolution = refs.resolve("module", arguments.get("target_ref"))
    if resolution.entry is None:
        return arguments
    return {
        **arguments,
        "module_id": str(resolution.entry.id),
        "context": CONTEXT_MODULE,
        "target_title": resolution.entry.title,
    }


DELEGATION_TOOLS: tuple[ProposalTool, ...] = (
    ProposalTool(
        name=EDIT_BLOCK,
        spec=_edit_block_spec,
        build_handler=_build_edit_block_handler,
        rewrite_args=_rewrite_block_args,
    ),
    ProposalTool(
        name=EDIT_MODULE,
        spec=_edit_module_spec,
        build_handler=_build_edit_module_handler,
        rewrite_args=_rewrite_module_args,
    ),
)

DELEGATION_TOOL_NAMES = frozenset(tool.name for tool in DELEGATION_TOOLS)


def delegated_message(instructions: str) -> str:
    """Message du tour du sous-assistant (après le contexte du tour, cf.
    ``turn_message``) : la note de délégation, puis les consignes sous le
    titre « Demande du professeur » (:func:`~app.course_assistant.context.teacher_message`
    — la règle de structure du tour reste vraie pour lui)."""
    return f"{DELEGATED_NOTE}\n\n{teacher_message(instructions)}"


def outcome_line(name: str, summary: str | None, decision: str) -> str:
    """Une ligne du compte rendu : l'outil de proposition, son résumé, la
    décision du professeur (texte du résultat du tool)."""
    label = f"{name} — « {summary} »" if summary else name
    return f"{label} → {decision}"


def recap(outcomes: Sequence[str], final_text: str) -> str:
    """Compte rendu d'un sous-assistant terminé, lu par le parent : les
    propositions tranchées (:func:`outcome_line`, dans l'ordre), son message
    final abrégé (:data:`RECAP_TEXT_CHARS`), et le rappel que l'accepté est
    déjà appliqué."""
    lines = [RECAP_HEAD]
    if outcomes:
        lines.append(RECAP_PROPOSALS)
        lines.extend(f"{i}. {line}" for i, line in enumerate(outcomes, start=1))
    else:
        lines.append(RECAP_NO_PROPOSAL)
    text = " ".join(final_text.split())
    if text:
        if len(text) > RECAP_TEXT_CHARS:
            text = text[:RECAP_TEXT_CHARS].rstrip() + "…"
        lines.append(f"Message final du sous-assistant : {text}")
    lines.append(RECAP_FOOTER)
    return "\n".join(lines)


def resume_value(text: str, *, ok: bool = True) -> dict[str, Any]:
    """Valeur de reprise du parent (``Command(resume=…)``) : le texte du
    résultat du tool ``edit_*`` et son statut. Clés non hexadécimales —
    LangGraph lirait un dict à clés hex comme une table d'interrupts."""
    return {"ok": ok, "text": text}
