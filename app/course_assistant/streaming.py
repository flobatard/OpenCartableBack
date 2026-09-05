"""Flux SSE d'un tour d'assistant de cours (agent) et reprise HITL.

Contrat SSE — extension du contrat de référence de :mod:`app.core.sse` :

.. code-block:: text

    event: token         data: {"delta": "…"}
    event: thinking      data: {"delta": "…"}
    event: tool_call     data: {"id": "…", "name": "read_block", "args": {…}}
    event: tool_result   data: {"id": "…", "name": "…", "is_error": false,
                                "excerpt": "…", "length": 12345}
    event: interrupt     data: {"tool_call_id": "…", "message_ids": ["…"]}
    event: done          data: {"usage": {…}|null, "user_message_id": "…",
                                "message_ids": ["…"], "sources": {…},
                                "title": "…"|null}
    event: error         data: {"status": 503, "detail": "…"}

``interrupt`` n'existe que dans un **contexte d'édition** (flux HITL,
descripteurs de :mod:`app.course_assistant.editing`) : l'agent a appelé un
tool de proposition, le run est figé (checkpointer du client IA, cf.
``hitl.py``), le tour partiel est persisté et le flux se ferme SANS ``done``.
La reprise est le flux SSE de la route de décision (:func:`sse_resume_stream`,
même contrat : ``tool_result``…``done``, ou un nouvel ``interrupt``). La
proposition voyage dans les ``args`` du ``tool_call`` (relayés en entier,
références courtes réécrites en UUID par le descripteur).

Le contenu complet des résultats d'outils ne part jamais sur le flux : seul un
extrait borné l'accompagne (:data:`TOOL_RESULT_EXCERPT_CHARS`), le détail de
conversation sert le reste. ``done`` porte les ids des messages persistés du
tour (le front réconcilie sans refetch) et le titre s'il a été posé.

Persistance (:mod:`app.models.ai_message`) : le message ``user`` est inséré
AVANT l'appel provider (durable même si l'appel échoue) ; le tour — segments
``assistant`` (texte + ``tool_calls``) suivis de leurs lignes ``tool`` — est
inséré à la clôture, le segment final portant ``sources`` et l'usage ; sur
erreur mid-stream, rounds complets et texte partiel sont persistés (l'appel
est compté dès le premier token). La boucle d'encodage est partagée avec le
tuteur d'exercice (:mod:`app.course_assistant.turn_encoder`) : ce module ne
porte que la préparation des tours et leur persistance (:class:`_AssistantTurn`).

Tout est scopé au propriétaire (404 jamais 403) ; l'ordre des ``execute`` de
chaque fonction est un contrat des tests (fausse session FIFO).
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_credentials.service import (
    effective_config,
    refund_default_quota,
    refund_on_error,
)
from app.core.ai import AIClient, AIStreamEvent, AIToolCall, ChatMessage
from app.core.auth import AuthenticatedUser
from app.core.config import settings
from app.core.database import touch
from app.core.http import invalid, not_found
from app.core.storage import Storage
from app.course_assistant import hitl
from app.course_assistant.context import (
    TRUNCATED_HISTORY_NOTICE,
    build_course_context,
    build_refs,
    extract_sources,
    replay_messages,
)
from app.course_assistant.editing import TARGET_MODULE, EditContext, edit_context_for
from app.course_assistant.refs import CourseRefs
from app.course_assistant.schemas import MessageCreate, ProposalDecisionCreate
from app.course_assistant.service import load_conversation, load_messages, load_snapshot
from app.course_assistant.tools import build_tool_executor, build_tool_specs
from app.course_assistant.turn_encoder import TOOL_RESULT_EXCERPT_CHARS as TOOL_RESULT_EXCERPT_CHARS
from app.course_assistant.turn_encoder import encode_turn
from app.courses.queries import get_owned_course
from app.models.ai_conversation import AIConversation
from app.models.ai_message import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER, AIMessage
from app.models.user import User

_TRACE_NAME = "course-assistant"

# Garde-fous (422 au-delà) : les tours tool comptent dans le plafond.
MAX_MESSAGES_PER_CONVERSATION = 300
TITLE_TRUNCATE_CHARS = 80
MAX_TOOL_ROUNDS = 5


def _resolve_focus(
    edit: EditContext | None, conversation: AIConversation, blocks: list, modules: list
) -> tuple[Any, Any]:
    """Cible d'un contexte d'édition, retrouvée dans l'instantané déjà chargé :
    ``(focus_block, focus_module)`` — au plus un des deux, ``(None, None)`` hors
    contexte d'édition. Une cible absente (supprimée : la conversation part en
    cascade avec elle, cas théorique) donne ``None`` — l'appelant décide (404
    défensif à l'aller, tolérance à la reprise). **Seul aiguillage sur
    ``edit.target``** du streaming."""
    if edit is None:
        return None, None
    if edit.target == TARGET_MODULE:
        return None, next((m for m in modules if m.id == conversation.module_id), None)
    return next((b for b in blocks if b.id == conversation.block_id), None), None


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
    que la route ne retourne la réponse : propriété (404), conversation (404),
    plafond de messages (422), cascade IA + quota (422/429/503 — remboursé sur
    erreur eager), validation eager de ``stream_agent``.

    Ordre des execute : 1) cours (contrôle de propriété), 2) conversation
    (scopée), 3) messages existants (historique + plafond), [cascade
    ``effective_config`` : ses propres execute], 4) blocs, 5) ressources,
    6) modules, 7) insert du message user (position suivante ; titre posé au
    premier message ; ``updated_at`` bumpé) puis commit. Le generator retourné
    insère ensuite les messages du tour (un execute + commit à la clôture).

    Contexte d'édition (même ordre d'execute) : la cible éditée — bloc ou
    module selon le descripteur — est retrouvée dans l'instantané (404
    défensif si elle a disparu), mise en avant dans le system prompt, le run
    est **checkpointé** (``thread_id``) et les tools de proposition du
    descripteur sont exposés. Un nouveau message alors qu'une proposition
    attendait abandonne la reprise (registre + thread purgés).
    """
    course = await get_owned_course(db, user, course_id)
    conversation = await load_conversation(db, course, user, conversation_id)
    existing = await load_messages(db, conversation)
    if len(existing) >= MAX_MESSAGES_PER_CONVERSATION:
        raise invalid("Conversation pleine — démarrez-en une nouvelle")

    # ``config`` None = repli serveur AI_* (résolu par AIClient.resolve_config) ;
    # le provider effectif sert au replay (repli inter-provider) et à la
    # colonne ``provider`` des segments persistés.
    config, ticket = await effective_config(db, auth, None)
    provider = config.provider.value if config is not None else settings.AI_PROVIDER

    blocks, resources, modules = await load_snapshot(db, course)

    edit = edit_context_for(conversation.context)
    focus_block, focus_module = _resolve_focus(edit, conversation, blocks, modules)
    if edit is not None and focus_block is None and focus_module is None:
        # Le quota a déjà été réservé par la cascade : remboursé.
        if ticket is not None:
            await refund_default_quota(db, ticket)
        raise not_found(
            "Module introuvable" if edit.target == TARGET_MODULE else "Bloc introuvable"
        )
    # Instantané en références courtes (B1/R1/M1 — et Q1… pour les questions
    # du bloc exercice édité) : le modèle ne manipule jamais d'UUID.
    refs = build_refs(blocks, resources, modules, focus_block=focus_block)
    thread_id: str | None = None
    if edit is not None:
        thread_id = str(uuid.uuid4())
        stale = hitl.drop(conversation.id)
        if stale is not None:
            client.drop_agent_thread(stale.thread_id)
    system_content = build_course_context(
        course, refs, focus_block=focus_block, focus_module=focus_module, edit=edit
    )
    history, truncated = replay_messages(existing, provider)
    if truncated:
        system_content += TRUNCATED_HISTORY_NOTICE
    model_messages = [
        ChatMessage(role="system", content=system_content),
        *history,
        ChatMessage(role="user", content=payload.content),
    ]

    executor = build_tool_executor(storage, refs, edit=edit)

    # Message user durable AVANT l'appel provider (un échec provider ne perd
    # pas la question).
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
    touch(conversation)
    await db.commit()

    async with refund_on_error(db, ticket):
        events = client.stream_agent(
            model_messages,
            config,
            tools=build_tool_specs(refs, edit=edit),
            tool_executor=executor,
            max_tool_rounds=MAX_TOOL_ROUNDS,
            thread_id=thread_id,
            trace_name=_TRACE_NAME,
            user_id=auth.sub,
        )

    sink = _AssistantTurn(
        client=client,
        db=db,
        conversation=conversation,
        refs=refs,
        edit=edit,
        provider=provider,
        config=config,
        thread_id=thread_id,
        base_position=len(existing) + 1,
        user_message_id=user_message_id,
        title_set=title_set,
    )
    return encode_turn(events, db=db, refs=refs, ticket=ticket, sink=sink)


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
    tool de proposition est ré-exécuté, son résultat EST la décision, et la
    réponse est le **SSE de la suite du tour** (même contrat que
    ``stream_message`` : ``tool_result``…``done`` — ou un nouvel ``interrupt``
    si le modèle re-propose après un rejet commenté).

    404 si rien n'attend (proposition inconnue, déjà tranchée, expirée, ou
    perdue — redémarrage). La **config de la reprise est celle du tour
    d'origine** (registre in-process — même provider garanti, pas de nouvelle
    cascade ni de quota : un tour HITL = un appel compté) ; pas de nouveau
    message user, les positions continuent le tour persisté. Le graphe est
    rebâti avec les tools du **même contexte d'édition** (contrat de
    ``stream_agent``).

    Ordre des execute : 1) cours (contrôle de propriété), 2) conversation
    (scopée), 3) messages existants (position suivante), 4) blocs, 5)
    ressources, 6) modules (l'instantané des tools est rechargé — le modèle
    peut encore lire le cours après la décision). Aucune écriture ici : le
    generator persiste la suite du tour à la clôture.
    """
    course = await get_owned_course(db, user, course_id)
    conversation = await load_conversation(db, course, user, conversation_id)
    edit = edit_context_for(conversation.context)
    # Une reprise n'existe que pour un contexte d'édition (le registre n'est
    # consulté — et consommé — qu'à ce titre).
    pending = hitl.take(conversation.id, tool_call_id) if edit is not None else None
    if pending is None:
        raise not_found("Aucune proposition en attente pour cet appel")
    existing = await load_messages(db, conversation)

    blocks, resources, modules = await load_snapshot(db, course)
    # La cible éditée (absente = supprimée pendant la revue, cas théorique :
    # le tool répondra par une erreur actionnable) et la numérotation Q… du
    # tour d'origine, rejouée pour la suite du tour.
    focus_block, _ = _resolve_focus(edit, conversation, blocks, modules)
    refs = build_refs(
        blocks,
        resources,
        modules,
        focus_block=focus_block,
        question_refs=pending.question_refs,
    )

    try:
        events = client.stream_agent(
            [],
            pending.config,
            tools=build_tool_specs(refs, edit=edit),
            tool_executor=build_tool_executor(storage, refs, edit=edit),
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

    sink = _AssistantTurn(
        client=client,
        db=db,
        conversation=conversation,
        refs=refs,
        edit=edit,
        provider=pending.provider,
        config=pending.config,
        thread_id=pending.thread_id,
        base_position=len(existing),
        user_message_id=None,
        title_set=None,
    )
    return encode_turn(events, db=db, refs=refs, ticket=None, sink=sink)


@dataclass
class _AssistantTurn:
    """Sink d'un tour d'assistant : accumule les segments et les persiste.

    Un round du modèle = un segment ``assistant`` (texte + ``tool_calls``)
    clos par l'arrivée du premier ``tool_result``, suivi de ses lignes
    ``tool``. Sur ``interrupt``, le tour PARTIEL est persisté (segment porteur
    du ``tool_call``, sans ligne ``tool`` — un abandon le laissera en round
    incomplet, replié au replay) et la reprise est enregistrée au registre
    ``hitl``. Sur ``done``/erreur, le thread checkpointé est purgé.
    """

    client: AIClient
    db: AsyncSession
    conversation: AIConversation
    refs: CourseRefs
    edit: EditContext | None
    provider: str
    config: Any
    thread_id: str | None
    base_position: int
    user_message_id: uuid.UUID | None
    title_set: str | None
    _all_text: list[str] = field(default_factory=list)
    _turn_rows: list[dict[str, Any]] = field(default_factory=list)
    _segment_text: list[str] = field(default_factory=list)
    _segment_tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def text(self, delta: str) -> None:
        self._segment_text.append(delta)
        self._all_text.append(delta)

    def tool_call(self, call: AIToolCall) -> dict[str, Any]:
        # Proposition d'édition : références courtes des args réécrites en UUID
        # par le descripteur AVANT relais et persistance — le payload reçu par
        # le front est directement applicable.
        arguments = call.arguments
        tool = self.edit.tool(call.name) if self.edit is not None else None
        if tool is not None:
            arguments = tool.rewrite_args(arguments, self.refs)
        self._segment_tool_calls.append({"id": call.id, "name": call.name, "arguments": arguments})
        return arguments

    def tool_result(self, event: AIStreamEvent) -> None:
        self._close_segment()
        self._turn_rows.append(
            {
                "role": ROLE_TOOL,
                "content": event.delta,
                "tool_call_id": event.tool_call.id or "?",
                "is_error": bool(event.tool_result_error),
            }
        )

    async def interrupt(self, event: AIStreamEvent) -> dict[str, Any]:
        self._close_segment()
        ids = await self._persist(None, None)
        tool_call_id = (event.interrupt_value or {}).get("tool_call_id") or "?"
        # Numérotation Q… des questions du bloc édité, rejouée à la reprise
        # (références stables le temps du tour).
        question_refs = {e.ref: str(e.id) for e in self.refs.entries["question"]}
        replaced = hitl.register(
            self.conversation.id,
            hitl.PendingProposal(
                thread_id=self.thread_id or "",
                tool_call_id=tool_call_id,
                provider=self.provider,
                config=self.config,
                question_refs=question_refs or None,
            ),
        )
        if replaced is not None:
            self.client.drop_agent_thread(replaced.thread_id)
        return {"tool_call_id": tool_call_id, "message_ids": [str(i) for i in ids]}

    async def done(self, usage: dict[str, Any] | None) -> dict[str, Any]:
        self._close_segment()
        sources = extract_sources(
            "".join(self._all_text), self.refs.ids("block"), self.refs.ids("resource")
        )
        ids = await self._persist(sources, usage)
        self._drop_thread()
        return {
            "usage": usage,
            "user_message_id": (
                str(self.user_message_id) if self.user_message_id is not None else None
            ),
            "message_ids": [str(i) for i in ids],
            "sources": sources,
            "title": self.title_set,
        }

    async def failed(self) -> None:
        self._close_segment()
        try:
            await self._persist(None, None)
        finally:
            self._drop_thread()

    def _close_segment(self) -> None:
        if self._segment_text or self._segment_tool_calls:
            self._turn_rows.append(
                {
                    "role": ROLE_ASSISTANT,
                    "content": "".join(self._segment_text),
                    "tool_calls": list(self._segment_tool_calls),
                    "provider": self.provider,
                }
            )
            self._segment_text.clear()
            self._segment_tool_calls.clear()

    def _drop_thread(self) -> None:
        if self.thread_id is not None:
            self.client.drop_agent_thread(self.thread_id)

    async def _persist(
        self, sources: dict[str, Any] | None, usage: dict[str, Any] | None
    ) -> list[uuid.UUID]:
        """Insère les lignes du tour (un execute), bump + commit."""
        rows = self._turn_rows
        if not rows:
            return []
        if sources is not None:
            rows[-1]["sources"] = sources
        if usage is not None and rows[-1]["role"] == ROLE_ASSISTANT:
            rows[-1]["input_tokens"] = usage.get("input_tokens")
            rows[-1]["output_tokens"] = usage.get("output_tokens")
        ids = [uuid.uuid4() for _ in rows]
        # Clés homogènes obligatoires (executemany Core) : chaque ligne est
        # normalisée sur le jeu complet de colonnes.
        await self.db.execute(
            insert(AIMessage),
            [
                {
                    "id": row_id,
                    "conversation_id": self.conversation.id,
                    "position": self.base_position + i,
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
                for i, (row_id, row) in enumerate(zip(ids, rows, strict=True))
            ],
        )
        touch(self.conversation)
        await self.db.commit()
        return ids
