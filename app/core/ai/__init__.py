"""Client IA générique multi-provider — approche « Bring Your Own Token ».

**Seuls les modules de ce package sont autorisés à importer ``langchain*``,
``langgraph`` et ``langfuse``** (même exigence de remplaçabilité que boto3
dans :mod:`app.core.storage` et l'IdP dans :mod:`app.core.auth` : changer de
stack IA ne doit toucher qu'ici). Les features consommatrices (J5 : assistant
de cours, RAG, résumés, quiz, review de copies) n'importent que les noms
ré-exportés ci-dessous.

Contrats :

- **BYO token** : la config (:class:`AIRequestConfig` — provider, clé
  ``SecretStr``, modèle, base_url) voyage à chaque appel ; fallback serveur
  optionnel via les settings ``AI_*`` (résolu dans
  :meth:`AIClient.resolve_config`). Aucune clé n'est retenue ni loggée.
- **Trois modes** : :meth:`AIClient.complete` (réponse complète),
  :meth:`AIClient.stream` (async generator d'événements
  ``token``/``thinking``/``done``, servi en SSE par les routes) et
  :meth:`AIClient.stream_agent` (boucle agent LangGraph avec tools neutres
  :class:`AIToolSpec`, événements enrichis ``tool_call``/``tool_result``).
  Validation eager pour les deux flux, cf. docstring de
  :mod:`app.core.ai.client`.
- **Erreurs** (traduites au bord, :mod:`app.core.ai.errors`) : 422 config
  invalide, 400 clé refusée par le provider (jamais 401 — réservé au JWT
  Zitadel), 429 quota provider, 503 provider injoignable ; jamais 500.
- **Langfuse opt-in** (:mod:`app.core.ai.observability`) : no-op total sans
  les settings ``LANGFUSE_*``.
"""

from app.core.ai.client import AIClient, get_ai_client
from app.core.ai.model_catalog import PROVIDERS_WITH_MODEL_LISTING, list_models
from app.core.ai.observability import shutdown_langfuse
from app.core.ai.types import (
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
)

__all__ = [
    "PROVIDERS_WITH_MODEL_LISTING",
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
    "get_ai_client",
    "list_models",
    "shutdown_langfuse",
]
