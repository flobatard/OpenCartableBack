"""Flux SSE d'un tour d'assistant de cours (agent) et reprise HITL.

Contrat SSE — extension du contrat de référence de :mod:`app.core.sse` :

.. code-block:: text

    event: token         data: {"delta": "…"}
    event: thinking      data: {"delta": "…"}
    event: tool_call     data: {"id": "…", "name": "read_block", "args": {…}}
    event: tool_result   data: {"id": "…", "name": "…", "is_error": false,
                                "excerpt": "…", "length": 12345}
    event: interrupt     data: {"tool_call_id": "…", "kind": "proposal"|"questions",
                                "message_ids": ["…"], "usage": {…}|null}
    event: done          data: {"usage": {…}|null, "user_message_id": "…",
                                "message_ids": ["…"], "sources": {…},
                                "title": "…"|null}
    event: error         data: {"status": 503, "detail": "…"}

``interrupt`` (flux HITL, cf. ``hitl.py``) : l'agent a appelé un tool
bloquant, le run est figé (checkpointer du client IA — tout tour d'assistant
est checkpointé), le tour partiel est persisté et le flux se ferme SANS
``done``. ``kind`` (champ additif) dit ce qui attend le professeur :
``proposal`` — une proposition d'édition, dans un **contexte d'édition**
(descripteurs de :mod:`app.course_assistant.editing`) ou chez un
sous-assistant d'édition (ci-dessous), reprise par la route de décision
(:func:`sse_decision_stream`) — ou ``questions`` — des questions de
l'assistant (:mod:`app.course_assistant.questions`, **tous contextes**),
reprises par la route de réponse (:func:`sse_answer_stream`). Une reprise est
le flux SSE de la suite du tour (même contrat : ``tool_result``…``done``, ou
un nouvel ``interrupt``). La proposition ou les questions voyagent dans les
``args`` du ``tool_call`` (relayés en entier, références courtes d'une
proposition réécrites en UUID par le descripteur).

**Délégation** (:mod:`app.course_assistant.delegation`) : en contexte
``course`` avec l'édition globale activée (``allow_edit`` du message), le tool
bloquant ``edit_block``/``edit_module`` fige le run PARENT d'un interrupt de
genre ``delegation`` — jamais relayé au front ni enregistré au registre : le
driver du flux (:func:`_drive_turn`) lance aussitôt le **sous-assistant** (run
du descripteur d'édition de la cible, sur son propre thread) dans le même
flux, ses événements tagués d'un champ additif ``agent`` (id de l'appel
``edit_*`` — ``token``, ``thinking``, ``tool_call``, ``tool_result``,
``interrupt``). Une proposition ou des questions du sous-assistant suivent le
flux HITL ordinaire (``interrupt`` porteur d'``agent``, registre à
``delegation`` : le thread figé est celui du sous-assistant, le parent
attend derrière) ; la reprise rouvre le run du sous-assistant. Quand il
termine, son compte rendu devient la valeur de reprise du parent — le
résultat du tool ``edit_*``, émis en ``tool_result`` (sans ``agent``) — et le
parent continue : il peut déléguer à nouveau (un sous-assistant à la fois,
plafond :data:`~app.course_assistant.delegation.MAX_DELEGATIONS_PER_TURN` par
tour) puis répond. La transcription du sous-assistant n'est **pas persistée**
(v1) : seuls l'appel ``edit_*`` (args réécrits : cible, contexte, titre) et
son résultat (compte rendu) le sont ; l'usage de ses rounds s'ajoute à celui
du flux.

Un flux refermé sans ``done``, erreur ni ``interrupt`` (Stop, déconnexion)
purge ses threads checkpointés (:func:`_release_on_close`) ; supprimer une
conversation abandonne sa reprise (:func:`drop_pending_resume`) — les threads
d'une délégation (sous-assistant et parent) se purgent ensemble.

Le contenu complet des résultats d'outils ne part jamais sur le flux : seul un
extrait borné l'accompagne (:data:`TOOL_RESULT_EXCERPT_CHARS`), le détail de
conversation sert le reste. ``done`` porte les ids des messages persistés du
tour (le front réconcilie sans refetch) et le titre s'il a été posé.

Persistance (:mod:`app.models.ai_message`) : le message ``user`` est inséré
AVANT l'appel provider (durable même si l'appel échoue) ; le tour — segments
``assistant`` (texte + ``tool_calls``) suivis de leurs lignes ``tool`` — est
inséré à la clôture, le segment final portant ``sources`` et l'usage des
rounds du flux. Un segment clos par ``interrupt`` porte de même l'usage du
flux figé (la reprise repart de zéro) : un tour HITL = plusieurs segments dont
la somme est l'usage du tour ; l'usage d'un flux qui n'a rien à persister
(sous-assistant seul) est reporté sur le segment suivant par le registre.
Sur erreur mid-stream, rounds complets et texte partiel sont persistés, sans
usage (l'appel est compté dès le premier token). La boucle d'encodage est
partagée avec le tuteur d'exercice (:mod:`app.course_assistant.turn_encoder`) :
ce module ne porte que la préparation des tours, le driver et la persistance
(:class:`_AssistantTurn`).

Tout est scopé au propriétaire (404 jamais 403) ; l'ordre des ``execute`` de
chaque fonction est un contrat des tests (fausse session FIFO).
"""

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_credentials.service import (
    QuotaTicket,
    effective_config,
    refund_default_quota,
    refund_on_error,
)
from app.core.ai import AIClient, AIStreamEvent, AIToolCall, AIToolResult, AIToolSpec, ChatMessage
from app.core.auth import AuthenticatedUser
from app.core.config import settings
from app.core.database import touch
from app.core.http import invalid, not_found
from app.core.sse import sse_event
from app.core.storage import Storage
from app.course_assistant import hitl
from app.course_assistant.context import (
    build_refs,
    build_turn_context,
    extract_sources,
    system_prompt_for,
    teacher_message,
    turn_message,
)
from app.course_assistant.delegation import (
    DELEGATION_TOOLS,
    DELEGATIONS_EXCEEDED_NOTICE,
    INTERRUPTED_TEXT,
    MAX_DELEGATIONS_PER_TURN,
    DelegationRequest,
    delegated_message,
    outcome_line,
    recap,
    resume_value,
)
from app.course_assistant.editing import TARGET_MODULE, EditContext, edit_context_for
from app.course_assistant.editing.base import ProposalTool
from app.course_assistant.questions import answers_error
from app.course_assistant.questions import resume_value as questions_resume_value
from app.course_assistant.refs import CourseRefs
from app.course_assistant.replay import TRUNCATED_HISTORY_NOTICE, replay_messages
from app.course_assistant.schemas import (
    MessageCreate,
    ProposalDecisionCreate,
    QuestionAnswerCreate,
)
from app.course_assistant.service import load_conversation, load_messages, load_snapshot
from app.course_assistant.tools import build_tool_executor, build_tool_specs
from app.course_assistant.turn_encoder import TOOL_RESULT_EXCERPT_CHARS as TOOL_RESULT_EXCERPT_CHARS
from app.course_assistant.turn_encoder import encode_turn
from app.courses.queries import get_owned_course
from app.models.ai_conversation import AIConversation
from app.models.ai_message import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER, AIMessage
from app.models.user import User

