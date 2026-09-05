"""Assistant IA d'un cours : conversations persistées (CRUD).

Le flux SSE d'un tour d'assistant et sa reprise HITL vivent dans
:mod:`app.course_assistant.streaming` (contrat SSE documenté là) ; ce module
porte le CRUD des conversations et les chargements scopés qu'il partage avec
lui (:func:`load_conversation`, :func:`load_messages` ; le cours lui-même vient
de :func:`app.courses.queries.get_owned_course`). Les contextes d'édition
(validation de la cible visée à la création) sont décrits par
:mod:`app.course_assistant.editing` — aucune
branche par contexte ici, seulement sur la **cible** du descripteur (bloc ou
module).

Comme partout, tout est scopé au propriétaire (404 jamais 403) et l'ordre des
``execute`` de chaque fonction est un contrat des tests (fausse session FIFO).
"""

import uuid

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import touch
from app.core.http import invalid, not_found
from app.course_assistant.editing import TARGET_MODULE, EditContext, edit_context_for
from app.course_assistant.schemas import (
    ConversationCreate,
    ConversationDetailRead,
    ConversationRead,
    ConversationUpdate,
    MessageRead,
)
from app.courses.queries import get_owned_course
from app.models.ai_conversation import AIConversation
from app.models.ai_message import AIMessage
from app.models.block import Block
from app.models.course import Course
from app.models.module import Module
from app.models.resource import Resource
from app.models.user import User

CONVERSATION_LIST_LIMIT = 100


