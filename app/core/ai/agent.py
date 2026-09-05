"""Graphe agent LangGraph autour des tools neutres, et interruption HITL.

- :func:`build_agent` compile le graphe (``create_agent`` de langchain) :
  chaque :class:`AIToolSpec` est enrobé en ``StructuredTool`` async dont la
  coroutine délègue à l'exécuteur neutre — aucun type langchain/langgraph ne
  franchit la frontière du package ;
- :func:`agent_interrupt` fige un run en attendant une reprise (HITL) —
  wrapper neutre de ``langgraph.types.interrupt``, seul point d'appel autorisé
  hors du package ;
- le middleware image (:func:`_tool_image_middleware`) montre au modèle les
  images retournées par les tools.

Imports langchain/langgraph paresseux (dans les fonctions), comme partout
dans le package : rien n'est chargé au boot de l'API.
"""

import contextvars
import logging
from collections.abc import Awaitable, Callable, Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from app.core.ai.types import AIToolCall, AIToolImage, AIToolResult, AIToolSpec

if TYPE_CHECKING:  # uniquement pour les annotations — jamais importé au runtime
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)

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

# Contenu de repli quand l'exécuteur de tools viole son contrat en levant une
# exception : le modèle reçoit un échec générique, jamais le message brut.
_TOOL_FAILURE_FALLBACK = "Échec interne de l'outil"

# Marqueur (``additional_kwargs``) des messages utilisateur porteurs d'une
# image d'outil, posés dans l'état par le middleware ; ils sont déplacés
# après le run de résultats d'outils du round avant chaque appel modèle.
_TOOL_IMAGE_MARKER = "oc_tool_image"
_TOOL_IMAGE_DEFAULT_CAPTION = "Image jointe au résultat de l'outil {name}."


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


def build_agent(
    model: "BaseChatModel",
    tools: Sequence[AIToolSpec],
    tool_executor: Callable[[AIToolCall], Awaitable[AIToolResult]],
    max_tool_rounds: int,
    checkpointer: Any = None,
) -> "CompiledStateGraph":
    """Compile le graphe agent LangGraph autour des tools neutres.

    Un résultat ``is_error=True`` est relayé au modèle en ``ToolMessage`` de
    statut ``error`` (via ``ToolException`` + ``handle_tool_error``), jamais
    en exception du flux. Le plafond de rounds passe par
    ``ModelCallLimitMiddleware`` (sortie propre ``exit_behavior="end"``) :
    ``max_tool_rounds`` rounds d'outils + l'appel de synthèse finale.

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