_TRACE_NAME = "course-assistant"
_DELEGATE_TRACE_NAME = "course-assistant-delegate"

# Garde-fous (422 au-delà) : les tours tool comptent dans le plafond.
MAX_MESSAGES_PER_CONVERSATION = 300
TITLE_TRUNCATE_CHARS = 80
MAX_TOOL_ROUNDS = 5

_TARGET_GONE_TEXT = "La cible de la délégation n'existe plus dans le cours."

Snapshot = tuple[list, list, list]
ToolExecutor = Callable[[AIToolCall], Awaitable[AIToolResult]]


def _find_target(
    edit: EditContext | None, target_id: uuid.UUID | None, blocks: list, modules: list
) -> tuple[Any, Any]:
    """Cible d'un contexte d'édition retrouvée dans l'instantané déjà chargé :
    ``(focus_block, focus_module)`` — au plus un des deux, ``(None, None)``
    hors contexte d'édition ou cible absente. **Seul aiguillage sur
    ``edit.target``** du streaming."""
    if edit is None or target_id is None:
        return None, None
    if edit.target == TARGET_MODULE:
        return None, next((m for m in modules if m.id == target_id), None)
    return next((b for b in blocks if b.id == target_id), None), None


def _resolve_focus(
    edit: EditContext | None, conversation: AIConversation, blocks: list, modules: list
) -> tuple[Any, Any]:
    """Cible de la conversation d'un contexte d'édition (:func:`_find_target`).
    Une cible absente (supprimée : la conversation part en cascade avec elle,
    cas théorique) donne ``None`` — l'appelant décide (404 défensif à l'aller,
    tolérance à la reprise)."""
    if edit is None:
        return None, None
    target_id = conversation.module_id if edit.target == TARGET_MODULE else conversation.block_id
    return _find_target(edit, target_id, blocks, modules)


def _turn_tools(
    storage: Storage, refs: CourseRefs, edit: EditContext | None, *, delegation: bool = False
) -> tuple[list[AIToolSpec], ToolExecutor]:
    """Specs et exécuteur d'un run d'assistant — identiques à l'aller et à
    la reprise (contrat de ``stream_agent``) : lectures du cours, tools de
    proposition du contexte d'édition, tools de délégation (édition globale
    du contexte ``course``), questions au professeur."""
    return (
        build_tool_specs(refs, edit=edit, questions=True, delegation=delegation),
        build_tool_executor(storage, refs, edit=edit, questions=True, delegation=delegation),
    )


def _hitl_tools(edit: EditContext | None, allow_edit: bool) -> dict[str, ProposalTool]:
    """Tools dont les args d'un appel sont réécrits à l'émission (sink) :
    propositions du contexte d'édition, délégations de l'assistant global."""
    tools = {tool.name: tool for tool in edit.tools} if edit is not None else {}
    if allow_edit:
        tools.update({tool.name: tool for tool in DELEGATION_TOOLS})
    return tools


def _drop_threads(client: AIClient, pending: hitl.PendingInterrupt) -> None:
    for thread_id in pending.thread_ids():
        client.drop_agent_thread(thread_id)


