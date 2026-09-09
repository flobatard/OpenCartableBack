"""Client IA générique multi-provider — approche « Bring Your Own Token ».

**Seuls les modules de ce package sont autorisés à importer ``langchain*``,
``langgraph`` et ``langfuse``** (même exigence de remplaçabilité que boto3
dans :mod:`app.core.storage` et l'IdP dans :mod:`app.core.auth` : changer de
stack IA ne doit toucher qu'ici). Les features consommatrices (assistant de
cours, tuteur d'exercice, et demain RAG, résumés, quiz…) n'importent que les
noms ré-exportés ci-dessous.

Contrats :

- **BYO token** : la config (:class:`AIRequestConfig` — provider, clé
  ``SecretStr``, modèle, base_url, préférences de raisonnement
  ``reasoning``/``reasoning_effort`` encodées par provider dans
  :mod:`app.core.ai.providers`) voyage à chaque appel ; fallback serveur
  optionnel via les settings ``AI_*`` (résolu dans
  :meth:`AIClient.resolve_config`, préférences de raisonnement du fallback
  comprises : ``AI_REASONING`` / ``AI_REASONING_EFFORT``). Aucune clé n'est
  retenue ni loggée. Les capacités de raisonnement par provider sont
  déclarées par ``PROVIDERS_WITH_REASONING_TOGGLE`` /
  ``PROVIDERS_WITH_REASONING_EFFORT`` (miroirs front), les niveaux natifs par
  ``PROVIDER_REASONING_EFFORTS`` (règle de gating unique
  :func:`check_reasoning_support`) et les options à proposer pour un couple
  (provider, modèle) par :func:`reasoning_options` (catalogue
  :mod:`app.core.ai.reasoning`).
- **Trois modes** : :meth:`AIClient.complete` (réponse complète),
  :meth:`AIClient.stream` (async generator d'événements
  ``token``/``thinking``/``done``, servi en SSE par les routes) et
  :meth:`AIClient.stream_agent` (boucle agent LangGraph avec tools neutres
  :class:`AIToolSpec`, événements enrichis ``tool_call``/``tool_result`` —
  et, pour un run à ``thread_id``, le HITL : :func:`agent_interrupt` dans un
  exécuteur fige le run, événement ``interrupt``, reprise par
  ``stream_agent(..., thread_id=, resume=)``). Validation eager pour les deux
  flux, cf. docstring de :mod:`app.core.ai.client`.
- **Erreurs** (traduites au bord, :mod:`app.core.ai.errors`) : 422 config
  invalide, 400 clé refusée par le provider (jamais 401 — réservé au JWT
  Zitadel), 429 quota provider, 503 provider injoignable ; jamais 500.
- **Langfuse opt-in** (:mod:`app.core.ai.observability`) : no-op total sans
  les settings ``LANGFUSE_*``.
"""

from app.core.ai.agent import agent_interrupt
from app.core.ai.client import AIClient, get_ai_client
from app.core.ai.model_catalog import PROVIDERS_WITH_MODEL_LISTING, list_models
from app.core.ai.observability import shutdown_langfuse
from app.core.ai.reasoning import ReasoningOptions, reasoning_options
from app.core.ai.types import (
    PROVIDER_REASONING_EFFORTS,
    PROVIDERS_WITH_REASONING_EFFORT,
    PROVIDERS_WITH_REASONING_TOGGLE,
    REASONING_EFFORT_MAX_LENGTH,
    AICompletion,
    AIProvider,
    AIRequestConfig,
    AIStreamEvent,
    AIToolCall,
    AIToolImage,
    AIToolResult,
    AIToolSpec,
    AIUsage,
    ChatMessage,
    check_reasoning_support,
)

__all__ = [
    "PROVIDER_REASONING_EFFORTS",
    "PROVIDERS_WITH_MODEL_LISTING",
    "PROVIDERS_WITH_REASONING_EFFORT",
    "PROVIDERS_WITH_REASONING_TOGGLE",
    "REASONING_EFFORT_MAX_LENGTH",
    "AIClient",
    "AICompletion",
    "AIProvider",
    "AIRequestConfig",
    "AIStreamEvent",
    "AIToolCall",
    "AIToolImage",
    "AIToolResult",
    "AIToolSpec",
    "AIUsage",
    "ChatMessage",
    "ReasoningOptions",
    "agent_interrupt",
    "check_reasoning_support",
    "get_ai_client",
    "list_models",
    "reasoning_options",
    "shutdown_langfuse",
]
