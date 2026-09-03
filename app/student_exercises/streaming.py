"""Flux SSE d'un tour du tuteur d'exercice élève.

Contrat SSE — extension du contrat de référence de :mod:`app.ai.service`,
mêmes événements que l'assistant de cours (le parseur front est partagé) :

.. code-block:: text

    event: token         data: {"delta": "…"}
    event: thinking      data: {"delta": "…"}
    event: tool_call     data: {"id": "…", "name": "…", "args": {…}}
    event: tool_result   data: {"id": "…", "name": "…", "is_error": false,
                                "excerpt": "…", "length": 123}
    event: done          data: {"submission_id": "…", "verdict": "correct"|…,
                                "effort": "sufficient"|…|null,
                                "revealed": true|false,
                                "expected_answer": "…"|null, "usage": {…}|null}
    event: error         data: {"status": 503, "detail": "…"}

Verdict structuré : le modèle appelle le tool **`record_verdict`** (verdict,
effort, reveal) AVANT de rédiger son retour ; le tool dépose la décision dans
le holder du tour et invite le modèle à écrire. **Garde serveur** : ``reveal``
n'est honoré que si le verdict est ``correct`` ou l'effort ``sufficient`` ;
sans appel du tool, verdict ``none`` et rien n'est révélé (défaut sûr). Le
corrigé (``expected_answer``) n'est joint au ``done`` que si ``revealed`` —
c'est le seul chemin par lequel il atteint l'élève.

Persistance (:mod:`app.models.exercise_submission`) : la ligne du tour est
insérée + commitée AVANT l'appel provider ; à la clôture (``done`` ou erreur
mid-stream), un UPDATE pose ``feedback`` (texte streamé, partiel sur erreur),
``verdict``/``effort``/``revealed`` et l'usage. Quota (IA par défaut de
l'élève) : remboursé sur erreur eager ou avant le premier token — règles de
:mod:`app.ai_credentials`.
"""

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_credentials.service import effective_config, refund_default_quota
from app.core.ai import AIClient, AIToolCall, AIToolResult, AIToolSpec, ChatMessage
from app.core.auth import AuthenticatedUser
from app.core.storage import Storage
from app.course_assistant.context import build_refs
from app.course_assistant.editing.base import tool_error
from app.course_assistant.refs import CitationRewriter
from app.course_assistant.streaming import TOOL_RESULT_EXCERPT_CHARS, _load_snapshot
from app.course_assistant.tools import build_tool_executor, build_tool_specs
from app.models.exercise_submission import (
    EFFORT_SUFFICIENT,
    EFFORTS,
    VERDICT_CORRECT,
    VERDICT_NONE,
    VERDICTS,
    ExerciseSubmission,
)
from app.models.user import User
from app.student_exercises.context import (
    build_tutor_context,
    history_messages,
    redact_blocks,
    student_message,
)
from app.student_exercises.schemas import SubmissionCreate
from app.student_exercises.service import (
    MAX_TURNS_PER_QUESTION,
    load_exercise,
    load_turns,
    require_question,
)

_TRACE_NAME = "exercise-tutor"
MAX_TOOL_ROUNDS = 5

RECORD_VERDICT = "record_verdict"

RECORD_VERDICT_SPEC = AIToolSpec(
    name=RECORD_VERDICT,
    description=(
        "Enregistre ton évaluation du tour AVANT de rédiger ton retour à l'élève : "
        "verdict sur sa réponse, effort fourni, et si le corrigé du professeur "
        "peut lui être révélé. Appel obligatoire, une fois par tour."
    ),
    parameters={
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": list(VERDICTS),
                "description": (
                    "correct : réponse juste ; partial : partiellement juste ; "
                    "incorrect : fausse ; none : pas une réponse (demande d'aide, "
                    "question sur le cours)"
                ),
            },
            "effort": {
                "type": "string",
                "enum": list(EFFORTS),
                "description": (
                    "sufficient : l'élève a raisonné, essayé, progressé ; "
                    "insufficient : réponse au hasard, sans justification, ou "
                    "simple demande de la solution"
                ),
            },
            "reveal": {
                "type": "boolean",
                "description": (
                    "true UNIQUEMENT si la réponse est juste, ou si l'élève a compris "
                    "l'essentiel et fourni un effort suffisant"
                ),
            },
        },
        "required": ["verdict", "effort", "reveal"],
    },
)


