"""Catalogue des modèles par provider (app/core/ai/model_catalog) — sans réseau.

Motif ``build_chat_model`` de test_ai_client.py : le GET réseau ``_get_json``
est monkeypatché par un fake qui enregistre (url, headers, params) et sert un
payload canné. Règles vérifiées transversalement : la clé voyage en header,
jamais dans l'URL ni les params ; les erreurs sont traduites (jamais 500) et
ne relaient jamais la clé.
"""

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.core.ai import model_catalog
from app.core.ai.model_catalog import list_models
from app.core.ai.types import AIProvider

API_KEY = "sk-cle-de-test-secrete"


class _FakeGet:
    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, dict, dict]] = []

    async def __call__(self, url, *, headers, params):
        self.calls.append((url, headers, params))
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture
def fake_get(monkeypatch: pytest.MonkeyPatch):
    """Installe un _FakeGet reconfigurable à la place du GET réseau."""

    def install(payload=None, error: Exception | None = None) -> _FakeGet:
        fake = _FakeGet(payload, error)
        monkeypatch.setattr(model_catalog, "_get_json", fake)
        return fake

    return install


def _key_never_in_url(fake: _FakeGet) -> None:
    [(url, _, params)] = fake.calls
    assert API_KEY not in url
    assert all(API_KEY not in str(v) for v in params.values())


# ---------------------------------------------------------------- nominal


@pytest.mark.anyio
async def test_openai_listing(fake_get) -> None:
    fake = fake_get({"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]})
    models = await list_models(AIProvider.OPENAI, SecretStr(API_KEY), None)
    assert models == ["gpt-4o", "gpt-4o-mini"]  # dédoublonné, ordre conservé
    [(url, headers, _)] = fake.calls
    assert url == "https://api.openai.com/v1/models"
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    _key_never_in_url(fake)


@pytest.mark.anyio
async def test_anthropic_listing(fake_get) -> None:
    fake = fake_get({"data": [{"id": "claude-sonnet-5"}, {"id": "claude-opus-5"}]})
    models = await list_models(AIProvider.ANTHROPIC, SecretStr(API_KEY), None)
    assert models == ["claude-sonnet-5", "claude-opus-5"]
    [(url, headers, params)] = fake.calls
    assert url == "https://api.anthropic.com/v1/models"
    assert headers["x-api-key"] == API_KEY
    assert headers["anthropic-version"] == "2023-06-01"
    assert params == {"limit": 1000}
    _key_never_in_url(fake)


@pytest.mark.anyio
async def test_mistral_listing(fake_get) -> None:
    fake = fake_get({"data": [{"id": "mistral-large-latest"}]})
    assert await list_models(AIProvider.MISTRAL, SecretStr(API_KEY), None) == [
        "mistral-large-latest"
    ]
    [(url, headers, _)] = fake.calls
    assert url == "https://api.mistral.ai/v1/models"
    assert headers["Authorization"] == f"Bearer {API_KEY}"


@pytest.mark.anyio
async def test_google_listing_filters_and_strips_prefix(fake_get) -> None:
    """Seuls les modèles generateContent, préfixe models/ retiré, clé en header."""
    generate = {"supportedGenerationMethods": ["generateContent"]}
    fake = fake_get(
        {
            "models": [
                {"name": "models/gemini-2.5-pro", **generate},
                {"name": "models/embedding-001", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemini-2.5-flash", **generate},
            ]
        }
    )
    models = await list_models(AIProvider.GOOGLE, SecretStr(API_KEY), None)
    assert models == ["gemini-2.5-pro", "gemini-2.5-flash"]
    [(_, headers, params)] = fake.calls
    assert headers["x-goog-api-key"] == API_KEY
    assert "key" not in params  # jamais le ?key= documenté : header uniquement
    _key_never_in_url(fake)


@pytest.mark.anyio
async def test_ollama_listing_without_key(fake_get) -> None:
    fake = fake_get({"models": [{"name": "llama3.2:latest"}, {"name": "qwen3:8b"}]})
    models = await list_models(AIProvider.OLLAMA, None, "http://pi:11434/")
    assert models == ["llama3.2:latest", "qwen3:8b"]
    [(url, headers, _)] = fake.calls
    assert url == "http://pi:11434/api/tags"
    assert headers == {}


@pytest.mark.anyio
async def test_ollama_default_base_url(fake_get) -> None:
    fake = fake_get({"models": []})
    assert await list_models(AIProvider.OLLAMA, None, None) == []
    assert fake.calls[0][0] == "http://localhost:11434/api/tags"


@pytest.mark.anyio
async def test_openai_compatible_key_optional(fake_get) -> None:
    """Sans clé : aucun header Authorization (pas de placeholder en REST brut)."""
    fake = fake_get({"data": [{"id": "llama-3.3-70b"}]})
    models = await list_models(AIProvider.OPENAI_COMPATIBLE, None, "https://api.groq.com/openai/v1")
    assert models == ["llama-3.3-70b"]
    [(url, headers, _)] = fake.calls
    assert url == "https://api.groq.com/openai/v1/models"
    assert "Authorization" not in headers


# ---------------------------------------------------------------- validation locale (422)


@pytest.mark.anyio
async def test_huggingface_not_listable(fake_get) -> None:
    fake = fake_get({})
    with pytest.raises(HTTPException) as exc_info:
        await list_models(AIProvider.HUGGINGFACE, SecretStr(API_KEY), None)
    assert exc_info.value.status_code == 422
    assert fake.calls == []  # aucun réseau


@pytest.mark.anyio
async def test_openai_compatible_requires_base_url(fake_get) -> None:
    fake = fake_get({})
    with pytest.raises(HTTPException) as exc_info:
        await list_models(AIProvider.OPENAI_COMPATIBLE, SecretStr(API_KEY), None)
    assert exc_info.value.status_code == 422
    assert fake.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "provider",
    [AIProvider.ANTHROPIC, AIProvider.OPENAI, AIProvider.GOOGLE, AIProvider.MISTRAL],
)
async def test_cloud_providers_require_key(fake_get, provider: AIProvider) -> None:
    fake = fake_get({})
    with pytest.raises(HTTPException) as exc_info:
        await list_models(provider, None, None)
    assert exc_info.value.status_code == 422
    assert fake.calls == []


# ---------------------------------------------------------------- erreurs provider traduites


def _status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://provider.example/models")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status_code, request=request)
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (_status_error(401), 400),  # clé refusée → jamais 401 (réservé au JWT)
        (_status_error(429), 429),
        (_status_error(404), 422),
        (_status_error(500), 503),
        (httpx.ConnectError("down"), 503),
    ],
)
async def test_provider_errors_translated(fake_get, error, expected_status: int) -> None:
    fake_get(error=error)
    with pytest.raises(HTTPException) as exc_info:
        await list_models(AIProvider.OPENAI, SecretStr(API_KEY), None)
    assert exc_info.value.status_code == expected_status
    assert API_KEY not in str(exc_info.value.detail)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "provider", [AIProvider.OPENAI, AIProvider.GOOGLE, AIProvider.OLLAMA]
)
async def test_malformed_payload_yields_empty_list(fake_get, provider: AIProvider) -> None:
    """Parseurs défensifs : un payload inattendu → aucune suggestion, jamais un 500."""
    fake_get(["pas", "la", "forme", "attendue"])
    assert await list_models(provider, SecretStr(API_KEY), None) == []
