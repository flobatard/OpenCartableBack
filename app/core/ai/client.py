"""Le client IA générique : stateless, BYO token, trois modes d'appel.

- :meth:`AIClient.complete` — appel classique (``ainvoke``), réponse complète ;
- :meth:`AIClient.stream` — flux de :class:`AIStreamEvent` (``astream``),
  destiné à être servi en SSE par les routes ;
- :meth:`AIClient.stream_agent` — boucle agent avec tools (LangGraph via le
  ``create_agent`` de langchain), même flux d'événements neutres, enrichi de
  ``tool_call``/``tool_result``.

La config (provider, clé, modèle) voyage **à chaque appel** ; à défaut, le
fallback serveur optionnel ``AI_*`` s'applique (:meth:`resolve_config`).

``stream()`` et ``stream_agent()`` sont volontairement **eager** sur la
validation : la résolution de config, la construction du chat model (et du
graphe agent) se font AVANT de retourner l'async generator — une config
invalide produit ainsi un vrai status HTTP 4xx, pas une erreur en plein flux.
Seules les erreurs survenant pendant l'itération sont levées depuis le
generator (le routeur les convertit en événement SSE ``error``, le status HTTP
200 étant déjà parti). Nuance agent : le support du tool-calling par le
provider n'est vérifiable qu'à l'appel — un provider qui le refuse produit une
erreur mid-stream, traduite comme les autres.
"""

import contextvars
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr, ValidationError

from app.core.ai.errors import invalid_config, translate_provider_error
from app.core.ai.observability import build_run_config
from app.core.ai.providers import build_chat_model
from app.core.ai.types import (
    AICompletion,
    AIRequestConfig,
    AIStreamEvent,
    AIToolCall,
    AIToolImage,
    AIToolResult,
    AIToolSpec,
    AIUsage,
    ChatMessage,
)
from app.core.config import settings

if TYPE_CHECKING:  # uniquement pour les annotations — jamais importé au runtime
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessageChunk, BaseMessage
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

# Nœuds LangGraph dont les messages sont relayés au flux ; tout autre nœud
# (middlewares) est filtré — son contenu (ex. l'avis anglais du plafond
# d'appels) ne doit jamais fuiter en token.
_MODEL_NODE = "model"
_TOOLS_NODE = "tools"

# Id de l'appel d'outil EN COURS d'exécution, posé par le middleware
# (``wrap_tool_call``) et lu par la coroutine des ``StructuredTool`` : nos
# tools sont déclarés par un args_schema JSON, dans lequel LangChain ne peut
# pas injecter l'id d'appel — or l'exécuteur doit pouvoir apparier son
# exécution à l'événement ``tool_call`` émis sur le flux (clé des attentes
# HITL de l'assistant, notamment). ContextVar : les appels concurrents d'un
# même round s'exécutent chacun dans leur propre tâche/contexte asyncio.
_CURRENT_TOOL_CALL_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "oc_current_tool_call_id", default=""
)

# Injecté en token quand le plafond de rounds coupe la boucle : l'appelant
# persiste/affiche ainsi une fin de réponse explicite plutôt qu'un silence.
_TOOL_ROUNDS_EXCEEDED_NOTICE = (
    "\n\n*Limite d'appels au modèle atteinte pour cette réponse — synthèse interrompue.*"
)

# Contenu de repli quand l'exécuteur de tools viole son contrat en levant une
# exception : le modèle reçoit un échec générique, jamais le message brut.
_TOOL_FAILURE_FALLBACK = "Échec interne de l'outil"

# Marqueur (``additional_kwargs``) des messages utilisateur porteurs d'une
# image d'outil, posés dans l'état par le middleware ; ils sont déplacés
# après le run de résultats d'outils du round avant chaque appel modèle.
_TOOL_IMAGE_MARKER = "oc_tool_image"
_TOOL_IMAGE_DEFAULT_CAPTION = "Image jointe au résultat de l'outil {name}."


