"""Routes du tuteur d'exercice élève (JWT requis — l'élève est authentifié).

Préfixe ``/student/courses/{course_id}/blocks/{block_id}`` : HORS ``/public/``
(le front y attache le Bearer), l'accès au cours étant néanmoins celui du
régime public — le token de partage voyage en query ``?token=`` (motif
:mod:`app.public.router`). Le streaming est un POST servi en
``text/event-stream`` (contrat dans :mod:`app.student_exercises.streaming`).
"""

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai import AIClient, get_ai_client
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.storage import Storage, get_storage
from app.student_exercises import service, streaming
from app.student_exercises.schemas import SubmissionCreate, SubmissionsRead
from app.users import service as users_service

router = APIRouter(
    prefix="/student/courses/{course_id}/blocks/{block_id}", tags=["student-exercises"]
)

_SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
}


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
    return StreamingResponse(events, media_type="text/event-stream", headers=_SSE_HEADERS)
