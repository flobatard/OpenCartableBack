"""Assistant IA d'un cours : conversations persistées + flux SSE agent.

Contrat SSE — extension du contrat de référence de :mod:`app.ai.service` :

.. code-block:: text

    event: token         data: {"delta": "…"}
    event: thinking      data: {"delta": "…"}
    event: tool_call     data: {"id": "…", "name": "read_block", "args": {…}}
    event: tool_result   data: {"id": "…", "name": "…", "is_error": false,
                                "excerpt": "…", "length": 12345}
    event: done          data: {"usage": {…}|null, "user_message_id": "…",
                                "message_ids": ["…"], "sources": {…},
                                "title": "…"|null}
    event: error         data: {"status": 503, "detail": "…"}

Le contenu COMPLET des résultats d'outils ne part jamais sur le flux
(potentiellement 40k caractères de PDF) — il est persisté et servi par le
détail de conversation ; seul un **extrait borné** l'accompagne
(:data:`TOOL_RESULT_EXCERPT_CHARS` premiers caractères + longueur totale —
un message d'échec tient toujours dedans) pour l'affichage déplié des appels
d'outils côté front. ``done`` porte les ids des messages persistés du tour
(le front réconcilie sans refetch) et le titre si posé à ce tour.

Persistance du tour (rôles ``assistant``/``tool``, motif documenté dans
:mod:`app.models.ai_message`) : chaque round du modèle devient un segment
``assistant`` (texte + ``tool_calls``) suivi de ses lignes ``tool`` ; le
segment final porte ``sources`` (citations validées) et l'usage. Le message
``user`` est persisté AVANT l'appel provider (durable même si l'appel
échoue) ; sur erreur mid-stream, les rounds déjà complets et le texte partiel
sont persistés (l'appel est compté dès le premier token — décision actée).

Comme partout, tout est scopé au propriétaire (404 jamais 403) et l'ordre des
``execute`` de chaque fonction est un contrat des tests (fausse session FIFO).
"""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_credentials.service import effective_config, refund_default_quota
from app.core.ai import AIClient, ChatMessage
from app.core.auth import AuthenticatedUser
from app.core.config import settings
from app.core.storage import Storage
from app.course_assistant.context import (
    TRUNCATED_HISTORY_NOTICE,
    build_course_context,
    extract_sources,
    replay_messages,
)
from app.course_assistant.schemas import (
    ConversationCreate,
    ConversationDetailRead,
    ConversationRead,
    ConversationUpdate,
    MessageCreate,
    MessageRead,
)
from app.course_assistant.tools import TOOL_SPECS, build_tool_executor
from app.models.ai_conversation import AIConversation
from app.models.ai_message import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER, AIMessage
from app.models.block import Block
from app.models.course import Course
from app.models.module import Module
from app.models.resource import Resource
from app.models.user import User

_TRACE_NAME = "course-assistant"

# Garde-fous (422 au-delà) : les tours tool comptent dans le plafond.
MAX_MESSAGES_PER_CONVERSATION = 300
CONVERSATION_LIST_LIMIT = 100
TITLE_TRUNCATE_CHARS = 80
MAX_TOOL_ROUNDS = 5

