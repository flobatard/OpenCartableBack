"""Catalogue des modèles disponibles chez un provider — REST direct, sans SDK.

Alimente l'auto-complétion du champ « modèle » de l'écran de réglages IA du
front (via ``app/ai_credentials/``). LangChain n'expose pas de « list models »
uniforme : on interroge directement l'API de listing de chaque provider en
httpx (le confinement de ``app/core/ai/`` porte sur la stack IA — la logique
par provider reste ici, remplaçable d'un bloc). La clé API voyage toujours en
**header**, jamais dans l'URL ni les query params (elle finirait sinon dans
les logs d'accès du provider ou d'un proxy) — y compris chez Google, où
``x-goog-api-key`` remplace le ``?key=`` documenté.

Providers sans listing : ``huggingface`` (le Hub entier n'est pas un
catalogue exploitable) → 422 explicite, et le front ne propose pas le bouton
(constante miroir :data:`PROVIDERS_WITH_MODEL_LISTING`).

Erreurs : mêmes règles que le reste du package — validation locale → 422
(:func:`invalid_config`), erreur provider/réseau/réponse inexploitable →
:func:`translate_provider_error` (400 clé refusée, 429, 503 injoignable…),
jamais 500, jamais la clé dans un ``detail``.
"""

from collections.abc import Callable
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.ai.errors import invalid_config, translate_provider_error
from app.core.ai.types import AIProvider

# Un listing est un petit GET : timeout court dédié, indépendant du
# AI_TIMEOUT_SECONDS (60 s) taillé pour les générations.
_LIST_TIMEOUT_SECONDS = 10.0

_OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"
_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"
_MISTRAL_MODELS_URL = "https://api.mistral.ai/v1/models"
_GOOGLE_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

#: Providers dont les modèles sont listables — miroir front
#: ``PROVIDERS_WITH_MODEL_LISTING`` (ai-credentials.model.ts).
PROVIDERS_WITH_MODEL_LISTING = frozenset(
    {
        AIProvider.ANTHROPIC,
        AIProvider.OPENAI,
        AIProvider.GOOGLE,
        AIProvider.MISTRAL,
        AIProvider.OLLAMA,
        AIProvider.OPENAI_COMPATIBLE,
    }
)

# (url, headers, params) d'une requête de listing.
_ListingRequest = tuple[str, dict[str, str], dict[str, Any]]


def _require_key(provider: AIProvider, api_key: SecretStr | None) -> str:
    if api_key is None or not api_key.get_secret_value():
        raise invalid_config(f"Clé API requise pour le provider « {provider.value} »")
    return api_key.get_secret_value()


def _anthropic_request(api_key: SecretStr | None, base_url: str | None) -> _ListingRequest:
    root = (base_url or "").rstrip("/")
    url = f"{root}/v1/models" if root else _ANTHROPIC_MODELS_URL
    headers = {
        "x-api-key": _require_key(AIProvider.ANTHROPIC, api_key),
        "anthropic-version": _ANTHROPIC_VERSION,
    }
    # Paginé (défaut 20) : le plafond documenté suffit largement au catalogue.
    return url, headers, {"limit": 1000}


def _openai_request(api_key: SecretStr | None, base_url: str | None) -> _ListingRequest:
    root = (base_url or _OPENAI_DEFAULT_BASE_URL).rstrip("/")
    key = _require_key(AIProvider.OPENAI, api_key)
    return f"{root}/models", {"Authorization": f"Bearer {key}"}, {}


def _openai_compatible_request(api_key: SecretStr | None, base_url: str | None) -> _ListingRequest:
    if not base_url:
        raise invalid_config("base_url requise pour le provider « openai_compatible »")
    # Clé facultative (serveurs locaux type vLLM/LM Studio) : contrairement au
    # SDK, un GET brut n'exige rien — pas de header plutôt qu'un placeholder.
    headers = (
        {"Authorization": f"Bearer {api_key.get_secret_value()}"}
        if api_key and api_key.get_secret_value()
        else {}
    )
    return f"{base_url.rstrip('/')}/models", headers, {}


