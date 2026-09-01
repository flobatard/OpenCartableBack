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

S'y ajoute, en contexte ``block_text`` (flux HITL), l'événement terminal :

.. code-block:: text

    event: interrupt     data: {"tool_call_id": "…", "message_ids": ["…"]}

émis quand l'agent propose une édition (``propose_block_edit``) : le run est
FIGÉ (interrupt LangGraph, état au checkpointer InMemory du client — cf.
``hitl.py``), le tour partiel est persisté et le flux se ferme SANS ``done``.
La reprise est le **flux SSE de la route de décision**
(``POST .../proposals/{tool_call_id}/decision`` → :func:`sse_resume_stream`,
même contrat : ``tool_result``…``done``, ou un nouvel ``interrupt``). Le
markdown proposé voyage dans les ``args`` du ``tool_call`` (relayés en entier,
contrairement aux résultats), références de contenu réécrites en UUID.

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
from app.course_assistant import hitl
from app.course_assistant.context import (
    TRUNCATED_HISTORY_NOTICE,
    build_course_context,
    build_refs,
    extract_sources,
    replay_messages,
)
from app.course_assistant.refs import CitationRewriter
from app.course_assistant.schemas import (
    ConversationCreate,
    ConversationDetailRead,
    ConversationRead,
    ConversationUpdate,
    MessageCreate,
    MessageRead,
    ProposalDecisionCreate,
)
from app.course_assistant.tools import (
    PROPOSE_BLOCK_EDIT,
    build_tool_executor,
    build_tool_specs,
)
from app.models.ai_conversation import CONTEXT_BLOCK_TEXT, AIConversation
from app.models.ai_message import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER, AIMessage
from app.models.block import TYPE_TEXT, Block
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
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    context: str,
    block_id: uuid.UUID | None = None,
) -> list[ConversationRead]:
    """Conversations du cours pour un contexte, la plus récente d'abord.

    ``block_id`` restreint aux conversations d'un bloc (contextes d'édition —
    ``None`` = pas de filtre, comportement historique du contexte ``course``).

    Ordre des execute : 1) cours (contrôle de propriété), 2) conversations
    (tri ``updated_at desc, id``, plafond :data:`CONVERSATION_LIST_LIMIT`).
    Lecture seule : pas de commit.
    """
    course = await _get_owned_course(db, user, course_id)
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
    conversations = (await db.execute(stmt)).scalars().all()
    return [_conversation_read(c) for c in conversations]


async def create_conversation(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: ConversationCreate
) -> ConversationRead:
    """Crée une conversation vide (le titre viendra du premier message).

    Ordre des execute : 1) cours (contrôle de propriété), [contexte
    ``block_text`` : 2) bloc scopé au cours — 404 introuvable/d'autrui, 422
    si le bloc n'est pas de type ``text``], puis insert (RETURNING les
    timestamps — motif ``create_module``). Le contexte ``course`` ne pointe
    ni bloc ni module ; ``block_text`` exige ``block_id`` (validé par le
    schéma ET le CHECK en base).
    """
    course = await _get_owned_course(db, user, course_id)
    if payload.context == CONTEXT_BLOCK_TEXT:
        block = (
            (
                await db.execute(
                    select(Block).where(
                        Block.id == payload.block_id, Block.course_id == course.id
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        if block is None:
            raise _not_found("Bloc introuvable")
        if block.type != TYPE_TEXT:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Ce contexte ne s'applique qu'aux blocs texte",
            )
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
            )
            .returning(AIConversation.created_at, AIConversation.updated_at)
        )
    ).one()
    await db.commit()
    return ConversationRead(
        id=conversation_id,
        context=payload.context,
        block_id=payload.block_id,
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

    Contexte ``block_text`` (même ordre d'execute) : le bloc édité est
    retrouvé dans l'instantané déjà chargé (404 défensif s'il a disparu), mis
    en avant dans le system prompt (``focus_block``), le run est **checkpointé**
    (``thread_id`` — InMemorySaver du client) et le tool HITL
    ``propose_block_edit`` est exposé — la proposition voyage dans les args du
    ``tool_call`` (références de contenu réécrites en UUID, relayés complets
    sur le flux et persistés dans ``tool_calls``), puis le run est **figé**
    (interrupt LangGraph) : le flux émet ``interrupt`` et se ferme, la reprise
    passe par :func:`sse_resume_stream` (route de décision). Un nouveau message
    alors qu'une proposition attendait abandonne la reprise.
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

    # Instantané du cours en références courtes (B1/R1/M1) : le modèle ne
    # manipule jamais d'UUID — cf. app/course_assistant/refs.py.
    refs = build_refs(blocks, resources, modules)
    # Contexte d'édition ``block_text`` : le bloc édité est mis en avant dans
    # le prompt (toujours rendu en entier) et le tool ``propose_block_edit``
    # est exposé. 404 défensif : la FK CASCADE rend le bloc absent théorique.
    focus_block = None
    if conversation.context == CONTEXT_BLOCK_TEXT:
        focus_block = next((b for b in blocks if b.id == conversation.block_id), None)
        if focus_block is None:
            # Le quota a déjà été réservé par la cascade : remboursé (motif de
            # l'erreur eager de stream_agent ci-dessous).
            if ticket is not None:
                await refund_default_quota(db, ticket)
            raise _not_found("Bloc introuvable")
    # Contexte d'édition : run checkpointé (thread) pour permettre l'interrupt
    # HITL du tool de proposition ; un nouveau message alors qu'une proposition
    # attendait abandonne la reprise (registre + thread purgés).
    thread_id: str | None = None
    if focus_block is not None:
        thread_id = str(uuid.uuid4())
        stale = hitl.drop(conversation.id)
        if stale is not None:
            client.drop_agent_thread(stale.thread_id)
    system_content = build_course_context(course, refs, focus_block=focus_block)
    history, truncated = replay_messages(existing, provider)
    if truncated:
        system_content += TRUNCATED_HISTORY_NOTICE
    model_messages = [
        ChatMessage(role="system", content=system_content),
        *history,
        ChatMessage(role="user", content=payload.content),
    ]

    executor = build_tool_executor(storage, refs, include_propose=focus_block is not None)

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
            tools=build_tool_specs(refs, include_propose=focus_block is not None),
            tool_executor=executor,
            max_tool_rounds=MAX_TOOL_ROUNDS,
            thread_id=thread_id,
            trace_name=_TRACE_NAME,
            user_id=auth.sub,
        )
    except Exception:
        if ticket is not None:
            await refund_default_quota(db, ticket)
        raise

    return _encode_turn(
        client=client,
        db=db,
        events=events,
        conversation=conversation,
        refs=refs,
        provider=provider,
        config=config,
        thread_id=thread_id,
        base_position=len(existing) + 1,
        ticket=ticket,
        user_message_id=user_message_id,
        title_set=title_set,
    )


