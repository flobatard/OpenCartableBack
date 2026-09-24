"""Routes de l'assistant IA d'un cours (protégées prof).

Préfixe ``/courses/{course_id}/assistant`` — aucun conflit de littéral avec
``app/courses/`` (segment ``assistant`` dédié). Le streaming est un **POST**
servi en ``text/event-stream`` (contrat de base dans :mod:`app.core.sse`,
extension agent dans :mod:`app.course_assistant.streaming` ; le CRUD vit dans
:mod:`app.course_assistant.service`).
"""

import uuid

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ai import AIClient, get_ai_client
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.sse import sse_response
from app.core.storage import Storage, get_storage
from app.course_assistant import service, streaming
from app.course_assistant.schemas import (
    AttachmentCreate,
    AttachmentDownload,
    AttachmentPresign,
    AttachmentRead,
    ConversationCreate,
    ConversationDetailRead,
    ConversationRead,
    ConversationUpdate,
    MessageCreate,
    ProposalDecisionCreate,
    QuestionAnswerCreate,
)
from app.models.ai_conversation import CONTEXT_COURSE
from app.users import service as users_service

router = APIRouter(prefix="/courses/{course_id}/assistant", tags=["course-assistant"])


@router.post(
    "/attachments", response_model=AttachmentPresign, status_code=status.HTTP_201_CREATED
)
async def presign_attachment(
    course_id: uuid.UUID,
    payload: AttachmentCreate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> AttachmentPresign:
    """Déclare une pièce jointe et renvoie l'URL présignée d'upload direct
    navigateur → S3. Aucun id de conversation dans le chemin : le front peut
    joindre un fichier alors que la conversation est encore un brouillon."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.presign_attachment(db, user, course_id, payload, storage)


@router.post("/attachments/{attachment_id}/confirm", response_model=AttachmentRead)
async def confirm_attachment(
    course_id: uuid.UUID,
    attachment_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> AttachmentRead:
    """Vérifie l'objet uploadé (HEAD S3 : taille ET type) et rend la pièce
    jointe utilisable. 409 si l'objet est absent ou hors gabarit."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.confirm_attachment(db, user, course_id, attachment_id, storage)


@router.get("/attachments/{attachment_id}/download", response_model=AttachmentDownload)
async def download_attachment(
    course_id: uuid.UUID,
    attachment_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> AttachmentDownload:
    """URL présignée de lecture (disposition ``inline``) d'une pièce jointe."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.presign_attachment_download(
        db, user, course_id, attachment_id, storage
    )


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    course_id: uuid.UUID,
    attachment_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> Response:
    """Retire une pièce jointe **pas encore envoyée**. 409 sinon : une pièce
    rattachée à un message fait partie de la conversation."""
    user = await users_service.get_or_create_by_sub(db, auth)
    await service.delete_attachment(db, user, course_id, attachment_id, storage)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/conversations", response_model=list[ConversationRead])
async def list_conversations(
    course_id: uuid.UUID,
    context: str = Query(default=CONTEXT_COURSE),
    block_id: uuid.UUID | None = None,
    module_id: uuid.UUID | None = None,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ConversationRead]:
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.list_conversations(db, user, course_id, context, block_id, module_id)


@router.post(
    "/conversations", response_model=ConversationRead, status_code=status.HTTP_201_CREATED
)
async def create_conversation(
    course_id: uuid.UUID,
    payload: ConversationCreate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationRead:
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.create_conversation(db, user, course_id, payload)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailRead)
async def get_conversation(
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationDetailRead:
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.get_conversation_detail(db, user, course_id, conversation_id)


@router.patch("/conversations/{conversation_id}", response_model=ConversationRead)
async def rename_conversation(
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: ConversationUpdate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConversationRead:
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.rename_conversation(db, user, course_id, conversation_id, payload)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
    client: AIClient = Depends(get_ai_client),
) -> Response:
    user = await users_service.get_or_create_by_sub(db, auth)
    await service.delete_conversation(db, user, course_id, conversation_id, storage)
    # Une reprise en attente meurt avec sa conversation (registre + thread).
    streaming.drop_pending_resume(client, conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/conversations/{conversation_id}/proposals/{tool_call_id}/decision")
async def submit_proposal_decision(
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    tool_call_id: str,
    payload: ProposalDecisionCreate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
    client: AIClient = Depends(get_ai_client),
) -> StreamingResponse:
    """Décision du professeur sur une proposition d'édition en attente (flux
    HITL) : REPREND le run figé — la réponse est le **SSE de la suite du
    tour** (contrat de ``stream_message`` : ``tool_result``…``done``, ou un
    nouvel ``interrupt``). 404 si rien n'attend (déjà tranchée, expirée, ou
    perdue — redémarrage)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    events = await streaming.sse_decision_stream(
        client, db, storage, auth, user, course_id, conversation_id, tool_call_id, payload
    )
    return sse_response(events)


@router.post("/conversations/{conversation_id}/questions/{tool_call_id}/answer")
async def submit_questions_answer(
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    tool_call_id: str,
    payload: QuestionAnswerCreate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
    client: AIClient = Depends(get_ai_client),
) -> StreamingResponse:
    """Réponse du professeur aux questions de l'assistant en attente (flux
    HITL, tous contextes) : REPREND le run figé — la réponse est le **SSE de
    la suite du tour** (contrat de ``stream_message``). 404 si rien n'attend
    (déjà répondu, expirées, ou perdues — redémarrage) ; 422 si la réponse ne
    correspond pas aux questions posées (la reprise reste disponible)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    events = await streaming.sse_answer_stream(
        client, db, storage, auth, user, course_id, conversation_id, tool_call_id, payload
    )
    return sse_response(events)


@router.post("/conversations/{conversation_id}/messages/stream")
async def stream_message(
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: MessageCreate,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
    client: AIClient = Depends(get_ai_client),
) -> StreamingResponse:
    """Tour d'assistant streamé (SSE). Les erreurs « préparables » (404, 422
    plafond, cascade IA 422/429/503, validation eager) partent en vraies
    HTTPException AVANT le flux ; le reste devient un événement ``error``."""
    user = await users_service.get_or_create_by_sub(db, auth)
    events = await streaming.sse_stream(
        client, db, storage, auth, user, course_id, conversation_id, payload
    )
    return sse_response(events)
