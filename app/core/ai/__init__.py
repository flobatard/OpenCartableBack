"""Client IA générique multi-provider — approche « Bring Your Own Token ».

**Seuls les modules de ce package sont autorisés à importer ``langchain*`` et
``langfuse``** (même exigence de remplaçabilité que boto3 dans
:mod:`app.core.storage` et l'IdP dans :mod:`app.core.auth` : changer de stack
IA ne doit toucher qu'ici). Les features consommatrices (J5 : RAG, résumés,
quiz, review de copies) n'importent que les noms ré-exportés ci-dessous.

Contrats :

- **BYO token** : la config (:class:`AIRequestConfig` — provider, clé
  ``SecretStr``, modèle, base_url) voyage à chaque appel ; fallback serveur
  optionnel via les settings ``AI_*`` (résolu dans
  :meth:`AIClient.resolve_config`). Aucune clé n'est retenue ni loggée.
- **Deux modes** : :meth:`AIClient.complete` (réponse complète) et
  :meth:`AIClient.stream` (async generator d'événements ``token``/``done``,
  servi en SSE par les routes — validation eager, cf. docstring de
  :mod:`app.core.ai.client`).
- **Erreurs** (traduites au bord, :mod:`app.core.ai.errors`) : 422 config
  invalide, 400 clé refusée par le provider (jamais 401 — réservé au JWT
  Zitadel), 429 quota provider, 503 provider injoignable ; jamais 500.
- **Langfuse opt-in** (:mod:`app.core.ai.observability`) : no-op total sans
  les settings ``LANGFUSE_*``.
"""

from app.core.ai.client import AIClient, get_ai_client
from app.core.ai.observability import shutdown_langfuse
from app.core.ai.types import (
    AICompletion,
    AIProvider,
    AIRequestConfig,
    AIStreamEvent,
    AIUsage,
    ChatMessage,
)

__all__ = [
    "AIClient",
    "AICompletion",
    "AIProvider",
    "AIRequestConfig",
    "AIStreamEvent",
    "AIUsage",
    "ChatMessage",
    "get_ai_client",
    "shutdown_langfuse",
]