def drop_pending_resume(client: AIClient, conversation_id: uuid.UUID) -> None:
    """Abandonne la reprise qui attendait dans une conversation (nouveau
    message, suppression) et balaie les reprises expirées : entrées retirées
    du registre, threads checkpointés purgés (sous-assistant et parent d'une
    délégation compris)."""
    abandoned = [hitl.drop(conversation_id), *hitl.sweep_expired()]
    for pending in abandoned:
        if pending is not None:
            _drop_threads(client, pending)


async def _release_on_close(
    stream: AsyncGenerator[str, None], sink: "_AssistantTurn"
) -> AsyncIterator[str]:
    """Relaie le flux d'un tour et, quoi qu'il arrive à sa fermeture — flux
    consommé, abandon par le client (Stop, déconnexion : annulation ou
    fermeture du generator), exception imprévue —, libère les threads d'un
    tour resté sans suite (:meth:`_AssistantTurn.release`). ``finally``
    synchrone : dans une portée annulée, un ``await`` relèverait
    l'annulation."""
    try:
        async with aclosing(stream) as events:
            async for chunk in events:
                yield chunk
    finally:
        sink.release()


@dataclass(frozen=True)
class _ParentRun:
    """Le run de l'assistant (contexte de la conversation) tel qu'il se
    reprend après un sous-assistant : thread, config et graphe identiques."""

    thread_id: str
    config: Any
    tools: list[AIToolSpec]
    executor: ToolExecutor
    system_prompt: str


@dataclass
class _AgentRun:
    """Le sous-assistant en cours dans le flux (mode agent du sink).

    ``proposals`` : résumé des propositions émises et non encore tranchées
    (id d'appel → ``summary``) ; ``outcomes`` : lignes de compte rendu des
    propositions tranchées ; ``text`` : son texte streamé (jamais persisté,
    abrégé dans le compte rendu) ; ``count`` : rang de la délégation dans le
    tour."""

    call_id: str
    thread_id: str
    edit: EditContext
    refs: CourseRefs
    context: str
    target_id: uuid.UUID
    instructions: str
    count: int
    proposals: dict[str, str | None] = field(default_factory=dict)
    outcomes: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)


def _build_child(
    client: AIClient,
    storage: Storage,
    auth: AuthenticatedUser,
    course,
    snapshot: Snapshot,
    request: DelegationRequest,
    config: Any,
    count: int,
) -> tuple[_AgentRun, AsyncIterator[AIStreamEvent]] | None:
    """Prépare le run d'un sous-assistant sur l'instantané du flux (aucun
    execute) : descripteur d'édition de la cible, références courtes avec la
    cible en focus, contexte du tour (cible en entier + sommaire) et message
    des consignes, tools du descripteur (lectures, propositions, questions),
    thread neuf — ``(run, événements)``. ``None`` si la cible a disparu de
    l'instantané ou si le contexte est inconnu (défensif : le handler a validé
    sur ce même instantané). Validation eager de ``stream_agent`` :
    l'appelant traduit une ``HTTPException``."""
    blocks, resources, modules = snapshot
    edit = edit_context_for(request.context)
    focus_block, focus_module = _find_target(edit, request.target_id, blocks, modules)
    if edit is None or (focus_block is None and focus_module is None):
        return None
    refs = build_refs(blocks, resources, modules, focus_block=focus_block)
    context = build_turn_context(
        course, refs, focus_block=focus_block, focus_module=focus_module, edit=edit
    )
    tools, executor = _turn_tools(storage, refs, edit)
    thread_id = str(uuid.uuid4())
    events = client.stream_agent(
        [
            ChatMessage(role="system", content=edit.system_prompt),
            ChatMessage(
                role="user", content=turn_message(context, delegated_message(request.instructions))
            ),
        ],
        config,
        tools=tools,
        tool_executor=executor,
        max_tool_rounds=MAX_TOOL_ROUNDS,
        thread_id=thread_id,
        trace_name=_DELEGATE_TRACE_NAME,
        user_id=auth.sub,
    )
    run = _AgentRun(
        call_id=request.call_id,
        thread_id=thread_id,
        edit=edit,
        refs=refs,
        context=request.context,
        target_id=request.target_id,
        instructions=request.instructions,
        count=count,
    )
    return run, events