@dataclass
class VerdictHolder:
    """Décision structurée du tour (défaut sûr : rien d'évalué, rien révélé)."""

    verdict: str = VERDICT_NONE
    effort: str | None = None
    reveal: bool = False
    recorded: bool = False


def guard_reveal(verdict: str, effort: str | None, reveal: bool) -> bool:
    """Garde serveur du dévoilement : jamais sans réponse juste ni effort
    suffisant, quoi qu'en dise le modèle."""
    return bool(reveal) and (verdict == VERDICT_CORRECT or effort == EFFORT_SUFFICIENT)


def build_tutor_executor(
    storage: Storage, refs, holder: VerdictHolder
) -> Callable[[AIToolCall], Awaitable[AIToolResult]]:
    """Exécuteur du tour : ``record_verdict`` (holder) prime, les lectures du
    cours sont celles de l'assistant (instantané redacté)."""
    base = build_tool_executor(storage, refs)

    async def _record(call: AIToolCall) -> AIToolResult:
        args = call.arguments or {}
        verdict = args.get("verdict")
        effort = args.get("effort")
        reveal = args.get("reveal")
        if verdict not in VERDICTS:
            return tool_error(f"Paramètre verdict invalide (attendu : {', '.join(VERDICTS)}).")
        if effort not in EFFORTS:
            return tool_error(f"Paramètre effort invalide (attendu : {', '.join(EFFORTS)}).")
        if not isinstance(reveal, bool):
            return tool_error("Paramètre reveal invalide (booléen attendu).")
        holder.verdict = verdict
        holder.effort = effort
        holder.reveal = guard_reveal(verdict, effort, reveal)
        holder.recorded = True
        if holder.reveal:
            hint = (
                "La plateforme affichera le corrigé du professeur sous ton message : "
                "tu peux le commenter, sans le recopier."
            )
        else:
            hint = "Le corrigé reste confidentiel : ne le révèle pas, guide l'élève."
        return AIToolResult(
            content=(
                f"Évaluation enregistrée (verdict : {verdict}, effort : {effort}, "
                f"révélation : {'oui' if holder.reveal else 'non'}). Rédige maintenant "
                f"ton retour à l'élève. {hint}"
            )
        )

    async def _execute(call: AIToolCall) -> AIToolResult:
        if call.name == RECORD_VERDICT:
            return await _record(call)
        return await base(call)

    return _execute


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def sse_stream(
    client: AIClient,
    db: AsyncSession,
    storage: Storage,
    auth: AuthenticatedUser,
    user: User,
    course_id: uuid.UUID,
    token: str | None,
    block_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: SubmissionCreate,
) -> AsyncIterator[str]:
    """Prépare le flux SSE d'un tour (docstring du module). Tout ce qui peut
    échouer en vraie HTTPException est résolu ICI, avant la
    ``StreamingResponse`` : accès au cours/bloc/question (404), plafond du fil
    (422), cascade IA + quota de l'élève (422/429/503 — remboursé sur erreur
    eager), validation eager de ``stream_agent``.

    Ordre des execute : 1) cours (régime public, 1–2), 2) bloc exercice, 3)
    tours de la question, [cascade ``effective_config`` : ses propres
    execute], 4) blocs, 5) ressources, 6) modules, 7) insert du tour puis
    commit. Le generator retourné pose l'UPDATE final à la clôture.
    """
    course, block = await load_exercise(db, course_id, token, block_id)
    question_number, question = require_question(block, question_id)
    turns = await load_turns(db, user, block, question_id)
    if len(turns) >= MAX_TURNS_PER_QUESTION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Fil de la question plein",
        )

    config, ticket = await effective_config(db, auth, None)

    blocks, resources, modules = await _load_snapshot(db, course)
    # Redaction structurelle : aucun corrigé dans l'instantané (contexte ET
    # tools) — seul celui de la question cible est joint au prompt.
    redacted = redact_blocks(blocks)
    focus = next(b for b in redacted if b.id == block.id)
    refs = build_refs(redacted, resources, modules)
    system_content = build_tutor_context(
        course,
        refs,
        block=focus,
        question_number=question_number,
        expected_answer=question.get("expected_answer"),
    )
    model_messages = [
        ChatMessage(role="system", content=system_content),
        *history_messages(turns),
        ChatMessage(role="user", content=student_message(payload.kind, payload.content)),
    ]

    holder = VerdictHolder()
    executor = build_tutor_executor(storage, refs, holder)

    submission_id = uuid.uuid4()
    await db.execute(
        insert(ExerciseSubmission).values(
            id=submission_id,
            user_id=user.id,
            course_id=course.id,
            block_id=block.id,
            question_id=question_id,
            kind=payload.kind,
            content=payload.content,
        )
    )
    await db.commit()

    try:
        events = client.stream_agent(
            model_messages,
            config,
            tools=[*build_tool_specs(refs), RECORD_VERDICT_SPEC],
            tool_executor=executor,
            max_tool_rounds=MAX_TOOL_ROUNDS,
            trace_name=_TRACE_NAME,
            user_id=auth.sub,
        )
    except Exception:
        if ticket is not None:
            await refund_default_quota(db, ticket)
        raise

    return _encode_turn(
        db=db,
        events=events,
        refs=refs,
        holder=holder,
        submission_id=submission_id,
        expected_answer=question.get("expected_answer"),
        ticket=ticket,
    )