async def sse_resume_stream(
    client: AIClient,
    db: AsyncSession,
    storage: Storage,
    auth: AuthenticatedUser,
    user: User,
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    tool_call_id: str,
    payload: ProposalDecisionCreate,
) -> AsyncIterator[str]:
    """Reprend un run figé par une proposition d'édition (flux HITL) : la
    décision du professeur devient la valeur de reprise de l'interrupt — le
    tool ``propose_block_edit`` est ré-exécuté, son résultat EST la décision,
    et la réponse est le **SSE de la suite du tour** (même contrat que
    ``stream_message`` : ``tool_result``…``done`` — ou un nouvel ``interrupt``
    si le modèle re-propose après un rejet commenté).

    404 si rien n'attend (proposition inconnue, déjà tranchée, expirée, ou
    perdue — redémarrage). La **config de la reprise est celle du tour
    d'origine** (registre in-process — même provider garanti, pas de nouvelle
    cascade ni de quota : un tour HITL = un appel compté, décision actée) ;
    pas de nouveau message user, les positions continuent le tour persisté.

    Ordre des execute : 1) cours (contrôle de propriété), 2) conversation
    (scopée), 3) messages existants (position suivante), 4) blocs, 5)
    ressources, 6) modules (l'instantané des tools est rechargé — le modèle
    peut encore lire le cours après la décision). Aucune écriture ici : le
    generator persiste la suite du tour à la clôture.
    """
    course = await _get_owned_course(db, user, course_id)
    conversation = await _get_conversation(db, course, user, conversation_id)
    pending = hitl.take(conversation.id, tool_call_id)
    if pending is None:
        raise _not_found("Aucune proposition en attente pour cet appel")
    existing = await _get_messages(db, conversation)

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
    refs = build_refs(blocks, resources, modules)

    try:
        events = client.stream_agent(
            [],
            pending.config,
            tools=build_tool_specs(refs, include_propose=True),
            tool_executor=build_tool_executor(storage, refs, include_propose=True),
            max_tool_rounds=MAX_TOOL_ROUNDS,
            thread_id=pending.thread_id,
            resume={"accepted": payload.accepted, "comment": payload.comment},
            trace_name=_TRACE_NAME,
            user_id=auth.sub,
        )
    except Exception:
        # Reprise consommée mais run irrécupérable : thread purgé, le round
        # restera incomplet (replié au replay).
        client.drop_agent_thread(pending.thread_id)
        raise

    return _encode_turn(
        client=client,
        db=db,
        events=events,
        conversation=conversation,
        refs=refs,
        provider=pending.provider,
        config=pending.config,
        thread_id=pending.thread_id,
        base_position=len(existing),
        ticket=None,
        user_message_id=None,
        title_set=None,
    )