def _to_langchain_messages(messages: Sequence[ChatMessage]) -> list["BaseMessage"]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    converted: list[BaseMessage] = []
    for m in messages:
        if m.role == "tool":
            converted.append(
                ToolMessage(
                    content=m.content,
                    tool_call_id=m.tool_call_id or "",
                    status="error" if m.is_error else "success",
                )
            )
        elif m.role == "assistant" and m.tool_calls:
            converted.append(
                AIMessage(
                    content=m.content,
                    tool_calls=[
                        {"name": tc.name, "args": tc.arguments, "id": tc.id or None}
                        for tc in m.tool_calls
                    ],
                )
            )
        else:
            lc_classes = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
            converted.append(lc_classes[m.role](content=m.content))
    return converted


def _delta_events(chunk: "AIMessageChunk") -> Iterator[AIStreamEvent]:
    """Événements ``thinking``/``token`` portés par un chunk de modèle.

    Les blocs « reasoning » sont lus via ``content_blocks`` (représentation
    standardisée de langchain-core 1.x, tous providers) ; ``chunk.text`` ne
    contient jamais le raisonnement.
    """
    for block in chunk.content_blocks:
        if block.get("type") == "reasoning":
            reasoning = block.get("reasoning", "")
            if reasoning:
                yield AIStreamEvent(type="thinking", delta=reasoning)
    if chunk.text:
        yield AIStreamEvent(type="token", delta=chunk.text)


def _to_usage(usage_metadata: dict[str, Any] | None) -> AIUsage | None:
    """Normalise l'``usage_metadata`` LangChain (absent chez certains providers)."""
    if not usage_metadata:
        return None
    return AIUsage(
        input_tokens=usage_metadata.get("input_tokens"),
        output_tokens=usage_metadata.get("output_tokens"),
    )


def agent_interrupt(payload: dict[str, Any]) -> Any:
    """Fige le run agent en attendant une reprise (HITL) — wrapper NEUTRE de
    ``langgraph.types.interrupt``, seul point d'appel autorisé hors du package.

    À appeler UNIQUEMENT depuis un ``tool_executor`` d'un run à ``thread_id``
    (checkpointer requis). ``payload`` est relayé à l'appelant du flux dans
    l'événement ``interrupt`` (l'appelant y met de quoi retrouver la reprise —
    ex. l'id d'appel d'outil) ; au resume, la fonction RETOURNE la valeur de
    reprise. ⚠ À la reprise, le nœud (donc le tool) est **ré-exécuté depuis le
    début** : tout ce qui précède l'appel doit être idempotent.
    """
    from langgraph.types import interrupt

    return interrupt(payload)


