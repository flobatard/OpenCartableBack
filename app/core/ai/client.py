"""Le client IA générique : stateless, BYO token, deux modes d'appel.

- :meth:`AIClient.complete` — appel classique (``ainvoke``), réponse complète ;
- :meth:`AIClient.stream` — flux de :class:`AIStreamEvent` (``astream``),
  destiné à être servi en SSE par les routes.

La config (provider, clé, modèle) voyage **à chaque appel** ; à défaut, le
fallback serveur optionnel ``AI_*`` s'applique (:meth:`resolve_config`).

``stream()`` est volontairement **eager** sur la validation : la résolution de
config et la construction du chat model se font AVANT de retourner l'async
generator — une config invalide produit ainsi un vrai status HTTP 4xx, pas une
erreur en plein flux. Seules les erreurs survenant pendant l'itération sont
levées depuis le generator (le routeur les convertit en événement SSE
``error``, le status HTTP 200 étant déjà parti).
"""

from collections.abc import AsyncIterator, Sequence
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
    AIUsage,
    ChatMessage,
)
from app.core.config import settings

if TYPE_CHECKING:  # uniquement pour les annotations — jamais importé au runtime
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import BaseMessage


def _to_langchain_messages(messages: Sequence[ChatMessage]) -> list["BaseMessage"]:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    lc_classes = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
    return [lc_classes[m.role](content=m.content) for m in messages]


def _to_usage(usage_metadata: dict[str, Any] | None) -> AIUsage | None:
    """Normalise l'``usage_metadata`` LangChain (absent chez certains providers)."""
    if not usage_metadata:
        return None
    return AIUsage(
        input_tokens=usage_metadata.get("input_tokens"),
        output_tokens=usage_metadata.get("output_tokens"),
    )


class AIClient:
    """Client IA stateless : aucune connexion ni clé retenue entre deux appels."""

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
                if chunk.text:
                    yield AIStreamEvent(type="token", delta=chunk.text)
        except Exception as exc:
            raise translate_provider_error(exc, cfg.provider.value) from exc
        yield AIStreamEvent(type="done", usage=usage)


@lru_cache
def get_ai_client() -> AIClient:
    """Dépendance FastAPI : client partagé (overridable en test, motif get_storage)."""
    return AIClient()