async def _encode_turn(
    *,
    client: AIClient,
    db: AsyncSession,
    events: AsyncIterator[Any],
    conversation: AIConversation,
    refs: Any,
    provider: str,
    config: Any,
    thread_id: str | None,
    base_position: int,
    ticket: Any,
    user_message_id: uuid.UUID | None,
    title_set: str | None,
) -> AsyncIterator[str]:
    """Encode un flux agent en SSE et persiste le tour — partagé par
    ``sse_stream`` (tour complet ou jusqu'à l'interrupt) et
    ``sse_resume_stream`` (suite du tour, ``user_message_id``/``ticket``
    ``None``). La session ``db`` reste utilisable ici : les dépendances yield
    de FastAPI ne sont refermées qu'après l'envoi complet du flux.

    Sur ``interrupt`` (proposition HITL) : le tour PARTIEL est persisté
    (segment assistant porteur du ``tool_call``, sans tour ``tool`` — un
    abandon le laissera en round incomplet, replié au replay), la reprise est
    enregistrée au registre (``hitl``) et le flux se clôt SANS ``done``. Sur
    ``done``/``error``, le thread checkpointé est purgé (best-effort).
    """
    block_ids = refs.ids("block")
    resource_ids = refs.ids("resource")
    tokens_emitted = False
    all_text: list[str] = []
    turn_rows: list[dict[str, Any]] = []
    segment_text: list[str] = []
    segment_tool_calls: list[dict[str, Any]] = []
    # Citations oc-block:B3 / oc-resource:R2 réécrites en UUID au fil du
    # flux : le texte streamé est celui persisté (et celui des sources).
    rewriter = CitationRewriter(refs)

    def _emit_text(text: str) -> str | None:
        """Texte prêt à partir (citations résolues) : accumulé, ou None."""
        if not text:
            return None
        segment_text.append(text)
        all_text.append(text)
        return _sse_event("token", {"delta": text})

    def _flush_segment() -> str | None:
        """Clôt le segment assistant courant ; retourne l'éventuel
        événement ``token`` du texte retenu par le rewriter (à yield
        AVANT tout événement suivant)."""
        sse = _emit_text(rewriter.flush())
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
        return sse

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
                sse = _emit_text(rewriter.feed(event.delta))
                if sse is not None:
                    yield sse
            elif event.type == "thinking":
                yield _sse_event("thinking", {"delta": event.delta})
            elif event.type == "tool_call":
                # Le texte retenu par le rewriter part avant l'appel d'outil
                # (ordre d'affichage côté front).
                held = _emit_text(rewriter.flush())
                if held is not None:
                    yield held
                call = event.tool_call
                # Proposition d'édition : les références courtes des liens
                # de contenu (oc-resource:R2/oc-module:M1) sont réécrites
                # en UUID AVANT relais et persistance — le markdown reçu
                # par le front est directement applicable au bloc.
                arguments = call.arguments
                if call.name == PROPOSE_BLOCK_EDIT and isinstance(
                    arguments.get("new_markdown"), str
                ):
                    arguments = {
                        **arguments,
                        "new_markdown": refs.rewrite_content_refs(arguments["new_markdown"]),
                    }
                segment_tool_calls.append(
                    {"id": call.id, "name": call.name, "arguments": arguments}
                )
                yield _sse_event(
                    "tool_call",
                    {"id": call.id, "name": call.name, "args": arguments},
                )
            elif event.type == "tool_result":
                # Le segment assistant porteur des tool_calls est clos par
                # l'arrivée du premier résultat.
                held = _flush_segment()
                if held is not None:
                    yield held
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
            elif event.type == "interrupt":
                # Proposition HITL : tour partiel persisté, reprise enregistrée,
                # flux clos SANS done (docstring). Le quota du tour reste
                # consommé (l'appel provider a eu lieu).
                held = _flush_segment()
                if held is not None:
                    yield held
                ids = await _persist_turn(None, None)
                tool_call_id = (event.interrupt_value or {}).get("tool_call_id") or "?"
                replaced = hitl.register(
                    conversation.id,
                    hitl.PendingProposal(
                        thread_id=thread_id or "",
                        tool_call_id=tool_call_id,
                        provider=provider,
                        config=config,
                    ),
                )
                if replaced is not None:
                    client.drop_agent_thread(replaced.thread_id)
                yield _sse_event(
                    "interrupt",
                    {
                        "tool_call_id": tool_call_id,
                        "message_ids": [str(i) for i in ids],
                    },
                )
                return
            else:  # done
                held = _flush_segment()
                if held is not None:
                    yield held
                sources = extract_sources("".join(all_text), block_ids, resource_ids)
                usage = event.usage.model_dump() if event.usage else None
                ids = await _persist_turn(sources, usage)
                if thread_id is not None:
                    client.drop_agent_thread(thread_id)
                yield _sse_event(
                    "done",
                    {
                        "usage": usage,
                        "user_message_id": (
                            str(user_message_id) if user_message_id is not None else None
                        ),
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
        held = _flush_segment()
        if held is not None:
            yield held
        try:
            await _persist_turn(None, None)
        except Exception:  # noqa: BLE001 — best-effort assumé
            pass
        if thread_id is not None:
            client.drop_agent_thread(thread_id)
        yield _sse_event("error", {"status": exc.status_code, "detail": exc.detail})