def _conversation_read(conversation: AIConversation) -> ConversationRead:
    return ConversationRead(
        id=conversation.id,
        context=conversation.context,
        block_id=conversation.block_id,
        module_id=conversation.module_id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def _message_read(message: AIMessage) -> MessageRead:
    return MessageRead(
        id=message.id,
        role=message.role,
        position=message.position,
        content=message.content,
        tool_calls=message.tool_calls,
        tool_call_id=message.tool_call_id,
        is_error=message.is_error,
        sources=message.sources,
        input_tokens=message.input_tokens,
        output_tokens=message.output_tokens,
        created_at=message.created_at,
    )


async def load_conversation(
    db: AsyncSession, course: Course, user: User, conversation_id: uuid.UUID
) -> AIConversation:
    """Charge une conversation scopée au cours ET au propriétaire ; 404 sinon."""
    conversation = (
        (
            await db.execute(
                select(AIConversation).where(
                    AIConversation.id == conversation_id,
                    AIConversation.course_id == course.id,
                    AIConversation.owner_id == user.id,
                )
            )
        )
        .scalars()
        .one_or_none()
    )
    if conversation is None:
        raise not_found("Conversation introuvable")
    return conversation


async def load_messages(db: AsyncSession, conversation: AIConversation) -> list[AIMessage]:
    """Messages d'une conversation, tri stable ``position, id``."""
    return list(
        (
            await db.execute(
                select(AIMessage)
                .where(AIMessage.conversation_id == conversation.id)
                .order_by(AIMessage.position, AIMessage.id)
            )
        )
        .scalars()
        .all()
    )


async def load_snapshot(
    db: AsyncSession, course: Course
) -> tuple[list[Block], list[Resource], list[Module]]:
    """Instantané du cours pour un tour d'IA : blocs (tri ``position, id``),
    ressources et modules (tri ``created_at desc, id``) — trois execute, dans
    cet ordre (contrat FIFO). Partagé avec le tuteur d'exercice."""
    blocks = list(
        (
            await db.execute(
                select(Block)
                .where(Block.course_id == course.id)
                .order_by(Block.position, Block.id)
            )
        )
        .scalars()
        .all()
    )
    resources = list(
        (
            await db.execute(
                select(Resource)
                .where(Resource.course_id == course.id)
                .order_by(Resource.created_at.desc(), Resource.id)
            )
        )
        .scalars()
        .all()
    )
    modules = list(
        (
            await db.execute(
                select(Module)
                .where(Module.course_id == course.id)
                .order_by(Module.created_at.desc(), Module.id)
            )
        )
        .scalars()
        .all()
    )
    return blocks, resources, modules


async def list_conversations(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    context: str,
    block_id: uuid.UUID | None = None,
    module_id: uuid.UUID | None = None,
) -> list[ConversationRead]:
    """Conversations du cours pour un contexte, la plus récente d'abord.

    ``block_id`` / ``module_id`` restreignent aux conversations d'une cible
    d'édition (``None`` = pas de filtre, comportement historique du contexte
    ``course``).

    Ordre des execute : 1) cours (contrôle de propriété), 2) conversations
    (tri ``updated_at desc, id``, plafond :data:`CONVERSATION_LIST_LIMIT`).
    Lecture seule : pas de commit.
    """
    course = await get_owned_course(db, user, course_id)
    stmt = (
        select(AIConversation)
        .where(
            AIConversation.course_id == course.id,
            AIConversation.owner_id == user.id,
            AIConversation.context == context,
        )
        .order_by(AIConversation.updated_at.desc(), AIConversation.id)
        .limit(CONVERSATION_LIST_LIMIT)
    )
    if block_id is not None:
        stmt = stmt.where(AIConversation.block_id == block_id)
    if module_id is not None:
        stmt = stmt.where(AIConversation.module_id == module_id)
    conversations = (await db.execute(stmt)).scalars().all()
    return [_conversation_read(c) for c in conversations]


async def _check_edit_target(
    db: AsyncSession, course: Course, edit: EditContext, payload: ConversationCreate
) -> None:
    """Vérifie la cible d'un contexte d'édition : un module ou un bloc du cours
    (404 introuvable/d'autrui), du type attendu par le descripteur pour un bloc
    (422 sinon). **Seul aiguillage sur ``edit.target``** du service — un execute
    dans les deux cas (contrat FIFO)."""
    if edit.target == TARGET_MODULE:
        module = (
            (
                await db.execute(
                    select(Module).where(
                        Module.id == payload.module_id, Module.course_id == course.id
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        if module is None:
            raise not_found("Module introuvable")
        return
    block = (
        (
            await db.execute(
                select(Block).where(Block.id == payload.block_id, Block.course_id == course.id)
            )
        )
        .scalars()
        .one_or_none()
    )
    if block is None:
        raise not_found("Bloc introuvable")
    if block.type != edit.block_type:
        raise invalid(edit.type_error_detail)


async def create_conversation(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: ConversationCreate
) -> ConversationRead:
    """Crée une conversation vide (le titre viendra du premier message).

    Ordre des execute : 1) cours (contrôle de propriété), [contexte
    d'édition : 2) la **cible** scopée au cours — bloc ou module selon le
    descripteur, cf. :func:`_check_edit_target`], puis insert (RETURNING les
    timestamps — motif ``create_module``). Le contexte ``course`` ne pointe ni
    bloc ni module ; un contexte d'édition exige sa cible (validée par le
    schéma ET le CHECK en base).
    """
    course = await get_owned_course(db, user, course_id)
    edit = edit_context_for(payload.context)
    if edit is not None:
        await _check_edit_target(db, course, edit, payload)
    conversation_id = uuid.uuid4()
    created_at, updated_at = (
        await db.execute(
            insert(AIConversation)
            .values(
                id=conversation_id,
                course_id=course.id,
                owner_id=user.id,
                context=payload.context,
                block_id=payload.block_id,
                module_id=payload.module_id,
            )
            .returning(AIConversation.created_at, AIConversation.updated_at)
        )
    ).one()
    await db.commit()
    return ConversationRead(
        id=conversation_id,
        context=payload.context,
        block_id=payload.block_id,
        module_id=payload.module_id,
        title=None,
        created_at=created_at,
        updated_at=updated_at,
    )


async def get_conversation_detail(
    db: AsyncSession, user: User, course_id: uuid.UUID, conversation_id: uuid.UUID
) -> ConversationDetailRead:
    """Détail d'une conversation avec ses messages (tours tool inclus).

    Ordre des execute : 1) cours, 2) conversation (scopée), 3) messages
    (tri ``position, id``). Lecture seule : pas de commit.
    """
    course = await get_owned_course(db, user, course_id)
    conversation = await load_conversation(db, course, user, conversation_id)
    messages = await load_messages(db, conversation)
    return ConversationDetailRead(
        **_conversation_read(conversation).model_dump(),
        messages=[_message_read(m) for m in messages],
    )


async def rename_conversation(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: ConversationUpdate,
) -> ConversationRead:
    """Renomme une conversation (mutation d'attribut, ``updated_at`` Python).

    Ordre des execute : 1) cours, 2) conversation (scopée). Le
    ``ConversationRead`` est construit AVANT le commit (piège MissingGreenlet).
    """
    course = await get_owned_course(db, user, course_id)
    conversation = await load_conversation(db, course, user, conversation_id)
    conversation.title = payload.title
    touch(conversation)
    read = _conversation_read(conversation)
    await db.commit()
    return read


async def delete_conversation(
    db: AsyncSession, user: User, course_id: uuid.UUID, conversation_id: uuid.UUID
) -> None:
    """Supprime une conversation ; ses messages partent par FK ``CASCADE``.

    Ordre des execute : 1) cours, 2) conversation (scopée), 3) delete.
    """
    course = await get_owned_course(db, user, course_id)
    conversation = await load_conversation(db, course, user, conversation_id)
    await db.execute(delete(AIConversation).where(AIConversation.id == conversation.id))
    await db.commit()
