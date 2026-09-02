"""Socle des contextes d'édition HITL de l'assistant : descripteur + gate.

Un **contexte d'édition** (``block_text``, ``block_exercise``…) est décrit par
un :class:`EditContext` immuable : le type de bloc qu'il édite (validé à la
création de la conversation), son system prompt et ses **tools de
proposition** (:class:`ProposalTool`). Service et streaming ne connaissent que
ce descripteur, résolu par le registre de :mod:`app.course_assistant.editing`
— ajouter un contexte = un module sous ``editing/`` + une entrée au registre,
aucune branche ``if context == …`` ailleurs.

Un tool de proposition **ne mute rien** : la proposition voyage dans les
``args`` du ``tool_call`` (relayés complets sur le flux SSE et persistés dans
``tool_calls`` — :attr:`ProposalTool.rewrite_args` y réécrit les références
courtes en UUID **à l'émission**, le front reçoit un payload directement
applicable), et son exécuteur, après validation, **fige le run** via
:func:`hitl_gate` — seul point d'appel d'``agent_interrupt`` du package —
jusqu'à la décision du professeur, dont le texte EST le résultat du tool
(cf. ``hitl.py``). L'application, elle, reste côté front (routes d'édition
existantes) : la route de décision ne mute jamais le bloc.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.core.ai import AIToolCall, AIToolResult, AIToolSpec, agent_interrupt
from app.course_assistant.refs import CourseRefs

Handler = Callable[[AIToolCall], Awaitable[AIToolResult]]


@dataclass(frozen=True)
class ProposalTool:
    """Un tool HITL d'un contexte d'édition, instancié **par tour** : ``refs``
    (instantané du cours) alimente l'``enum`` de la spec, la résolution des
    arguments du handler et la réécriture des args à l'émission."""

    name: str
    spec: Callable[[CourseRefs], AIToolSpec]
    build_handler: Callable[[CourseRefs], Handler]
    rewrite_args: Callable[[dict, CourseRefs], dict]


@dataclass(frozen=True)
class EditContext:
    """Descripteur d'un contexte d'édition (voir docstring du module)."""

    context: str
    block_type: str
    type_error_detail: str
    system_prompt: str
    tools: tuple[ProposalTool, ...]

    def tool(self, name: str) -> ProposalTool | None:
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None


def hitl_gate(call: AIToolCall, *, accepted_text: str, rejected_text: str) -> AIToolResult:
    """Fige le run (interrupt LangGraph — le flux SSE se ferme) ; à la reprise,
    ``agent_interrupt`` RETOURNE la décision du professeur, dont le texte
    (+ « Son commentaire : … ») est le résultat du tool.

    À appeler APRÈS validation des args : un échec de validation doit répondre
    immédiatement (aucun run figé) — et, le tool étant ré-exécuté depuis le
    début à la reprise, cette validation doit être idempotente.
    """
    decision = agent_interrupt({"tool_call_id": call.id or "?"})
    accepted = isinstance(decision, dict) and bool(decision.get("accepted"))
    comment = decision.get("comment") if isinstance(decision, dict) else None
    content = accepted_text if accepted else rejected_text
    if comment:
        content += f" Son commentaire : {comment}"
    return AIToolResult(content=content)