async def _drive_turn(
    client: AIClient,
    db: AsyncSession,
    storage: Storage,
    auth: AuthenticatedUser,
    course,
    snapshot: Snapshot,
    sink: "_AssistantTurn",
    *,
    events: AsyncIterator[AIStreamEvent],
    refs: CourseRefs,
    ticket: QuotaTicket | None,
    parent: _ParentRun,
) -> AsyncIterator[str]:
    """Driver d'un flux : enchaîne les runs (:func:`encode_turn`) tant que le
    sink n'a pas clos le flux — le run de l'assistant, puis, à chaque
    délégation, celui du sous-assistant, puis la reprise de l'assistant avec
    le compte rendu (docstring du module).

    ``events`` est le premier run (assistant, ou sous-assistant repris) ;
    ``refs`` celles de l'assistant (``run_refs`` suit le run courant : le
    rewriter de citations et la réécriture des args travaillent sur les
    siennes). Le ticket de quota ne vaut que pour le premier run (le
    remboursement se joue avant le premier token du flux). Plafond de
    délégations du tour (reprises comprises — ``sink.delegations`` compte
    chaque demande) : au-delà, le tour se clôt sur une notice et ``done``, le
    run parent abandonné (repris, il relancerait sans fin : son plafond de
    rounds repart à chaque reprise ; son appel reste un round incomplet,
    replié au replay). Une exception eager d'un run enchaîné
    (``HTTPException`` traduite) devient un événement ``error`` (le 200 est
    déjà parti), partiel persisté, threads purgés.
    """
    run_refs = sink.agent.refs if sink.agent is not None else refs
    while True:
        async for chunk in encode_turn(
            events, db=db, refs=run_refs, ticket=ticket, sink=sink, agent=sink.agent_id
        ):
            yield chunk
        ticket = None
        if sink.closed:
            return
        resume = sink.take_parent_resume()
        if resume is None:
            request = sink.take_delegation()
            if request is None:
                # Run terminé sans done, interrupt ni délégation : rien à
                # enchaîner (jamais en pratique — ``stream_agent`` émet done).
                return
            sink.delegations += 1
            if sink.delegations > MAX_DELEGATIONS_PER_TURN:
                sink.text(DELEGATIONS_EXCEEDED_NOTICE)
                yield sse_event("token", {"delta": DELEGATIONS_EXCEEDED_NOTICE})
                payload = await sink.done(None)
                if payload is not None:
                    yield sse_event("done", payload)
                return
            try:
                child = _build_child(
                    client,
                    storage,
                    auth,
                    course,
                    snapshot,
                    request,
                    parent.config,
                    sink.delegations,
                )
            except HTTPException as exc:
                yield await sink.fail(exc)
                return
            if child is None:
                resume = resume_value(_TARGET_GONE_TEXT, ok=False)
            else:
                run, events = child
                sink.enter_agent(run)
                run_refs = run.refs
                continue
        # Le sous-assistant a terminé (ou n'a pu être lancé) : l'assistant
        # reprend avec le compte rendu — même thread, même graphe.
        try:
            events = client.stream_agent(
                [ChatMessage(role="system", content=parent.system_prompt)],
                parent.config,
                tools=parent.tools,
                tool_executor=parent.executor,
                max_tool_rounds=MAX_TOOL_ROUNDS,
                thread_id=parent.thread_id,
                resume=resume,
                trace_name=_TRACE_NAME,
                user_id=auth.sub,
            )
        except HTTPException as exc:
            yield await sink.fail(exc)
            return
        run_refs = refs


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

    Messages du modèle : ``[system, *historique, user]`` — le system prompt
    est **statique** par contexte (:func:`system_prompt_for`), l'historique
    est rejoué abrégé (:mod:`app.course_assistant.replay`), et le message
    user du tour porte en tête le **contexte du tour** (cible d'édition en
    entier + sommaire du cours, :func:`build_turn_context`) puis la demande
    du professeur (:func:`turn_message`) : le préfixe cacheable ne change pas
    quand la cible change. Seule la demande brute est persistée.

    Tout tour est **checkpointé** (``thread_id``) : ``ask_questions`` est
    exposé dans tous les contextes. Contexte d'édition (même ordre
    d'execute) : la cible éditée — bloc ou module selon le descripteur — est
    retrouvée dans l'instantané (404 défensif si elle a disparu), rendue en
    entier dans le contexte du tour, et les tools de proposition du
    descripteur sont exposés. Contexte ``course`` à ``allow_edit`` (édition
    globale) : prompt à règle de délégation et tools ``edit_*`` exposés, le
    driver enchaîne les sous-assistants sur l'instantané de ce flux. Un
    nouveau message alors qu'une proposition ou des questions attendaient
    abandonne la reprise (registre + threads purgés, reprises expirées
    balayées au passage — :func:`drop_pending_resume`).
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
    allow_edit = edit is None and payload.allow_edit
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
    thread_id = str(uuid.uuid4())
    drop_pending_resume(client, conversation.id)
    context = build_turn_context(
        course, refs, focus_block=focus_block, focus_module=focus_module, edit=edit
    )
    history, truncated = replay_messages(existing, provider)
    system_prompt = system_prompt_for(edit, allow_edit=allow_edit)
    model_messages = [
        ChatMessage(role="system", content=system_prompt),
        *history,
        ChatMessage(
            role="user",
            content=turn_message(
                context,
                teacher_message(payload.content),
                notice=TRUNCATED_HISTORY_NOTICE if truncated else None,
            ),
        ),
    ]

    tools, executor = _turn_tools(storage, refs, edit, delegation=allow_edit)

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
            tools=tools,
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
        hitl_tools=_hitl_tools(edit, allow_edit),
        allow_edit=allow_edit,
    )
    parent = _ParentRun(
        thread_id=thread_id,
        config=config,
        tools=tools,
        executor=executor,
        system_prompt=system_prompt,
    )
    return _release_on_close(
        _drive_turn(
            client,
            db,
            storage,
            auth,
            course,
            (blocks, resources, modules),
            sink,
            events=events,
            refs=refs,
            ticket=ticket,
            parent=parent,
        ),
        sink,
    )