async def _encode_turn(
    *,
    db: AsyncSession,
    events: AsyncIterator[Any],
    refs: Any,
    holder: VerdictHolder,
    submission_id: uuid.UUID,
    expected_answer: str | None,
    ticket: Any,
) -> AsyncIterator[str]:
    """Encode le flux agent en SSE et complète la ligne du tour à la clôture
    (la session ``db`` reste utilisable : dépendance yield refermée après
    l'envoi complet)."""
    tokens_emitted = False
    all_text: list[str] = []
    rewriter = CitationRewriter(refs)

    def _emit_text(text: str) -> str | None:
        if not text:
            return None
        all_text.append(text)
        return _sse_event("token", {"delta": text})

    async def _finalize(usage: dict[str, Any] | None) -> None:
        await db.execute(
            update(ExerciseSubmission)
            .where(ExerciseSubmission.id == submission_id)
            .values(
                feedback="".join(all_text),
                verdict=holder.verdict,
                effort=holder.effort,
                revealed=holder.reveal,
                input_tokens=(usage or {}).get("input_tokens"),
                output_tokens=(usage or {}).get("output_tokens"),
            )
        )
        await db.commit()

    try:
        async for event in events:
            if event.type == "token":
                tokens_emitted = True
                sse = _emit_text(rewriter.feed(event.delta))
                if sse is not None:
                    yield sse
            elif event.type == "thinking":
                yield _sse_event("thinking", {"delta": event.delta})
            elif event.type == "tool_call":
                held = _emit_text(rewriter.flush())
                if held is not None:
                    yield held
                call = event.tool_call
                yield _sse_event(
                    "tool_call", {"id": call.id, "name": call.name, "args": call.arguments}
                )
            elif event.type == "tool_result":
                held = _emit_text(rewriter.flush())
                if held is not None:
                    yield held
                yield _sse_event(
                    "tool_result",
                    {
                        "id": event.tool_call.id,
                        "name": event.tool_call.name,
                        "is_error": bool(event.tool_result_error),
                        "excerpt": event.delta[:TOOL_RESULT_EXCERPT_CHARS],
                        "length": len(event.delta),
                    },
                )
            elif event.type == "done":
                held = _emit_text(rewriter.flush())
                if held is not None:
                    yield held
                usage = event.usage.model_dump() if event.usage else None
                await _finalize(usage)
                yield _sse_event(
                    "done",
                    {
                        "submission_id": str(submission_id),
                        "verdict": holder.verdict,
                        "effort": holder.effort,
                        "revealed": holder.reveal,
                        "expected_answer": (
                            (expected_answer or None) if holder.reveal else None
                        ),
                        "usage": usage,
                    },
                )
            # ``interrupt`` n'existe pas ici (aucun tool HITL) : ignoré.
    except HTTPException as exc:
        if ticket is not None and not tokens_emitted:
            await refund_default_quota(db, ticket)
        held = _emit_text(rewriter.flush())
        if held is not None:
            yield held
        try:
            await _finalize(None)
        except Exception:  # noqa: BLE001 — best-effort assumé
            pass
        yield _sse_event("error", {"status": exc.status_code, "detail": exc.detail})
