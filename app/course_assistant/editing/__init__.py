"""Contextes d'édition HITL de l'assistant de cours — registre.

Chaque contexte d'édition est un module de ce package exposant un
:class:`EditContext` (docstring de :mod:`app.course_assistant.editing.base`) ;
:data:`EDIT_CONTEXTS` les indexe par nom de contexte et
:func:`edit_context_for` est le seul point de résolution utilisé par le
service et le streaming (``None`` = contexte sans édition, ``course``).
"""

from app.course_assistant.editing.base import EditContext, Handler, ProposalTool, hitl_gate
from app.course_assistant.editing.block_exercise import BLOCK_EXERCISE
from app.course_assistant.editing.block_text import BLOCK_TEXT

EDIT_CONTEXTS: dict[str, EditContext] = {
    BLOCK_TEXT.context: BLOCK_TEXT,
    BLOCK_EXERCISE.context: BLOCK_EXERCISE,
}


def edit_context_for(context: str) -> EditContext | None:
    """Descripteur du contexte d'édition ; ``None`` pour un contexte sans
    édition (``course``) ou inconnu."""
    return EDIT_CONTEXTS.get(context)


__all__ = [
    "EDIT_CONTEXTS",
    "EditContext",
    "Handler",
    "ProposalTool",
    "edit_context_for",
    "hitl_gate",
]