async def _sse_resume(
    client: AIClient,
    db: AsyncSession,
    storage: Storage,
    auth: AuthenticatedUser,
    user: User,
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    tool_call_id: str,
    *,
    kind: str,
    missing_detail: str,
    build_resume: Callable[[hitl.PendingInterrupt], Any],
) -> AsyncIterator[str]:
    """Reprend un run figé par un tool bloquant (flux HITL) : la réponse du
    professeur devient la valeur de reprise de l'interrupt — le tool est
    ré-exécuté, son résultat EST cette réponse, et le flux retourné est le
    **SSE de la suite du tour** (même contrat que ``stream_message`` :
    ``tool_result``…``done`` — ou un nouvel ``interrupt``).

    404 ``missing_detail`` si rien n'attend pour cet appel et ce genre
    (inconnu, déjà repris, expiré, perdu au redémarrage — ou proposition hors
    contexte d'édition qui ne vient pas d'un sous-assistant). ``build_resume``
    construit la valeur de reprise depuis l'entrée en attente, AVANT qu'elle
    ne soit consommée : son refus (HTTPException, 422) laisse la reprise
    disponible. La **config de la reprise est celle du tour d'origine**
    (registre in-process — même provider garanti, pas de nouvelle cascade ni
    de quota : un tour HITL = un appel compté) ; pas de nouveau message user,
    les positions continuent le tour persisté. Le graphe est rebâti avec les
    tools et le system prompt du **même contexte** — édition globale du tour
    (``allow_edit``) rejouée — (contrat de ``stream_agent``).

    Reprise d'un **sous-assistant** (entrée à ``delegation``) : c'est son run
    qui reprend (descripteur de son contexte, cible retrouvée dans
    l'instantané rechargé, numérotation ``Q…`` rejouée, ses tools et son
    prompt) ; le sink repart en mode agent (compte rendu réamorcé) et le
    driver reprendra l'assistant global, figé derrière, quand le
    sous-assistant aura terminé — dans ce même flux.

    Ordre des execute : 1) cours (contrôle de propriété), 2) conversation
    (scopée) — puis, sans execute : balayage des reprises expirées, entrée en
    attente (404), valeur de reprise (422), consommation — 3) messages
    existants (position suivante), 4) blocs, 5) ressources, 6) modules
    (l'instantané des tools est rechargé — le modèle peut encore lire le cours
    après la réponse). Aucune écriture ici : le generator persiste la suite du
    tour à la clôture.
    """
    course = await get_owned_course(db, user, course_id)
    conversation = await load_conversation(db, course, user, conversation_id)
    edit = edit_context_for(conversation.context)
    for expired in hitl.sweep_expired():
        _drop_threads(client, expired)
    pending = hitl.peek(conversation.id, tool_call_id, kind=kind)
    # Une proposition n'existe que dans un contexte d'édition — ou chez un
    # sous-assistant d'édition (délégation) ; des questions, dans tous.
    if (
        pending is not None
        and kind == hitl.KIND_PROPOSAL
        and edit is None
        and pending.delegation is None
    ):
        pending = None
    if pending is None:
        raise not_found(missing_detail)
    resume = build_resume(pending)
    hitl.take(conversation.id, tool_call_id, kind=kind)
    existing = await load_messages(db, conversation)

    blocks, resources, modules = await load_snapshot(db, course)
    delegation = pending.delegation
    agent: _AgentRun | None = None
    if delegation is None:
        # La cible éditée (absente = supprimée pendant l'attente, cas
        # théorique : le tool répondra par une erreur actionnable) et la
        # numérotation Q… du tour d'origine, rejouée pour la suite du tour.
        allow_edit = edit is None and pending.allow_edit
        focus_block, _ = _resolve_focus(edit, conversation, blocks, modules)
        refs = build_refs(
            blocks,
            resources,
            modules,
            focus_block=focus_block,
            question_refs=pending.question_refs,
        )
        tools, executor = _turn_tools(storage, refs, edit, delegation=allow_edit)
        system_prompt = system_prompt_for(edit, allow_edit=allow_edit)
        run_thread_id = pending.thread_id
        run_messages = [ChatMessage(role="system", content=system_prompt)]
        run_tools, run_executor = tools, executor
        parent_thread_id = pending.thread_id
    else:
        # Sous-assistant repris : son descripteur, sa cible et sa numérotation ;
        # l'assistant global (contexte ``course`` à édition globale) attend derrière.
        allow_edit = True
        run_edit = edit_context_for(delegation.context)
        if run_edit is None:
            raise not_found(missing_detail)
        target_id = uuid.UUID(delegation.target_id)
        focus_block, _ = _find_target(run_edit, target_id, blocks, modules)
        refs = build_refs(blocks, resources, modules)
        child_refs = build_refs(
            blocks,
            resources,
            modules,
            focus_block=focus_block,
            question_refs=pending.question_refs,
        )
        tools, executor = _turn_tools(storage, refs, None, delegation=True)
        system_prompt = system_prompt_for(None, allow_edit=True)
        run_thread_id = pending.thread_id
        run_messages = [ChatMessage(role="system", content=run_edit.system_prompt)]
        run_tools, run_executor = _turn_tools(storage, child_refs, run_edit)
        parent_thread_id = delegation.parent_thread_id
        agent = _AgentRun(
            call_id=delegation.parent_call_id,
            thread_id=pending.thread_id,
            edit=run_edit,
            refs=child_refs,
            context=delegation.context,
            target_id=target_id,
            instructions=delegation.instructions,
            count=delegation.count,
            proposals=(
                {pending.tool_call_id: delegation.pending_summary}
                if kind == hitl.KIND_PROPOSAL
                else {}
            ),
            outcomes=list(delegation.outcomes),
        )

    try:
        # Seul le system prompt (statique, hors état checkpointé) est repassé.
        events = client.stream_agent(
            run_messages,
            pending.config,
            tools=run_tools,
            tool_executor=run_executor,
            max_tool_rounds=MAX_TOOL_ROUNDS,
            thread_id=run_thread_id,
            resume=resume,
            trace_name=_DELEGATE_TRACE_NAME if agent is not None else _TRACE_NAME,
            user_id=auth.sub,
        )
    except Exception:
        # Reprise consommée mais run irrécupérable : threads purgés, le round
        # restera incomplet (replié au replay).
        _drop_threads(client, pending)
        raise

    sink = _AssistantTurn(
        client=client,
        db=db,
        conversation=conversation,
        refs=refs,
        edit=edit,
        provider=pending.provider,
        config=pending.config,
        thread_id=parent_thread_id,
        base_position=len(existing),
        user_message_id=None,
        title_set=None,
        hitl_tools=_hitl_tools(edit, allow_edit),
        allow_edit=allow_edit,
        carried_usage=pending.carried_usage,
        delegations=delegation.count if delegation is not None else 0,
    )
    if agent is not None:
        sink.enter_agent(agent)
    parent = _ParentRun(
        thread_id=parent_thread_id,
        config=pending.config,
        tools=tools,
        executor=executor,
        system_prompt=system_prompt,
    )
    return _release_on_close(
        _drive_turn(
            client,
            db,
            storage,
            auth,
            course,
            (blocks, resources, modules),
            sink,
            events=events,
            refs=refs,
            ticket=None,
            parent=parent,
        ),
        sink,
    )


