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

from sqlalchemy import and_, delete, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import touch
from app.core.http import conflict, invalid, not_found
from app.core.storage import Storage
from app.course_assistant.attachments import (
    attachment_kind,
    attachment_s3_key,
    max_bytes_for,
)
from app.course_assistant.editing import TARGET_MODULE, EditContext, edit_context_for
from app.course_assistant.schemas import (
    AttachmentCreate,
    AttachmentDownload,
    AttachmentPresign,
    AttachmentRead,
    ConversationCreate,
    ConversationDetailRead,
    ConversationRead,
    ConversationUpdate,
    MessageRead,
)
from app.courses.queries import get_owned_course
from app.models.ai_attachment import STATUS_AVAILABLE, STATUS_PENDING, AIAttachment
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


def _message_read(
    message: AIMessage, attachments: list[AttachmentRead] | None = None
) -> MessageRead:
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
        cached_input_tokens=message.cached_input_tokens,
        created_at=message.created_at,
        attachments=attachments or [],
    )


def _attachment_read(attachment: AIAttachment) -> AttachmentRead:
    return AttachmentRead(
        id=attachment.id,
        original_name=attachment.original_name,
        mime=attachment.mime,
        kind=attachment.kind,
        size=attachment.size,
        status=attachment.status,
        created_at=attachment.created_at,
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


async def load_conversation_attachments(
    db: AsyncSession, conversation: AIConversation
) -> list[AIAttachment]:
    """Pièces jointes déjà rattachées à une conversation, tri ``created_at, id``.

    **Un seul execute.** Le tri est stable et la liste ne fait que croître
    (une pièce rattachée est indélébile) : c'est ce qui rend les références
    courtes ``A…`` stables à vie dans une conversation, et donc l'``enum`` du
    tool ``read_attachment`` identique entre un tour et sa reprise HITL.
    """
    return list(
        (
            await db.execute(
                select(AIAttachment)
                .where(AIAttachment.conversation_id == conversation.id)
                .order_by(AIAttachment.created_at, AIAttachment.id)
            )
        )
        .scalars()
        .all()
    )


async def load_turn_attachments(
    db: AsyncSession,
    course: Course,
    user: User,
    conversation: AIConversation,
    new_ids: list[uuid.UUID],
) -> list[AIAttachment]:
    """Pièces jointes visibles du tour : celles déjà rattachées à la
    conversation, **plus** celles que ce message apporte.

    **Un seul execute** (contrat FIFO), même quand ``new_ids`` est vide. Les
    candidates du tour sont filtrées serré — propriétaire, cours, pas encore
    rattachées, upload confirmé : c'est ce filtre qui empêche de joindre la
    pièce d'un autre, ou de rejouer une pièce déjà envoyée. L'appelant compare
    ensuite le compte obtenu à ``new_ids`` et refuse le tour si l'une manque.

    Tri ``created_at, id`` : la numérotation ``A…`` est stable d'un tour à
    l'autre, donc identique entre un tour et sa reprise HITL.
    """
    candidate = and_(
        AIAttachment.id.in_(new_ids),
        AIAttachment.owner_id == user.id,
        AIAttachment.course_id == course.id,
        AIAttachment.conversation_id.is_(None),
        AIAttachment.status == STATUS_AVAILABLE,
    )
    return list(
        (
            await db.execute(
                select(AIAttachment)
                .where(or_(AIAttachment.conversation_id == conversation.id, candidate))
                .order_by(AIAttachment.created_at, AIAttachment.id)
            )
        )
        .scalars()
        .all()
    )


async def bind_attachments(
    db: AsyncSession,
    conversation: AIConversation,
    message_id: uuid.UUID,
    attachment_ids: list[uuid.UUID],
) -> None:
    """Rattache les pièces jointes du tour au message qui vient d'être inséré.

    **Un execute, seulement si le message en porte** (la seule irrégularité du
    contrat FIFO de ``sse_stream``). Vient APRÈS l'insert du message (FK
    ``message_id``) et dans la même transaction : un seul commit.
    """
    if not attachment_ids:
        return
    await db.execute(
        update(AIAttachment)
        .where(AIAttachment.id.in_(attachment_ids))
        .values(conversation_id=conversation.id, message_id=message_id)
    )


async def _get_attachment(
    db: AsyncSession, course: Course, user: User, attachment_id: uuid.UUID
) -> AIAttachment:
    """Charge une pièce jointe scopée au cours ET au propriétaire ; 404 sinon."""
    attachment = (
        (
            await db.execute(
                select(AIAttachment).where(
                    AIAttachment.id == attachment_id,
                    AIAttachment.course_id == course.id,
                    AIAttachment.owner_id == user.id,
                )
            )
        )
        .scalars()
        .one_or_none()
    )
    if attachment is None:
        raise not_found("Pièce jointe introuvable")
    return attachment


async def presign_attachment(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    payload: AttachmentCreate,
    storage: Storage,
) -> AttachmentPresign:
    """Crée la pièce jointe ``pending`` et renvoie l'URL présignée d'upload.

    Ordre des execute : 1) cours (contrôle de propriété), 2) insert. L'URL
    présignée PUT est du calcul local (pas d'execute).

    La pièce n'appartient encore qu'au couple (cours, propriétaire) :
    ``conversation_id`` et ``message_id`` restent nuls jusqu'à l'envoi du
    message qui la porte (:mod:`app.course_assistant.streaming`). C'est ce qui
    permet au front de joindre un fichier alors que la conversation est encore
    un brouillon sans id. Le plafond par conversation
    (``MAX_ATTACHMENTS_PER_CONVERSATION``) n'est donc pas vérifiable ici : il
    l'est au rattachement ; l'accumulation de pièces jamais envoyées est
    bornée par le job de maintenance ``ai_attachments``.
    """
    course = await get_owned_course(db, user, course_id)
    attachment_id = uuid.uuid4()
    s3_key = attachment_s3_key(course.id, attachment_id, payload.original_name)
    await db.execute(
        insert(AIAttachment).values(
            id=attachment_id,
            course_id=course.id,
            owner_id=user.id,
            conversation_id=None,
            message_id=None,
            s3_key=s3_key,
            original_name=payload.original_name,
            mime=payload.mime,
            kind=attachment_kind(payload.mime),
            size=payload.size,
            status=STATUS_PENDING,
        )
    )
    await db.commit()
    return AttachmentPresign(
        attachment_id=attachment_id,
        upload_url=storage.presign_put(s3_key, payload.mime),
        status=STATUS_PENDING,
        expires_in=settings.S3_PRESIGN_PUT_TTL,
    )


async def confirm_attachment(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    attachment_id: uuid.UUID,
    storage: Storage,
) -> AttachmentRead:
    """Vérifie l'objet S3 et passe la pièce jointe à ``available``.

    Ordre des execute : 1) cours, 2) pièce jointe (scopée). Après 2), HEAD S3
    — motif ``confirm_avatar``, plus strict que ``confirm_upload`` : objet
    absent → 409 ; ``ContentLength`` au-dessus du plafond **de la famille**
    (une URL présignée PUT ne borne pas la taille) ou ``ContentType``
    différent du mime déclaré → 409 avec purge de l'objet hors gabarit (la
    ligne reste ``pending``, le job de maintenance la ramassera).

    ⚠ L'``AttachmentRead`` est construit AVANT le commit (piège
    ``MissingGreenlet`` : ``created_at`` est généré côté SQL).
    """
    course = await get_owned_course(db, user, course_id)
    attachment = await _get_attachment(db, course, user, attachment_id)
    if attachment.status == STATUS_AVAILABLE:
        raise conflict("Pièce jointe déjà confirmée")

    metadata = await storage.head(attachment.s3_key)
    if metadata is None:
        raise conflict("Objet introuvable sur S3 : upload non abouti")
    size = metadata.get("ContentLength")
    content_type = metadata.get("ContentType")
    if (size is not None and size > max_bytes_for(attachment.mime)) or (
        content_type is not None and content_type != attachment.mime
    ):
        await storage.delete_many([attachment.s3_key])
        raise conflict("Objet hors gabarit (taille ou type inattendu)")

    attachment.status = STATUS_AVAILABLE
    read = _attachment_read(attachment)
    await db.commit()
    return read


async def presign_attachment_download(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    attachment_id: uuid.UUID,
    storage: Storage,
) -> AttachmentDownload:
    """URL présignée de lecture d'une pièce jointe ``available``.

    Ordre des execute : 1) cours, 2) pièce jointe (scopée). 409 tant que
    l'upload n'est pas confirmé. Disposition ``inline`` : le front affiche
    l'image ou le PDF au lieu de le télécharger. Lecture seule : pas de commit.
    """
    course = await get_owned_course(db, user, course_id)
    attachment = await _get_attachment(db, course, user, attachment_id)
    if attachment.status != STATUS_AVAILABLE:
        raise conflict("Pièce jointe non disponible (upload non confirmé)")
    return AttachmentDownload(
        download_url=storage.presign_get(
            attachment.s3_key, attachment.original_name, inline=True
        ),
        expires_in=settings.S3_PRESIGN_GET_TTL,
    )


async def delete_attachment(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    attachment_id: uuid.UUID,
    storage: Storage,
) -> None:
    """Supprime une pièce jointe **pas encore envoyée** et son objet S3.

    Ordre des execute : 1) cours, 2) pièce jointe (scopée — on relit sa
    ``s3_key``), 3) delete. Purge S3 APRÈS le commit (motif
    ``delete_resource``).

    Une pièce déjà rattachée à un message est **indélébile** (409) : elle fait
    partie de l'historique lisible de la conversation, et surtout l'``enum``
    du tool ``read_attachment`` doit rester identique entre un tour et sa
    reprise HITL — la retirer invaliderait un checkpoint en attente.
    """
    course = await get_owned_course(db, user, course_id)
    attachment = await _get_attachment(db, course, user, attachment_id)
    if attachment.message_id is not None:
        raise conflict("Pièce jointe déjà envoyée : elle fait partie de la conversation")
    s3_key = attachment.s3_key
    await db.execute(delete(AIAttachment).where(AIAttachment.id == attachment.id))
    await db.commit()
    await storage.delete_many([s3_key])


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
    (tri ``position, id``), 4) pièces jointes de la conversation, groupées en
    Python par ``message_id`` et posées sur leur message. Lecture seule : pas
    de commit.
    """
    course = await get_owned_course(db, user, course_id)
    conversation = await load_conversation(db, course, user, conversation_id)
    messages = await load_messages(db, conversation)
    attachments = await load_conversation_attachments(db, conversation)
    by_message: dict[uuid.UUID, list[AttachmentRead]] = {}
    for attachment in attachments:
        if attachment.message_id is not None:
            by_message.setdefault(attachment.message_id, []).append(
                _attachment_read(attachment)
            )
    return ConversationDetailRead(
        **_conversation_read(conversation).model_dump(),
        messages=[_message_read(m, by_message.get(m.id)) for m in messages],
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
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    storage: Storage,
) -> None:
    """Supprime une conversation ; ses messages partent par FK ``CASCADE``.

    Ordre des execute : 1) cours, 2) conversation (scopée), 3) clés S3 de ses
    pièces jointes, 4) delete. Les pièces jointes partent par FK ``CASCADE``
    comme les messages ; leurs objets S3, hors cascade DB, sont purgés APRÈS
    le commit (motif ``delete_resource`` : un échec S3 laisse un orphelin
    ramassé par la réconciliation, jamais une réf DB vers un objet absent).
    """
    course = await get_owned_course(db, user, course_id)
    conversation = await load_conversation(db, course, user, conversation_id)
    s3_keys = list(
        (
            await db.execute(
                select(AIAttachment.s3_key).where(
                    AIAttachment.conversation_id == conversation.id
                )
            )
        )
        .scalars()
        .all()
    )
    await db.execute(delete(AIConversation).where(AIConversation.id == conversation.id))
    await db.commit()
    if s3_keys:
        await storage.delete_many(s3_keys)
