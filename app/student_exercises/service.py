"""Chargements et lecture du fil — tuteur d'exercice élève.

Autorisation en deux temps, routes élève : le JWT identifie l'élève
(``get_or_create_by_sub`` dans le router), puis l'accès au COURS est celui du
régime public — :func:`app.public.access.get_public_course` (visibilité +
token de partage ``?token=``, 404 uniforme). Le bloc doit être un exercice du
cours et la question exister dans son content (404 sinon).

Routes professeur (résumé et effacement des tentatives de TOUS les élèves) :
scopées au propriétaire du cours (:func:`app.courses.queries.get_owned_course`
— 404 jamais 403), même sélection du bloc exercice.

L'ordre des ``execute`` de chaque fonction est un contrat des tests (fausse
session FIFO).
"""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.http import not_found
from app.courses.queries import get_owned_course
from app.models.block import TYPE_EXERCISE, Block
from app.models.course import Course
from app.models.exercise_submission import ExerciseSubmission
from app.models.user import User
from app.public.access import get_public_course
from app.student_exercises.context import find_question
from app.student_exercises.schemas import (
    QuestionThreadRead,
    SubmissionRead,
    SubmissionsRead,
    SubmissionSummaryRead,
)

# Garde-fou par question (422 au-delà) : un fil n'est pas infini.
MAX_TURNS_PER_QUESTION = 100


async def _select_exercise(db: AsyncSession, course: Course, block_id: uuid.UUID) -> Block:
    """Bloc exercice scopé au cours (1 execute) — 404 « Bloc introuvable » si
    absent ou d'un autre type."""
    block = (
        (
            await db.execute(
                select(Block).where(
                    Block.id == block_id,
                    Block.course_id == course.id,
                    Block.type == TYPE_EXERCISE,
                )
            )
        )
        .scalars()
        .one_or_none()
    )
    if block is None:
        raise not_found("Bloc introuvable")
    return block


async def load_exercise(
    db: AsyncSession, course_id: uuid.UUID, token: str | None, block_id: uuid.UUID
) -> tuple[Course, Block]:
    """Régime élève : cours (public, 1 ou 2 execute) puis bloc exercice (1)."""
    course = await get_public_course(db, course_id, token)
    return course, await _select_exercise(db, course, block_id)


async def load_owned_exercise(
    db: AsyncSession, user: User, course_id: uuid.UUID, block_id: uuid.UUID
) -> Block:
    """Régime professeur : cours du propriétaire (1 execute) puis bloc exercice (1)."""
    course = await get_owned_course(db, user, course_id)
    return await _select_exercise(db, course, block_id)


def require_question(block: Block, question_id: uuid.UUID) -> tuple[int, dict]:
    """``(numéro, question)`` — 404 « Question introuvable » sinon."""
    found = find_question(block, question_id)
    if found is None:
        raise not_found("Question introuvable")
    return found


async def load_turns(
    db: AsyncSession, user: User, block: Block, question_id: uuid.UUID | None = None
) -> list[ExerciseSubmission]:
    """Tours de l'élève sur le bloc (ou sur UNE question), tri ``created_at, id``
    — 1 execute."""
    stmt = select(ExerciseSubmission).where(
        ExerciseSubmission.user_id == user.id,
        ExerciseSubmission.block_id == block.id,
    )
    if question_id is not None:
        stmt = stmt.where(ExerciseSubmission.question_id == question_id)
    stmt = stmt.order_by(ExerciseSubmission.created_at, ExerciseSubmission.id)
    return list((await db.execute(stmt)).scalars().all())


def submission_read(row) -> SubmissionRead:
    return SubmissionRead(
        id=row.id,
        kind=row.kind,
        content=row.content,
        feedback=row.feedback,
        verdict=row.verdict,
        effort=row.effort,
        revealed=bool(row.revealed),
        created_at=row.created_at,
    )


async def list_threads(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    token: str | None,
    block_id: uuid.UUID,
) -> SubmissionsRead:
    """Fils de l'élève sur toutes les questions du bloc. Ordre des execute :
    cours (1–2), bloc, tours. Le corrigé d'une question n'est joint que si un
    tour du fil l'a révélé — relu dans le bloc (jamais copié en table)."""
    _course, block = await load_exercise(db, course_id, token, block_id)
    rows = await load_turns(db, user, block)
    by_question: dict[str, list] = {}
    for row in rows:
        by_question.setdefault(str(row.question_id), []).append(row)
    questions: dict[str, QuestionThreadRead] = {}
    for qid, turns in by_question.items():
        revealed_answer = None
        if any(t.revealed for t in turns):
            found = find_question(block, uuid.UUID(qid))
            if found is not None:
                revealed_answer = found[1].get("expected_answer") or None
        questions[qid] = QuestionThreadRead(
            turns=[submission_read(t) for t in turns],
            revealed_answer=revealed_answer,
        )
    return SubmissionsRead(questions=questions)


async def delete_turns(
    db: AsyncSession, user: User, block: Block, question_id: uuid.UUID | None
) -> int:
    """Efface les tours de l'élève sur le bloc — ou sur UNE question — (1
    delete + commit) ; retourne le nombre de lignes effacées. Aucune
    validation de la question : une question disparue du bloc garde ses tours
    effaçables."""
    stmt = delete(ExerciseSubmission).where(
        ExerciseSubmission.user_id == user.id,
        ExerciseSubmission.block_id == block.id,
    )
    if question_id is not None:
        stmt = stmt.where(ExerciseSubmission.question_id == question_id)
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount or 0


async def delete_submissions(
    db: AsyncSession, block: Block, question_id: uuid.UUID | None
) -> int:
    """Professeur : efface les tours de TOUS les élèves sur le bloc — ou sur
    UNE question — (1 delete + commit) ; retourne le nombre effacé."""
    stmt = delete(ExerciseSubmission).where(ExerciseSubmission.block_id == block.id)
    if question_id is not None:
        stmt = stmt.where(ExerciseSubmission.question_id == question_id)
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount or 0


async def submission_summary(db: AsyncSession, block: Block) -> SubmissionSummaryRead:
    """Professeur : nombre de tours par question sur le bloc (1 execute,
    ``GROUP BY question_id``)."""
    rows = (
        await db.execute(
            select(ExerciseSubmission.question_id, func.count())
            .where(ExerciseSubmission.block_id == block.id)
            .group_by(ExerciseSubmission.question_id)
        )
    ).all()
    by_question = {str(question_id): int(count) for question_id, count in rows}
    return SubmissionSummaryRead(total=sum(by_question.values()), by_question=by_question)
