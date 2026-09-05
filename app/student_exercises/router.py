"""Routes du tuteur d'exercice élève (JWT requis — l'élève est authentifié).

Préfixe ``/student/courses/{course_id}/blocks/{block_id}`` : HORS ``/public/``
(le front y attache le Bearer), l'accès au cours étant néanmoins celui du
régime public — le token de partage voyage en query ``?token=`` (motif
:mod:`app.public.router`). Le streaming est un POST servi en
``text/event-stream`` (contrat dans :mod:`app.student_exercises.streaming`).

``teacher_router`` (préfixe ``/courses/{course_id}/blocks/{block_id}/submissions``,
régime professeur scopé au propriétaire) : résumé des tentatives par question
et effacement de celles de TOUS les élèves — à la maille du bloc ou d'une
question (``?question_id=``), typiquement après avoir remanié l'exercice.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai import AIClient, get_ai_client
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.sse import sse_response
from app.core.storage import Storage, get_storage
from app.student_exercises import service, streaming
from app.student_exercises.schemas import (
    DeletedRead,
    SubmissionCreate,
    SubmissionsRead,
    SubmissionSummaryRead,
)
from app.users import service as users_service

router = APIRouter(
    prefix="/student/courses/{course_id}/blocks/{block_id}", tags=["student-exercises"]
)

@router.get("/submissions", response_model=SubmissionsRead)
async def list_submissions(
    course_id: uuid.UUID,
    block_id: uuid.UUID,
    token: str | None = Query(default=None),
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SubmissionsRead:
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.list_threads(db, user, course_id, token, block_id)


@router.post("/questions/{question_id}/submissions/stream")
async def stream_submission(
    course_id: uuid.UUID,
    block_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: SubmissionCreate,
    token: str | None = Query(default=None),
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
    client: AIClient = Depends(get_ai_client),
) -> StreamingResponse:
    user = await users_service.get_or_create_by_sub(db, auth)
    events = await streaming.sse_stream(
        client, db, storage, auth, user, course_id, token, block_id, question_id, payload
    )
    return sse_response(events)


@router.delete("/submissions", response_model=DeletedRead)
async def delete_submissions(
    course_id: uuid.UUID,
    block_id: uuid.UUID,
    token: str | None = Query(default=None),
    question_id: uuid.UUID | None = None,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeletedRead:
    """Efface les tours de l'élève courant sur le bloc, ou sur une question."""
    user = await users_service.get_or_create_by_sub(db, auth)
    _course, block = await service.load_exercise(db, course_id, token, block_id)
    deleted = await service.delete_turns(db, user, block, question_id)
    return DeletedRead(deleted=deleted)


teacher_router = APIRouter(
    prefix="/courses/{course_id}/blocks/{block_id}/submissions", tags=["student-exercises"]
)


@teacher_router.get("/summary", response_model=SubmissionSummaryRead)
async def submission_summary(
    course_id: uuid.UUID,
    block_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SubmissionSummaryRead:
    user = await users_service.get_or_create_by_sub(db, auth)
    block = await service.load_owned_exercise(db, user, course_id, block_id)
    return await service.submission_summary(db, block)


@teacher_router.delete("", response_model=DeletedRead)
async def delete_all_submissions(
    course_id: uuid.UUID,
    block_id: uuid.UUID,
    question_id: uuid.UUID | None = None,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeletedRead:
    """Efface les tours de TOUS les élèves sur le bloc, ou sur une question."""
    user = await users_service.get_or_create_by_sub(db, auth)
    block = await service.load_owned_exercise(db, user, course_id, block_id)
    deleted = await service.delete_submissions(db, block, question_id)
    return DeletedRead(deleted=deleted)
