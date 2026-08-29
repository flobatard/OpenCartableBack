"""Factories provider → chat model LangChain.

Instanciation **directe** des classes ``Chat*`` (pas ``init_chat_model`` : son
provider ``huggingface`` part en backend ``pipeline`` = chargement transformers
local, inacceptable sur Pi ; et sa table ne connaît ni ``openai_compatible`` ni
la distinction Ollama local/distant). Les imports langchain sont **paresseux,
par factory** : seul le package du provider demandé est importé — un partenaire
manquant ne casse pas les autres, et le démarrage de l'app reste léger.

Toutes les classes acceptent les kwargs standardisés (``model``, ``api_key``,
``base_url``, ``timeout``, ``max_retries``, ``max_tokens``) par alias pydantic
(``populate_by_name`` — vérifié sur les packages installés), sauf exceptions
notées par factory.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.core.ai.errors import invalid_config
from app.core.ai.types import AIProvider, AIRequestConfig
from app.core.config import settings

if TYPE_CHECKING:  # uniquement pour les annotations — jamais importé au runtime
    from langchain_core.language_models.chat_models import BaseChatModel


def _require_api_key(cfg: AIRequestConfig) -> str:
    if cfg.api_key is None or not cfg.api_key.get_secret_value():
        raise invalid_config(f"Clé API requise pour le provider « {cfg.provider.value} »")
    return cfg.api_key.get_secret_value()


def _common_kwargs(cfg: AIRequestConfig) -> dict[str, Any]:
    """Kwargs partagés — les optionnels ne sont passés que s'ils sont fournis
    (None écraserait le défaut du SDK chez certains providers)."""
    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "timeout": settings.AI_TIMEOUT_SECONDS,
        "max_retries": settings.AI_MAX_RETRIES,
    }
    if cfg.temperature is not None:
        kwargs["temperature"] = cfg.temperature
    if cfg.max_tokens is not None:
        kwargs["max_tokens"] = cfg.max_tokens
    return kwargs


def _build_anthropic(cfg: AIRequestConfig) -> "BaseChatModel":
    from langchain_anthropic import ChatAnthropic

    kwargs = _common_kwargs(cfg)
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return ChatAnthropic(api_key=_require_api_key(cfg), **kwargs)


def _build_openai(cfg: AIRequestConfig) -> "BaseChatModel":
    from langchain_openai import ChatOpenAI

    kwargs = _common_kwargs(cfg)
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return ChatOpenAI(api_key=_require_api_key(cfg), **kwargs)


def _build_openai_compatible(cfg: AIRequestConfig) -> "BaseChatModel":
    """Tout endpoint parlant le protocole OpenAI (Groq, Together, vLLM, LM
    Studio…). ``base_url`` obligatoire ; clé absente → placeholder (les serveurs
    locaux type vLLM exigent une chaîne non vide mais ne la vérifient pas)."""
    from langchain_openai import ChatOpenAI

    if not cfg.base_url:
        raise invalid_config("base_url requise pour le provider « openai_compatible »")
    api_key = cfg.api_key.get_secret_value() if cfg.api_key else "sk-no-key"
    return ChatOpenAI(api_key=api_key, base_url=cfg.base_url, **_common_kwargs(cfg))


def _build_google(cfg: AIRequestConfig) -> "BaseChatModel":
    # Gemini = endpoint fixe : pas de base_url custom ici.
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(api_key=_require_api_key(cfg), **_common_kwargs(cfg))


def _build_mistral(cfg: AIRequestConfig) -> "BaseChatModel":
    from langchain_mistralai import ChatMistralAI

    kwargs = _common_kwargs(cfg)
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return ChatMistralAI(api_key=_require_api_key(cfg), **kwargs)


def _build_ollama(cfg: AIRequestConfig) -> "BaseChatModel":
    """Pas de clé ; ``base_url`` optionnelle (défaut SDK : localhost:11434 =
    Ollama local, sinon instance distante « Ollama custom »). Le plafond de
    tokens s'appelle ``num_predict`` et il n'y a ni timeout ni retries."""
    from langchain_ollama import ChatOllama

    kwargs: dict[str, Any] = {"model": cfg.model}
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    if cfg.temperature is not None:
        kwargs["temperature"] = cfg.temperature
    if cfg.max_tokens is not None:
        kwargs["num_predict"] = cfg.max_tokens
    return ChatOllama(**kwargs)


def _build_huggingface(cfg: AIRequestConfig) -> "BaseChatModel":
    """Toujours via :class:`HuggingFaceEndpoint` (API distante). **Jamais**
    ``ChatHuggingFace.from_model_id`` ni le backend ``pipeline`` : ils chargent
    le modèle transformers en local — mortel sur Pi."""
    from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint

    kwargs: dict[str, Any] = {
        "repo_id": cfg.model,
        "huggingfacehub_api_token": _require_api_key(cfg),
        "timeout": settings.AI_TIMEOUT_SECONDS,
    }
    if cfg.temperature is not None:
        kwargs["temperature"] = cfg.temperature
    if cfg.max_tokens is not None:
        kwargs["max_new_tokens"] = cfg.max_tokens
    return ChatHuggingFace(llm=HuggingFaceEndpoint(**kwargs))


_FACTORIES: dict[AIProvider, Callable[[AIRequestConfig], "BaseChatModel"]] = {
    AIProvider.ANTHROPIC: _build_anthropic,
    AIProvider.OPENAI: _build_openai,
    AIProvider.OPENAI_COMPATIBLE: _build_openai_compatible,
    AIProvider.GOOGLE: _build_google,
    AIProvider.MISTRAL: _build_mistral,
    AIProvider.OLLAMA: _build_ollama,
    AIProvider.HUGGINGFACE: _build_huggingface,
}


def build_chat_model(cfg: AIRequestConfig) -> "BaseChatModel":
    """Construit le chat model du provider demandé (validation locale → 422)."""
    factory = _FACTORIES.get(cfg.provider)
    if factory is None:  # AIProvider est fermé, mais restons défensifs
        raise invalid_config(f"Provider IA inconnu : {cfg.provider}")
    return factory(cfg)
