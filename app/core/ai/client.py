"""Le client IA générique : stateless, BYO token, trois modes d'appel.

- :meth:`AIClient.complete` — appel classique (``ainvoke``), réponse complète ;
- :meth:`AIClient.stream` — flux de :class:`AIStreamEvent` (``astream``),
  destiné à être servi en SSE par les routes ;
- :meth:`AIClient.stream_agent` — boucle agent avec tools (graphe de
  :mod:`app.core.ai.agent`), même flux d'événements neutres, enrichi de
  ``tool_call``/``tool_result``/``interrupt``.

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

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr, ValidationError

from app.core.ai.agent import build_agent
from app.core.ai.errors import invalid_config, translate_provider_error
from app.core.ai.messages import delta_events, to_langchain_messages, to_usage
from app.core.ai.observability import build_run_config
from app.core.ai.providers import build_chat_model
from app.core.ai.types import (
    AICompletion,
    AIRequestConfig,
    AIStreamEvent,
    AIToolCall,
    AIToolResult,
    AIToolSpec,
    AIUsage,
    ChatMessage,
)
from app.core.config import settings

if TYPE_CHECKING:  # uniquement pour les annotations — jamais importé au runtime
    from langchain_core.language_models.chat_models import BaseChatModel
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

# Nœuds LangGraph dont les messages sont relayés au flux ; tout autre nœud
# (middlewares) est filtré — son contenu (ex. l'avis anglais du plafond
# d'appels) ne doit jamais fuiter en token.
_MODEL_NODE = "model"
_TOOLS_NODE = "tools"

# Injecté en token quand le plafond de rounds coupe la boucle : l'appelant
# persiste/affiche ainsi une fin de réponse explicite plutôt qu'un silence.
_TOOL_ROUNDS_EXCEEDED_NOTICE = (
    "\n\n*Limite d'appels au modèle atteinte pour cette réponse — synthèse interrompue.*"
)


class AIClient:
    """Client IA stateless sur les appels — la seule mémoire est le
    **checkpointer agent** (InMemorySaver, process-local) qui porte les runs
    figés par :func:`~app.core.ai.agent.agent_interrupt` entre deux requêtes
    (HITL). Un saver Postgres ne s'imposera qu'en multi-worker (TODO.md)."""

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
        « BYO token » rencontre le fallback : les features n'ont pas à
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
            result = await model.ainvoke(to_langchain_messages(messages), config=run_config)
        except Exception as exc:
            raise translate_provider_error(exc, cfg.provider.value) from exc
        return AICompletion(
            content=result.text,
            provider=cfg.provider.value,
            model=cfg.model,
            usage=to_usage(result.usage_metadata),
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
            async for chunk in model.astream(to_langchain_messages(messages), config=run_config):
                # L'usage arrive (chez certains providers) sur un dernier chunk
                # au delta vide : on l'agrège sans émettre de token vide.
                if chunk.usage_metadata:
                    usage = to_usage(chunk.usage_metadata)
                for event in delta_events(chunk):
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
        :func:`~app.core.ai.agent.agent_interrupt` pour figer le run : le flux
        émet un événement ``interrupt`` (portant le payload) et se termine SANS
        ``done``. La reprise est un nouvel appel avec le même ``thread_id`` et
        ``resume=<valeur>`` (les ``messages`` sont alors ignorés — l'état vit
        au checkpoint) : le nœud interrompu se ré-exécute, ``agent_interrupt``
        retourne la valeur, le flux continue (``tool_result``… ``done``).
        Le graphe de la reprise doit être bâti avec les MÊMES tools.

        ``max_tool_rounds`` borne le nombre d'appels au modèle (rounds de tools
        + réponse finale) ; à la coupure, un token d'avertissement clôt le texte
        (:data:`_TOOL_ROUNDS_EXCEEDED_NOTICE`) — jamais de boucle infinie.
        L'usage de ``done`` cumule tous les rounds. Un résultat porteur d'une
        :class:`AIToolImage` est montré au modèle (message utilisateur joint,
        cf. :mod:`app.core.ai.agent`) ; seul son ``content`` texte est relayé
        en ``tool_result``.
        """
        cfg = self.resolve_config(config)
        model = build_chat_model(cfg)
        checkpointer = self._get_checkpointer() if thread_id is not None else None
        agent = build_agent(model, tools, tool_executor, max_tool_rounds, checkpointer)
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
        agent_input: Any = {"messages": to_langchain_messages(messages)}
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
                        for event in delta_events(chunk):
                            yield event
                    elif node == _TOOLS_NODE:
                        if getattr(chunk, "type", None) != "tool":
                            # Message utilisateur image joint par le middleware
                            # (cf. app.core.ai.agent) : jamais relayé.
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


@lru_cache
def get_ai_client() -> AIClient:
    """Dépendance FastAPI : client partagé (overridable en test, motif get_storage)."""
    return AIClient()