async def sse_decision_stream(
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
    """Décision du professeur sur une proposition d'édition en attente :
    reprise (:func:`_sse_resume`, même ordre d'execute) dont la valeur est
    ``{"accepted", "comment"}`` — le texte du tool dit la décision."""
    return await _sse_resume(
        client,
        db,
        storage,
        auth,
        user,
        course_id,
        conversation_id,
        tool_call_id,
        kind=hitl.KIND_PROPOSAL,
        missing_detail="Aucune proposition en attente pour cet appel",
        build_resume=lambda _pending: {"accepted": payload.accepted, "comment": payload.comment},
    )


async def sse_answer_stream(
    client: AIClient,
    db: AsyncSession,
    storage: Storage,
    auth: AuthenticatedUser,
    user: User,
    course_id: uuid.UUID,
    conversation_id: uuid.UUID,
    tool_call_id: str,
    payload: QuestionAnswerCreate,
) -> AsyncIterator[str]:
    """Réponse du professeur aux questions de l'assistant en attente : reprise
    (:func:`_sse_resume`, même ordre d'execute) dont la valeur est
    ``{"declined", "answers"}`` (:func:`~app.course_assistant.questions.resume_value`).
    Une réponse qui ne correspond pas aux questions posées — nombre, choix
    inconnus ou en double, question sans réponse, plusieurs choix pour une
    question à choix unique — est un 422 qui laisse la reprise disponible."""

    def _resume(pending: hitl.PendingInterrupt) -> dict[str, Any]:
        value = questions_resume_value(
            payload.declined, [answer.model_dump() for answer in payload.answers or []]
        )
        if not payload.declined:
            error = answers_error(pending.answer_shape or [], value["answers"])
            if error is not None:
                raise invalid(error)
        return value

    return await _sse_resume(
        client,
        db,
        storage,
        auth,
        user,
        course_id,
        conversation_id,
        tool_call_id,
        kind=hitl.KIND_QUESTIONS,
        missing_detail="Aucune question en attente pour cet appel",
        build_resume=_resume,
    )


_USAGE_KEYS = ("input_tokens", "output_tokens")


def _add_usage(total: dict[str, Any] | None, usage: Any) -> dict[str, Any] | None:
    """Cumul de deux usages (dict ou :class:`AIUsage`) : ``None`` tant qu'aucun
    provider n'en a relayé ; ``cached_input_tokens`` reste ``None`` sans détail."""
    if usage is None:
        return total
    data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    if total is None:
        total = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": None}
    for key in _USAGE_KEYS:
        total[key] = (total.get(key) or 0) + (data.get(key) or 0)
    cached = data.get("cached_input_tokens")
    if cached is not None:
        total["cached_input_tokens"] = (total.get("cached_input_tokens") or 0) + cached
    return total


@dataclass
class _AssistantTurn:
    """Sink d'un flux d'assistant : accumule les segments et les persiste.

    Un round du modèle = un segment ``assistant`` (texte + ``tool_calls``)
    clos par l'arrivée du premier ``tool_result``, suivi de ses lignes
    ``tool``. Sur ``interrupt``, le tour PARTIEL est persisté (segment porteur
    du ``tool_call`` et de l'usage du flux figé, sans ligne ``tool`` — un
    abandon le laissera en round incomplet, replié au replay) et la reprise
    est enregistrée au registre ``hitl`` (genre et forme de réponse lus dans
    le payload de l'interrupt ; ``allow_edit`` du tour ; derrière un
    sous-assistant, le lien vers le parent figé). Sur ``done``/erreur — ou à
    la fermeture d'un flux resté sans suite (:meth:`release`) —, les threads
    checkpointés du flux sont purgés, une seule fois.

    **Mode agent** (:meth:`enter_agent`, un sous-assistant tourne) : rien de
    ce qu'il émet n'est persisté — son texte s'accumule pour le compte rendu,
    ses propositions (args réécrits par son descripteur) et leurs décisions
    forment les lignes du compte rendu ; son ``done`` est absorbé (valeur de
    reprise du parent, :meth:`take_parent_resume`) et son thread purgé. Un
    interrupt de genre ``delegation`` du parent est retenu pour le driver
    (:meth:`take_delegation`), jamais relayé.

    Usage : ``stream_usage`` cumule les runs du flux (relayé dans
    ``interrupt``/``done``) ; ``carried_usage`` est celui d'un flux
    précédent resté sans ligne à persister, ajouté à la persistance.
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
    hitl_tools: dict[str, ProposalTool] = field(default_factory=dict)
    allow_edit: bool = False
    carried_usage: dict[str, Any] | None = None
    agent: _AgentRun | None = None
    closed: bool = False
    # Délégations lancées dans le flux (rang de la dernière, reprises comprises).
    delegations: int = 0
    stream_usage: dict[str, Any] | None = None
    _all_text: list[str] = field(default_factory=list)
    _turn_rows: list[dict[str, Any]] = field(default_factory=list)
    _segment_text: list[str] = field(default_factory=list)
    _segment_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    _delegation: DelegationRequest | None = None
    _parent_resume: dict[str, Any] | None = None
    _threads: set[str] = field(default_factory=set)
    _dropped: set[str] = field(default_factory=set)
    # Reprise enregistrée : les threads doivent survivre à la fermeture du flux.
    _suspended: bool = False

    def __post_init__(self) -> None:
        if self.thread_id:
            self._threads.add(self.thread_id)

    # ------------------------------------------------------------ mode agent

    @property
    def agent_id(self) -> str | None:
        """Id de l'appel de délégation du sous-assistant en cours (tag ``agent``)."""
        return self.agent.call_id if self.agent is not None else None

    def enter_agent(self, run: _AgentRun) -> None:
        """Le flux relaie désormais le run d'un sous-assistant."""
        self.agent = run
        self._threads.add(run.thread_id)

    def take_delegation(self) -> DelegationRequest | None:
        """La délégation demandée par l'assistant (interrupt ``delegation``), une fois."""
        request, self._delegation = self._delegation, None
        return request

    def take_parent_resume(self) -> dict[str, Any] | None:
        """La valeur de reprise de l'assistant, quand le sous-assistant a
        terminé (compte rendu) ou n'a pu être lancé, une fois."""
        resume, self._parent_resume = self._parent_resume, None
        return resume

    # ------------------------------------------------------------ TurnSink

    def text(self, delta: str) -> None:
        if self.agent is not None:
            self.agent.text.append(delta)
            return
        self._segment_text.append(delta)
        self._all_text.append(delta)

    def tool_call(self, call: AIToolCall) -> dict[str, Any]:
        # Références courtes des args réécrites en UUID par le descripteur
        # (proposition d'édition) ou le tool de délégation (cible résolue)
        # AVANT relais et persistance — le payload reçu par le front est
        # directement applicable.
        run = self.agent
        if run is not None:
            tool = run.edit.tool(call.name)
            if tool is None:
                return call.arguments
            arguments = tool.rewrite_args(call.arguments, run.refs)
            summary = arguments.get("summary")
            run.proposals[call.id or "?"] = (
                summary if isinstance(summary, str) and summary else None
            )
            return arguments
        arguments = call.arguments
        tool = self.hitl_tools.get(call.name)
        if tool is not None:
            arguments = tool.rewrite_args(arguments, self.refs)
        self._segment_tool_calls.append({"id": call.id, "name": call.name, "arguments": arguments})
        return arguments

    def tool_result(self, event: AIStreamEvent) -> None:
        run = self.agent
        if run is not None:
            # Décision du professeur sur une proposition du sous-assistant :
            # une ligne du compte rendu (les lectures et questions n'y sont pas).
            call_id = event.tool_call.id or "?"
            if call_id in run.proposals:
                summary = run.proposals.pop(call_id)
                run.outcomes.append(outcome_line(event.tool_call.name, summary, event.delta))
            return
        self._close_segment()
        self._turn_rows.append(
            {
                "role": ROLE_TOOL,
                "content": event.delta,
                "tool_call_id": event.tool_call.id or "?",
                "is_error": bool(event.tool_result_error),
            }
        )

    async def interrupt(self, event: AIStreamEvent) -> dict[str, Any] | None:
        # Usage des rounds déjà joués par ce run (la reprise repart de zéro),
        # cumulé sur le flux — sans ``done``, il serait perdu.
        self.stream_usage = _add_usage(self.stream_usage, event.usage)
        value = event.interrupt_value or {}
        kind = value.get("kind") or hitl.KIND_PROPOSAL
        if kind == hitl.KIND_DELEGATION:
            # L'assistant délègue : rien n'attend le professeur — le driver
            # lance le sous-assistant (ou reprend l'assistant sur une
            # demande malformée, défensif). Son segment reste ouvert : le
            # sous-assistant n'y écrit pas (mode agent), la suite du round
            # (résultat du tool, ou notice de plafond) le clôt.
            request = DelegationRequest.from_interrupt(value)
            if request is None:
                self._parent_resume = resume_value(INTERRUPTED_TEXT, ok=False)
            else:
                self._delegation = request
            return None
        self._close_segment()
        ids = await self._persist(None, self._persisted_usage())
        tool_call_id = value.get("tool_call_id") or "?"
        run = self.agent
        run_refs = run.refs if run is not None else self.refs
        # Numérotation Q… des questions du bloc édité, rejouée à la reprise
        # (références stables le temps du tour).
        question_refs = {e.ref: str(e.id) for e in run_refs.entries["question"]}
        delegation = None
        if run is not None:
            delegation = hitl.Delegation(
                parent_thread_id=self.thread_id or "",
                parent_call_id=run.call_id,
                context=run.context,
                target_id=str(run.target_id),
                instructions=run.instructions,
                outcomes=tuple(run.outcomes),
                pending_summary=run.proposals.get(tool_call_id),
                count=run.count,
            )
        replaced = hitl.register(
            self.conversation.id,
            hitl.PendingInterrupt(
                thread_id=run.thread_id if run is not None else (self.thread_id or ""),
                tool_call_id=tool_call_id,
                provider=self.provider,
                config=self.config,
                question_refs=question_refs or None,
                kind=kind,
                answer_shape=value.get("answer_shape"),
                allow_edit=self.allow_edit,
                delegation=delegation,
                # Rien de persisté dans ce flux : son usage attend le segment suivant.
                carried_usage=None if ids else self._persisted_usage(),
            ),
        )
        self._suspended = True
        self.closed = True
        if replaced is not None:
            for thread in replaced.thread_ids():
                if thread not in self._threads:
                    self.client.drop_agent_thread(thread)
        return {
            "tool_call_id": tool_call_id,
            "kind": kind,
            "message_ids": [str(i) for i in ids],
            "usage": self.stream_usage,
        }

    async def done(self, usage: dict[str, Any] | None) -> dict[str, Any] | None:
        self.stream_usage = _add_usage(self.stream_usage, usage)
        run = self.agent
        if run is not None:
            # Sous-assistant terminé : compte rendu pour l'assistant, thread
            # purgé — le flux continue (aucun ``done`` émis).
            self.agent = None
            self._parent_resume = resume_value(recap(run.outcomes, "".join(run.text)))
            self._drop_one(run.thread_id)
            return None
        self._close_segment()
        sources = extract_sources(
            "".join(self._all_text), self.refs.ids("block"), self.refs.ids("resource")
        )
        ids = await self._persist(sources, self._persisted_usage())
        self.closed = True
        self._drop_thread()
        return {
            "usage": self.stream_usage,
            "user_message_id": (
                str(self.user_message_id) if self.user_message_id is not None else None
            ),
            "message_ids": [str(i) for i in ids],
            "sources": sources,
            "title": self.title_set,
        }

    async def failed(self) -> None:
        self.closed = True
        self._close_segment()
        try:
            await self._persist(None, None)
        finally:
            self._drop_thread()

    async def fail(self, exc: HTTPException) -> str:
        """Échec eager d'un run enchaîné par le driver (le 200 est parti) :
        partiel persisté best-effort, événement ``error``."""
        try:
            await self.failed()
        except Exception:  # noqa: BLE001 — best-effort : ne jamais masquer l'erreur provider
            pass
        return sse_event("error", {"status": exc.status_code, "detail": exc.detail})

    # ------------------------------------------------------------- internes

    def _persisted_usage(self) -> dict[str, Any] | None:
        """Usage à poser sur le segment persisté : celui du flux, plus celui
        reporté d'un flux précédent sans ligne."""
        return _add_usage(_add_usage(None, self.carried_usage), self.stream_usage)

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

    def _drop_one(self, thread_id: str) -> None:
        if thread_id not in self._dropped:
            self._dropped.add(thread_id)
            self._threads.discard(thread_id)
            self.client.drop_agent_thread(thread_id)

    def _drop_thread(self) -> None:
        """Purge tous les threads vivants du flux (assistant et sous-assistant), une fois."""
        for thread_id in sorted(self._threads):
            self._drop_one(thread_id)

    def release(self) -> None:
        """Flux refermé : purge les threads d'un tour resté sans suite — ni
        suspendu (reprise en attente), ni clos (``done``/``failed`` les ont
        déjà purgés) : abandon par le client ou exception imprévue."""
        if not self._suspended:
            self._drop_thread()

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
            rows[-1]["cached_input_tokens"] = usage.get("cached_input_tokens")
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
                    "cached_input_tokens": row.get("cached_input_tokens"),
                }
                for i, (row_id, row) in enumerate(zip(ids, rows, strict=True))
            ],
        )
        touch(self.conversation)
        await self.db.commit()
        return ids
