"""Helpers PURS du tuteur d'exercice : redaction du cours, contexte, historique.

Aucune I/O ici. Le principe de sécurité du tuteur est **structurel** : le
modèle ne voit qu'un instantané du cours de grade public — les blocs sont
passés par :func:`app.public.service.public_content` (celui-là même qui sert
les élèves), donc **aucun ``expected_answer``** n'existe dans le contexte, ni
dans ce que rendent les tools de lecture (``read_block`` lit ces mêmes
copies). Seul le corrigé de la **question cible** est ajouté, dans une section
dédiée explicitement confidentielle.

Le contexte du tour (:func:`build_tutor_context`) voyage en tête du message
utilisateur (:func:`~app.course_assistant.context.turn_message`), après
l'historique : le system prompt (:mod:`app.student_exercises.prompts`) reste
statique. Le cours n'y figure qu'en **sommaire** (jamais le contenu des
autres blocs, lisibles avec ``read_block``).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.ai import ChatMessage
from app.course_assistant.context import render_outline
from app.course_assistant.refs import CourseRefs
from app.course_assistant.render import format_block
from app.course_assistant.replay import abridge
from app.models.exercise_submission import KIND_ANSWER
from app.public.service import public_content

# Fenêtre des tours du fil rejoués au modèle (hystérésis, motif
# REPLAY_MESSAGE_LIMIT/KEEP de l'assistant) et plafond (caractères) d'un
# message ou retour rejoué.
HISTORY_TURN_LIMIT = 12
HISTORY_TURN_KEEP = 8
HISTORY_MESSAGE_CHARS = 2_000

FOCUS_NOTE = "exercice en cours de résolution"

NO_EXPECTED_ANSWER_NOTICE = (
    "(le professeur n'a pas renseigné de corrigé pour cette question : juge "
    "sur le fond, avec prudence, et ne révèle rien)"
)


@dataclass(frozen=True)
class RedactedBlock:
    """Copie d'un bloc au content de grade public (attributs lus par
    ``format_block``/``build_refs`` — jamais l'entité ORM)."""

    id: uuid.UUID
    course_id: uuid.UUID
    position: int
    type: str
    title: str | None
    description: str | None
    content: dict
    resource_id: uuid.UUID | None
    module_id: uuid.UUID | None


def redact_blocks(blocks: Sequence) -> list[RedactedBlock]:
    """Instantané des blocs sans aucun corrigé (ordre conservé)."""
    return [
        RedactedBlock(
            id=b.id,
            course_id=b.course_id,
            position=b.position,
            type=b.type,
            title=b.title,
            description=b.description,
            content=public_content(b.type, b.content or {}),
            resource_id=b.resource_id,
            module_id=b.module_id,
        )
        for b in blocks
    ]


def find_question(block, question_id: uuid.UUID) -> tuple[int, dict] | None:
    """``(numéro 1-based, question)`` d'une question du bloc, ``None`` si absente
    (numérotation = ordre du content, celle affichée à l'élève et rendue par
    ``format_block``)."""
    questions = [q for q in (block.content or {}).get("questions", []) if isinstance(q, dict)]
    for i, question in enumerate(questions, start=1):
        if str(question.get("id")) == str(question_id):
            return i, question
    return None


def build_tutor_context(
    course,
    refs: CourseRefs,
    *,
    block: RedactedBlock,
    question_number: int,
    expected_answer: str | None,
) -> str:
    """Contexte du tour du tuteur : exercice en cours (redacté, en entier,
    question cible désignée), corrigé confidentiel de CETTE question, puis le
    sommaire du cours (l'exercice y est remplacé par un pointeur)."""
    sections = [
        "## Exercice en cours de résolution",
        format_block(block, refs),
        f"L'élève travaille sur la **Question {question_number}** de cet exercice.",
        f"## Corrigé confidentiel de la question {question_number} (professeur)",
        expected_answer.strip() if expected_answer and expected_answer.strip()
        else NO_EXPECTED_ANSWER_NOTICE,
        render_outline(course, refs, focus_block=block, focus_note=FOCUS_NOTE),
    ]
    return "\n\n".join(sections)


def student_message(kind: str, content: str) -> str:
    """Message user d'un tour : la nature du tour est explicitée au modèle."""
    label = "Réponse de l'élève" if kind == KIND_ANSWER else "Message de l'élève"
    return f"{label} :\n\n{content}"


def history_messages(
    rows: Sequence, *, limit: int = HISTORY_TURN_LIMIT, keep: int = HISTORY_TURN_KEEP
) -> list[ChatMessage]:
    """Fil précédent de la question rejoué en messages user/assistant — au-delà
    de ``limit`` tours, seuls les ``keep`` derniers (fenêtre à hystérésis :
    préfixe stable pour le cache) ; un tour sans ``feedback`` (échec provider)
    n'a que son message élève ; messages et retours abrégés au-delà de
    :data:`HISTORY_MESSAGE_CHARS`."""
    window = rows[-min(keep, limit) :] if len(rows) > limit else rows
    messages: list[ChatMessage] = []
    for row in window:
        content = abridge(row.content, HISTORY_MESSAGE_CHARS, what="message")
        messages.append(ChatMessage(role="user", content=student_message(row.kind, content)))
        if row.feedback:
            feedback = abridge(row.feedback, HISTORY_MESSAGE_CHARS, what="retour")
            messages.append(ChatMessage(role="assistant", content=feedback))
    return messages