# Extrait d'un résultat d'outil relayé sur le flux (le contenu complet, lui,
# n'est servi que par le détail de conversation) — même valeur côté front.
TOOL_RESULT_EXCERPT_CHARS = 400


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _sse_event(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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


async def _get_owned_course(db: AsyncSession, user: User, course_id: uuid.UUID) -> Course:
    """Charge le cours du prof ; 404 s'il n'existe pas ou appartient à autrui."""
    course = (
        (
            await db.execute(
                select(Course).where(Course.id == course_id, Course.owner_id == user.id)
            )
        )
        .scalars()
        .one_or_none()
    )
    if course is None:
        raise _not_found("Cours introuvable")
    return course


async def _get_conversation(
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
        raise _not_found("Conversation introuvable")
    return conversation


async def _get_messages(db: AsyncSession, conversation: AIConversation) -> list[AIMessage]:
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


async def list_conversations(
    db: AsyncSession, user: User, course_id: uuid.UUID, context: str
) -> list[ConversationRead]:
    """Conversations du cours pour un contexte, la plus récente d'abord.

    Ordre des execute : 1) cours (contrôle de propriété), 2) conversations
    (tri ``updated_at desc, id``, plafond :data:`CONVERSATION_LIST_LIMIT`).
    Lecture seule : pas de commit.
    """
    course = await _get_owned_course(db, user, course_id)
    conversations = (
        (
            await db.execute(
                select(AIConversation)
                .where(
                    AIConversation.course_id == course.id,
                    AIConversation.owner_id == user.id,
                    AIConversation.context == context,
                )
                .order_by(AIConversation.updated_at.desc(), AIConversation.id)
                .limit(CONVERSATION_LIST_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return [_conversation_read(c) for c in conversations]


async def create_conversation(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: ConversationCreate
) -> ConversationRead:
    """Crée une conversation vide (le titre viendra du premier message).

    Ordre des execute : 1) cours (contrôle de propriété), 2) insert
    (RETURNING les timestamps — motif ``create_module``). Le contexte
    ``course`` ne pointe ni bloc ni module (CHECK en base).
    """
    course = await _get_owned_course(db, user, course_id)
    conversation_id = uuid.uuid4()
    created_at, updated_at = (
        await db.execute(
            insert(AIConversation)
            .values(
                id=conversation_id,
                course_id=course.id,
                owner_id=user.id,
                context=payload.context,
            )
            .returning(AIConversation.created_at, AIConversation.updated_at)
        )
    ).one()
    await db.commit()
    return ConversationRead(
        id=conversation_id,
        context=payload.context,
        block_id=None,
        module_id=None,
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
    course = await _get_owned_course(db, user, course_id)
    conversation = await _get_conversation(db, course, user, conversation_id)
    messages = await _get_messages(db, conversation)
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
    course = await _get_owned_course(db, user, course_id)
    conversation = await _get_conversation(db, course, user, conversation_id)
    conversation.title = payload.title
    conversation.updated_at = datetime.now(UTC)
    read = _conversation_read(conversation)
    await db.commit()
    return read


async def delete_conversation(
    db: AsyncSession, user: User, course_id: uuid.UUID, conversation_id: uuid.UUID
) -> None:
    """Supprime une conversation ; ses messages partent par FK ``CASCADE``.

    Ordre des execute : 1) cours, 2) conversation (scopée), 3) delete.
    """
    course = await _get_owned_course(db, user, course_id)
    conversation = await _get_conversation(db, course, user, conversation_id)
    await db.execute(delete(AIConversation).where(AIConversation.id == conversation.id))
    await db.commit()


async def sse_stream(
    client: AIClient,
    db: AsyncSession,
    storage: Storage,
    auth: AuthenticatedUser,
    user: User,
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: MessageCreate,
) -> AsyncIterator[str]:
    """Prépare le flux SSE d'un tour d'assistant (docstring du module).

    Tout ce qui peut échouer en « vraie » HTTPException est résolu ICI, avant
    que la route ne retourne la ``StreamingResponse`` : propriété (404),
    conversation (404), plafond de messages (422), cascade IA + quota
    (422/429/503 — remboursé sur erreur eager), validation eager de
    ``stream_agent``.

    Ordre des execute : 1) cours (contrôle de propriété), 2) conversation
    (scopée), 3) messages existants (historique + plafond), [cascade
    ``effective_config`` : ses propres execute], 4) blocs (tri ``position,
    id``), 5) ressources (tri ``created_at desc, id``), 6) modules (idem),
    7) insert du message user (position suivante ; titre posé au premier
    message ; ``updated_at`` bumpé) puis commit. Le generator retourné insère
    ensuite les messages du tour (un execute + commit à la clôture).
    """
    course = await _get_owned_course(db, user, course_id)
    conversation = await _get_conversation(db, course, user, conversation_id)
    existing = await _get_messages(db, conversation)
    if len(existing) >= MAX_MESSAGES_PER_CONVERSATION:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Conversation pleine — démarrez-en une nouvelle",
        )

    # ``config`` None = repli serveur AI_* (résolu par AIClient.resolve_config) ;
    # le provider effectif sert au replay (repli inter-provider) et à la
    # colonne ``provider`` des segments persistés.
    config, ticket = await effective_config(db, auth, None)
    provider = config.provider.value if config is not None else settings.AI_PROVIDER

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

    system_content = build_course_context(course, blocks, resources, modules)
    history, truncated = replay_messages(existing, provider)
    if truncated:
        system_content += TRUNCATED_HISTORY_NOTICE
    model_messages = [
        ChatMessage(role="system", content=system_content),
        *history,
        ChatMessage(role="user", content=payload.content),
    ]

    executor = build_tool_executor(
        storage,
        blocks_by_id={b.id: b for b in blocks},
        resources_by_id={r.id: r for r in resources},
        positions_by_id={b.id: i for i, b in enumerate(blocks, start=1)},
        modules_by_id={m.id: m for m in modules},
    )

    # Message user durable AVANT l'appel provider (le front garde sa saisie de
    # toute façon ; un échec provider ne perd pas la question).
    user_message_id = uuid.uuid4()
    await db.execute(
        insert(AIMessage).values(
            id=user_message_id,
            conversation_id=conversation.id,
            role=ROLE_USER,
            position=len(existing),
            content=payload.content,
        )
    )
    title_set: str | None = None
    if conversation.title is None:
        title_set = payload.content.strip()[:TITLE_TRUNCATE_CHARS]
        conversation.title = title_set
    conversation.updated_at = datetime.now(UTC)
    await db.commit()

    try:
        events = client.stream_agent(
            model_messages,
            config,
            tools=TOOL_SPECS,
            tool_executor=executor,
            max_tool_rounds=MAX_TOOL_ROUNDS,
            trace_name=_TRACE_NAME,
            user_id=auth.sub,
        )
    except Exception:
        if ticket is not None:
            await refund_default_quota(db, ticket)
        raise

    block_ids = {b.id for b in blocks}
    resource_ids = {r.id for r in resources}
    base_position = len(existing) + 1

    async def _encode() -> AsyncIterator[str]:
        # La session `db` reste utilisable ici : les dépendances yield de
        # FastAPI ne sont refermées qu'après l'envoi complet du flux.
        tokens_emitted = False
        all_text: list[str] = []
        turn_rows: list[dict[str, Any]] = []
        segment_text: list[str] = []
        segment_tool_calls: list[dict[str, Any]] = []

        def _flush_segment() -> None:
            if segment_text or segment_tool_calls:
                turn_rows.append(
                    {
                        "role": ROLE_ASSISTANT,
                        "content": "".join(segment_text),
                        "tool_calls": list(segment_tool_calls),
                        "provider": provider,
                    }
                )
                segment_text.clear()
                segment_tool_calls.clear()

        async def _persist_turn(
            sources: dict[str, Any] | None, usage: dict[str, Any] | None
        ) -> list[uuid.UUID]:
            """Insère les lignes du tour (un execute), bump + commit."""
            if not turn_rows:
                return []
            if sources is not None:
                turn_rows[-1]["sources"] = sources
            if usage is not None and turn_rows[-1]["role"] == ROLE_ASSISTANT:
                turn_rows[-1]["input_tokens"] = usage.get("input_tokens")
                turn_rows[-1]["output_tokens"] = usage.get("output_tokens")
            ids = [uuid.uuid4() for _ in turn_rows]
            # Clés homogènes obligatoires (executemany Core) : chaque ligne est
            # normalisée sur le jeu complet de colonnes.
            await db.execute(
                insert(AIMessage),
                [
                    {
                        "id": row_id,
                        "conversation_id": conversation.id,
                        "position": base_position + i,
                        "role": row["role"],
                        "content": row.get("content", ""),
                        "tool_calls": row.get("tool_calls", []),
                        "tool_call_id": row.get("tool_call_id"),
                        "is_error": row.get("is_error", False),
                        "provider": row.get("provider"),
                        "sources": row.get("sources", {}),
                        "input_tokens": row.get("input_tokens"),
                        "output_tokens": row.get("output_tokens"),
                    }
                    for i, (row_id, row) in enumerate(zip(ids, turn_rows, strict=True))
                ],
            )
            conversation.updated_at = datetime.now(UTC)
            await db.commit()
            return ids

        try:
            async for event in events:
                if event.type == "token":
                    tokens_emitted = True
                    segment_text.append(event.delta)
                    all_text.append(event.delta)
                    yield _sse_event("token", {"delta": event.delta})
                elif event.type == "thinking":
                    yield _sse_event("thinking", {"delta": event.delta})
                elif event.type == "tool_call":
                    call = event.tool_call
                    segment_tool_calls.append(
                        {"id": call.id, "name": call.name, "arguments": call.arguments}
                    )
                    yield _sse_event(
                        "tool_call",
                        {"id": call.id, "name": call.name, "args": call.arguments},
                    )
                elif event.type == "tool_result":
                    # Le segment assistant porteur des tool_calls est clos par
                    # l'arrivée du premier résultat.
                    _flush_segment()
                    turn_rows.append(
                        {
                            "role": ROLE_TOOL,
                            "content": event.delta,
                            "tool_call_id": event.tool_call.id or "?",
                            "is_error": bool(event.tool_result_error),
                        }
                    )
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
                else:  # done
                    _flush_segment()
                    sources = extract_sources("".join(all_text), block_ids, resource_ids)
                    usage = event.usage.model_dump() if event.usage else None
                    ids = await _persist_turn(sources, usage)
                    yield _sse_event(
                        "done",
                        {
                            "usage": usage,
                            "user_message_id": str(user_message_id),
                            "message_ids": [str(i) for i in ids],
                            "sources": sources,
                            "title": title_set,
                        },
                    )
        except HTTPException as exc:
            # Trop tard pour changer le status HTTP (200 parti) : remboursement
            # si l'erreur précède le premier token, persistance du partiel
            # (best-effort : ne jamais masquer l'erreur provider), puis
            # événement error portant le status du mapping app/core/ai/errors.
            if ticket is not None and not tokens_emitted:
                await refund_default_quota(db, ticket)
            _flush_segment()
            try:
                await _persist_turn(None, None)
            except Exception:  # noqa: BLE001 — best-effort assumé
                pass
            yield _sse_event("error", {"status": exc.status_code, "detail": exc.detail})

    return _encode()