def _google_request(api_key: SecretStr | None, base_url: str | None) -> _ListingRequest:
    key = _require_key(AIProvider.GOOGLE, api_key)
    return _GOOGLE_MODELS_URL, {"x-goog-api-key": key}, {"pageSize": 1000}


def _mistral_request(api_key: SecretStr | None, base_url: str | None) -> _ListingRequest:
    key = _require_key(AIProvider.MISTRAL, api_key)
    return _MISTRAL_MODELS_URL, {"Authorization": f"Bearer {key}"}, {}


def _ollama_request(api_key: SecretStr | None, base_url: str | None) -> _ListingRequest:
    root = (base_url or _OLLAMA_DEFAULT_BASE_URL).rstrip("/")
    return f"{root}/api/tags", {}, {}


def _parse_openai_style(data: Any) -> list[str]:
    """``{"data": [{"id": …}]}`` — OpenAI, compatibles, Anthropic et Mistral."""
    items = data.get("data") if isinstance(data, dict) else None
    return [
        item["id"]
        for item in items or []
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    ]


def _parse_google(data: Any) -> list[str]:
    """``{"models": [{"name": "models/…", "supportedGenerationMethods": […]}]}``.

    Seuls les modèles de génération de texte nous concernent (le catalogue
    mêle embeddings et modèles d'image) ; le préfixe ``models/`` est retiré —
    c'est la forme attendue par ``ChatGoogleGenerativeAI``.
    """
    items = data.get("models") if isinstance(data, dict) else None
    names: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if "generateContent" not in (item.get("supportedGenerationMethods") or []):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            names.append(name.removeprefix("models/"))
    return names


def _parse_ollama(data: Any) -> list[str]:
    """``{"models": [{"name": "llama3.2:latest"}]}`` — le ``GET /api/tags``."""
    items = data.get("models") if isinstance(data, dict) else None
    return [
        item["name"]
        for item in items or []
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"]
    ]


_REQUEST_BUILDERS: dict[
    AIProvider, Callable[[SecretStr | None, str | None], _ListingRequest]
] = {
    AIProvider.ANTHROPIC: _anthropic_request,
    AIProvider.OPENAI: _openai_request,
    AIProvider.OPENAI_COMPATIBLE: _openai_compatible_request,
    AIProvider.GOOGLE: _google_request,
    AIProvider.MISTRAL: _mistral_request,
    AIProvider.OLLAMA: _ollama_request,
}

_PARSERS: dict[AIProvider, Callable[[Any], list[str]]] = {
    AIProvider.ANTHROPIC: _parse_openai_style,
    AIProvider.OPENAI: _parse_openai_style,
    AIProvider.OPENAI_COMPATIBLE: _parse_openai_style,
    AIProvider.GOOGLE: _parse_google,
    AIProvider.MISTRAL: _parse_openai_style,
    AIProvider.OLLAMA: _parse_ollama,
}


async def _get_json(url: str, *, headers: dict[str, str], params: dict[str, Any]) -> Any:
    """Le GET réseau, isolé pour être monkeypatchable en test (motif
    ``build_chat_model``) — les erreurs sont traduites par l'appelant."""
    async with httpx.AsyncClient(timeout=_LIST_TIMEOUT_SECONDS) as client:
        response = await client.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()


async def list_models(
    provider: AIProvider, api_key: SecretStr | None, base_url: str | None
) -> list[str]:
    """Identifiants des modèles proposés par le provider, dans SON ordre.

    L'ordre du provider est conservé (Anthropic sert du plus récent au plus
    ancien — plus utile qu'un tri alphabétique pour une suggestion), les
    doublons retirés. Validation locale (provider non listable, clé ou
    base_url manquante) → 422 AVANT tout réseau.
    """
    builder = _REQUEST_BUILDERS.get(provider)
    if builder is None:
        raise invalid_config(
            f"Listing des modèles non disponible pour le provider « {provider.value} »"
        )
    url, headers, params = builder(api_key, base_url)
    try:
        data = await _get_json(url, headers=headers, params=params)
        models = _PARSERS[provider](data)
    except Exception as exc:
        raise translate_provider_error(exc, provider.value) from exc
    return list(dict.fromkeys(models))