class AIClient:
    """Client IA stateless sur les appels — la seule mémoire est le
    **checkpointer agent** (InMemorySaver, process-local) qui porte les runs
    figés par :func:`agent_interrupt` entre deux requêtes (HITL). Décision
    actée : passage à un saver Postgres seulement si multi-worker un jour."""

    def __init__(self) -> None:
        self._checkpointer: Any = None

    def _get_checkpointer(self) -> Any:
        """InMemorySaver partagé du client (créé paresseusement) : les runs
        figés ne sont retrouvables que sur l'instance qui les a créés —
        ``get_ai_client`` étant un singleton, c'est le cas en production."""
        if self._checkpointer is None:
            from langgraph.checkpoint.memory import InMemorySaver

            self._checkpointer = InMemorySaver()
        return self._checkpointer

    def drop_agent_thread(self, thread_id: str) -> None:
        """Purge l'état checkpointé d'un run (reprise consommée ou abandonnée) —
        best-effort, la mémoire est de toute façon libérée au redémarrage."""
        if self._checkpointer is None:
            return
        try:
            self._checkpointer.delete_thread(thread_id)
        except Exception:  # noqa: BLE001 — nettoyage best-effort assumé
            logger.warning("Purge du thread agent %s impossible", thread_id)

    def resolve_config(self, config: AIRequestConfig | None) -> AIRequestConfig:
        """Config fournie par l'appelant, sinon fallback serveur ``AI_*``.

        422 si aucune des deux n'est exploitable — c'est le point unique où le
        « BYO token » rencontre le fallback : les futures features n'ont pas à
        connaître les settings.
        """
        if config is not None:
            return config
        if not (settings.AI_PROVIDER and settings.AI_MODEL):
            raise invalid_config(
                "Aucune configuration IA fournie et pas de fallback serveur configuré"
            )
        try:
            return AIRequestConfig(
                provider=settings.AI_PROVIDER,
                model=settings.AI_MODEL,
                api_key=SecretStr(settings.AI_API_KEY) if settings.AI_API_KEY else None,
                base_url=settings.AI_BASE_URL or None,
            )
        except ValidationError as exc:
            raise invalid_config(
                "Fallback serveur IA invalide (vérifier AI_PROVIDER/AI_MODEL)"
            ) from exc

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        config: AIRequestConfig | None = None,
        *,
        trace_name: str | None = None,
        user_id: str | None = None,
    ) -> AICompletion:
        """Appel classique : la réponse complète, avec l'usage si le provider le relaie."""
        cfg = self.resolve_config(config)
        model = build_chat_model(cfg)
        run_config = build_run_config(trace_name=trace_name, user_id=user_id)
        try:
            result = await model.ainvoke(_to_langchain_messages(messages), config=run_config)
        except Exception as exc:
            raise translate_provider_error(exc, cfg.provider.value) from exc
        return AICompletion(
            content=result.text,
            provider=cfg.provider.value,
            model=cfg.model,
            usage=_to_usage(result.usage_metadata),
        )

    def stream(
        self,
        messages: Sequence[ChatMessage],
        config: AIRequestConfig | None = None,
        *,
        trace_name: str | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[AIStreamEvent]:
        """Flux d'événements ``token``… puis ``done`` (usage agrégé si fourni).

        Méthode SYNC qui valide/construit tout de suite (erreurs de config →
        vrai 4xx HTTP) et retourne le generator lazy — voir la docstring module.
        """
        cfg = self.resolve_config(config)
        model = build_chat_model(cfg)
        run_config = build_run_config(trace_name=trace_name, user_id=user_id)
        return self._stream(model, cfg, messages, run_config)

    async def _stream(
        self,
        model: "BaseChatModel",
        cfg: AIRequestConfig,
        messages: Sequence[ChatMessage],
        run_config: dict[str, Any],
    ) -> AsyncIterator[AIStreamEvent]:
        usage: AIUsage | None = None
        try:
            async for chunk in model.astream(_to_langchain_messages(messages), config=run_config):
                # L'usage arrive (chez certains providers) sur un dernier chunk
                # au delta vide : on l'agrège sans émettre de token vide.
                if chunk.usage_metadata:
                    usage = _to_usage(chunk.usage_metadata)
                for event in _delta_events(chunk):
                    yield event
        except Exception as exc:
            raise translate_provider_error(exc, cfg.provider.value) from exc
        yield AIStreamEvent(type="done", usage=usage)

    def stream_agent(
        self,
        messages: Sequence[ChatMessage],
        config: AIRequestConfig | None = None,
        *,
        tools: Sequence[AIToolSpec],
        tool_executor: Callable[[AIToolCall], Awaitable[AIToolResult]],
        max_tool_rounds: int = 5,
        thread_id: str | None = None,
        resume: Any = None,
        trace_name: str | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[AIStreamEvent]:
        """Boucle agent avec tools : ``thinking``/``token``/``tool_call``/``tool_result``… ``done``.

        Méthode SYNC à validation eager (motif :meth:`stream`) : config résolue,
        chat model construit et graphe agent compilé AVANT de retourner le
        generator. ``tool_executor`` reçoit chaque :class:`AIToolCall` (``id``
        réel de l'appel, propagé par contextvar) et retourne un
        :class:`AIToolResult` — **il ne lève jamais** : une erreur métier se dit
        ``is_error=True`` (relayée au modèle en résultat d'outil en échec) ; une
        exception imprévue est rattrapée et convertie en échec générique, sans
        fuiter son message.

        **HITL** : avec un ``thread_id``, le run est checkpointé (InMemorySaver
        du client) — un ``tool_executor`` peut alors appeler
        :func:`agent_interrupt` pour figer le run : le flux émet un événement
        ``interrupt`` (portant le payload) et se termine SANS ``done``. La
        reprise est un nouvel appel avec le même ``thread_id`` et
        ``resume=<valeur>`` (les ``messages`` sont alors ignorés — l'état vit
        au checkpoint) : le nœud interrompu se ré-exécute, ``agent_interrupt``
        retourne la valeur, le flux continue (``tool_result``… ``done``).
        Le graphe de la reprise doit être bâti avec les MÊMES tools.

        ``max_tool_rounds`` borne le nombre d'appels au modèle (rounds de tools
        + réponse finale) ; à la coupure, un token d'avertissement clôt le texte
        (:data:`_TOOL_ROUNDS_EXCEEDED_NOTICE`) — jamais de boucle infinie.
        L'usage de ``done`` cumule tous les rounds. Un résultat porteur d'une
        :class:`AIToolImage` est montré au modèle (message utilisateur joint,
        cf. :func:`_tool_image_middleware`) ; seul son ``content`` texte est
        relayé en ``tool_result``.
        """
        cfg = self.resolve_config(config)
        model = build_chat_model(cfg)
        checkpointer = self._get_checkpointer() if thread_id is not None else None
        agent = _build_agent(model, tools, tool_executor, max_tool_rounds, checkpointer)
        run_config = build_run_config(trace_name=trace_name, user_id=user_id)
        if thread_id is not None:
            run_config["configurable"] = {
                **run_config.get("configurable", {}),
                "thread_id": thread_id,
            }
        return self._stream_agent(agent, cfg, messages, run_config, resume=resume)

    async def _stream_agent(
        self,
        agent: "CompiledStateGraph",
        cfg: AIRequestConfig,
        messages: Sequence[ChatMessage],
        run_config: dict[str, Any],
        *,
        resume: Any = None,
    ) -> AsyncIterator[AIStreamEvent]:
        agent_input: Any = {"messages": _to_langchain_messages(messages)}
        if resume is not None:
            from langgraph.types import Command

            agent_input = Command(resume=resume)
        input_tokens = 0
        output_tokens = 0
        has_usage = False
        interrupted = False
        try:
            async for mode, payload in agent.astream(
                agent_input,
                config=run_config,
                stream_mode=["messages", "updates"],
            ):
                if mode == "messages":
                    chunk, metadata = payload
                    node = metadata.get("langgraph_node")
                    if node == _MODEL_NODE:
                        if chunk.usage_metadata:
                            has_usage = True
                            input_tokens += chunk.usage_metadata.get("input_tokens") or 0
                            output_tokens += chunk.usage_metadata.get("output_tokens") or 0
                        for event in _delta_events(chunk):
                            yield event
                    elif node == _TOOLS_NODE:
                        if getattr(chunk, "type", None) != "tool":
                            # Message utilisateur image joint par le middleware
                            # (cf. _tool_image_middleware) : jamais relayé.
                            continue
                        # ToolMessage complet : le delta porte le contenu pour
                        # la persistance par l'appelant (les routes SSE ne le
                        # relaient pas au navigateur).
                        yield AIStreamEvent(
                            type="tool_result",
                            delta=chunk.text,
                            tool_call=AIToolCall(
                                id=chunk.tool_call_id or "",
                                name=getattr(chunk, "name", None) or "",
                            ),
                            tool_result_error=chunk.status == "error",
                        )
                    continue

                # stream_mode="updates" : sorties de nœuds, émises à la fin de
                # chaque super-step — l'update du nœud modèle arrive AVANT
                # l'exécution des tools, c'est lui qui porte les tool_calls
                # complets (les chunks n'en donnent que des fragments).
                # Un interrupt (agent_interrupt dans un tool) arrive ici sous
                # la clé "__interrupt__" avec un TUPLE d'Interrupt — traité en
                # premier (la boucle par nœud attend des dicts).
                if "__interrupt__" in payload:
                    for intr in payload["__interrupt__"]:
                        interrupted = True
                        yield AIStreamEvent(
                            type="interrupt",
                            interrupt_id=getattr(intr, "id", None),
                            interrupt_value=intr.value if isinstance(intr.value, dict) else {},
                        )
                    continue
                for node, output in payload.items():
                    node_messages = (output or {}).get("messages", [])
                    if node == _MODEL_NODE:
                        for message in node_messages:
                            for tc in getattr(message, "tool_calls", None) or []:
                                yield AIStreamEvent(
                                    type="tool_call",
                                    tool_call=AIToolCall(
                                        id=tc.get("id") or "",
                                        name=tc["name"],
                                        arguments=tc.get("args") or {},
                                    ),
                                )
                    elif node.startswith("ModelCallLimitMiddleware") and node_messages:
                        yield AIStreamEvent(type="token", delta=_TOOL_ROUNDS_EXCEEDED_NOTICE)
        except Exception as exc:
            raise translate_provider_error(exc, cfg.provider.value) from exc
        if interrupted:
            # Run figé (HITL) : pas de ``done`` — l'appelant clôt son flux et
            # attend la reprise (nouvel appel avec resume=).
            return
        usage = AIUsage(input_tokens=input_tokens, output_tokens=output_tokens)
        yield AIStreamEvent(type="done", usage=usage if has_usage else None)


def _build_agent(
    model: "BaseChatModel",
    tools: Sequence[AIToolSpec],
    tool_executor: Callable[[AIToolCall], Awaitable[AIToolResult]],
    max_tool_rounds: int,
    checkpointer: Any = None,
) -> "CompiledStateGraph":
    """Compile le graphe agent LangGraph autour des tools neutres.

    Chaque :class:`AIToolSpec` est enrobé en ``StructuredTool`` async dont la
    coroutine délègue à ``tool_executor`` — aucun type langchain/langgraph ne
    franchit la frontière du package. Un résultat ``is_error=True`` est relayé
    au modèle en ``ToolMessage`` de statut ``error`` (via ``ToolException`` +
    ``handle_tool_error``), jamais en exception du flux.

    Le plafond de rounds passe par ``ModelCallLimitMiddleware`` (sortie propre
    ``exit_behavior="end"``) : ``max_tool_rounds`` rounds d'outils + l'appel de
    synthèse finale.

    Une :class:`AIToolImage` retournée par l'exécuteur voyage en **artefact**
    du ``ToolMessage`` (``response_format="content_and_artifact"`` — le
    contenu reste le texte) ; le middleware de :func:`_tool_image_middleware`
    la joint au modèle dans un message utilisateur.
    """
    from langchain.agents import create_agent
    from langchain.agents.middleware import ModelCallLimitMiddleware
    from langchain_core.tools import StructuredTool, ToolException
    from langgraph.errors import GraphBubbleUp

    lc_tools = []
    for spec in tools:

        async def _run(
            _spec: AIToolSpec = spec, **arguments: Any
        ) -> tuple[str, AIToolImage | None]:
            # L'id vient du middleware (contextvar) : celui de l'événement
            # ``tool_call`` du flux — l'exécuteur peut apparier (HITL).
            call = AIToolCall(
                id=_CURRENT_TOOL_CALL_ID.get(), name=_spec.name, arguments=arguments
            )
            try:
                result = await tool_executor(call)
            except GraphBubbleUp:
                # agent_interrupt (HITL) : signal de contrôle LangGraph, jamais
                # une erreur — il doit remonter jusqu'au graphe.
                raise
            except Exception as exc:  # contrat violé : filet sans fuite du message
                logger.error(
                    "Exécuteur de tool IA en exception (%s) : %s",
                    _spec.name,
                    type(exc).__name__,
                )
                result = AIToolResult(content=_TOOL_FAILURE_FALLBACK, is_error=True)
            if result.is_error:
                raise ToolException(result.content)
            return result.content, result.image

        lc_tools.append(
            StructuredTool.from_function(
                coroutine=_run,
                name=spec.name,
                description=spec.description,
                args_schema=spec.parameters,
                response_format="content_and_artifact",
                handle_tool_error=True,
            )
        )

    middleware = [
        ModelCallLimitMiddleware(run_limit=max_tool_rounds + 1, exit_behavior="end"),
        _tool_image_middleware()(),
    ]
    return create_agent(model, lc_tools, middleware=middleware, checkpointer=checkpointer)


def _hoist_tool_images(messages: Sequence["BaseMessage"]) -> list["BaseMessage"]:
    """Vue modèle d'un historique : les messages image d'outils (marqués)
    passent APRÈS le run de ``ToolMessage`` qui les entoure.

    L'état LangGraph les range juste derrière LEUR résultat d'outil
    (``[tool_1, image_1, tool_2, image_2]`` quand le modèle lit deux images
    en parallèle) ; or les providers exigent des résultats de round contigus
    (OpenAI : tout message ``tool`` suit directement l'assistant ; Anthropic :
    ``tool_result`` en tête du tour utilisateur fusionné). Le modèle voit donc
    ``[tool_1, tool_2, image_1, image_2]`` — l'état, lui, n'est jamais réécrit.
    """
    from langchain_core.messages import HumanMessage, ToolMessage

    ordered: list[BaseMessage] = []
    pending: list[BaseMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage) and message.additional_kwargs.get(
            _TOOL_IMAGE_MARKER
        ):
            pending.append(message)
            continue
        if pending and not isinstance(message, ToolMessage):
            ordered.extend(pending)
            pending = []
        ordered.append(message)
    ordered.extend(pending)
    return ordered


@lru_cache
def _tool_image_middleware() -> type:
    """Middleware joignant au modèle les images retournées par les tools.

    Classe définie paresseusement (imports langchain différés, motif des
    factories de providers). Une image dans un message ``tool`` n'est acceptée
    que par certains providers ; dans un message **utilisateur**, par tous les
    modèles à vision — d'où le détour :

    - ``wrap_tool_call`` : un ``ToolMessage`` dont l'artefact est une
      :class:`AIToolImage` est complété par un message utilisateur marqué
      (légende + bloc image base64 standard de langchain-core 1.x) via un
      ``Command`` — le ``ToolMessage`` y reste le terminateur exigé par le
      ToolNode ; l'artefact est vidé (le base64 ne vit qu'une fois en état) ;
    - ``wrap_model_call`` : réordonnancement :func:`_hoist_tool_images` de la
      vue envoyée au modèle.

    L'image n'est jamais persistée par l'appelant (contenu texte seul) : elle
    ne vaut que pour le tour courant, le modèle relit la ressource au besoin.
    """
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.messages import HumanMessage, ToolMessage
    from langgraph.types import Command

    def _attach(result: Any, request: Any) -> Any:
        if not isinstance(result, ToolMessage) or not isinstance(result.artifact, AIToolImage):
            return result
        image = result.artifact
        result.artifact = None
        caption = image.caption or _TOOL_IMAGE_DEFAULT_CAPTION.format(
            name=request.tool_call["name"]
        )
        attachment = HumanMessage(
            content=[
                {"type": "text", "text": caption},
                {"type": "image", "base64": image.data, "mime_type": image.mime_type},
            ],
            additional_kwargs={_TOOL_IMAGE_MARKER: True},
        )
        return Command(update={"messages": [result, attachment]})

    class ToolImageMiddleware(AgentMiddleware):
        # Le wrap pose AUSSI l'id d'appel courant (contextvar lue par la
        # coroutine des StructuredTool — cf. _CURRENT_TOOL_CALL_ID) : le
        # handler s'exécute dans la même chaîne d'await, chaque appel
        # concurrent d'un round vivant dans sa propre tâche/contexte.
        async def awrap_tool_call(self, request, handler):  # noqa: ANN001, ANN202
            _CURRENT_TOOL_CALL_ID.set(request.tool_call.get("id") or "")
            return _attach(await handler(request), request)

        def wrap_tool_call(self, request, handler):  # noqa: ANN001, ANN202
            _CURRENT_TOOL_CALL_ID.set(request.tool_call.get("id") or "")
            return _attach(handler(request), request)

        async def awrap_model_call(self, request, handler):  # noqa: ANN001, ANN202
            return await handler(
                request.override(messages=_hoist_tool_images(request.messages))
            )

        def wrap_model_call(self, request, handler):  # noqa: ANN001, ANN202
            return handler(request.override(messages=_hoist_tool_images(request.messages)))

    return ToolImageMiddleware


@lru_cache
def get_ai_client() -> AIClient:
    """Dépendance FastAPI : client partagé (overridable en test, motif get_storage)."""
    return AIClient()
