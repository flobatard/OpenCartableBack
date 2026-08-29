"""Tests unitaires du module confiné app/core/ai/ — aucun réseau.

Le chat model est remplacé par le fake officiel de langchain_core
(``GenericFakeChatModel`` : ``ainvoke`` et ``astream`` supportés, le stream
découpe le contenu sur les espaces, pas d'``usage_metadata`` → ``usage`` à
``None``) via monkeypatch de ``app.core.ai.client.build_chat_model``.
"""

import sys
import types

import httpx
import pytest
from fastapi import HTTPException
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.core import ai as ai_module
from app.core.ai import AIClient, AIProvider, AIRequestConfig, ChatMessage, observability
from app.core.ai import client as client_module
from app.core.ai.errors import translate_provider_error
from app.core.ai.providers import build_chat_model
from app.core.config import settings

MESSAGES = [ChatMessage(role="user", content="Bonjour")]
CONFIG = AIRequestConfig(provider=AIProvider.OLLAMA, model="llama3.2")


def _fake_model(content: str = "Bonjour le monde") -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter([AIMessage(content=content)]))


@pytest.fixture
def fake_build(monkeypatch: pytest.MonkeyPatch):
    """Substitue build_chat_model ; retourne un holder pour changer le modèle."""
    holder = {"model": _fake_model()}
    monkeypatch.setattr(client_module, "build_chat_model", lambda cfg: holder["model"])
    return holder


# ---------------------------------------------------------------- complete / stream


@pytest.mark.anyio
async def test_complete_nominal(fake_build) -> None:
    result = await AIClient().complete(MESSAGES, CONFIG)
    assert result.content == "Bonjour le monde"
    assert result.provider == "ollama"
    assert result.model == "llama3.2"
    assert result.usage is None  # GenericFakeChatModel ne fournit pas d'usage


@pytest.mark.anyio
async def test_stream_nominal(fake_build) -> None:
    events = [e async for e in AIClient().stream(MESSAGES, CONFIG)]
    tokens = [e for e in events if e.type == "token"]
    assert "".join(t.delta for t in tokens) == "Bonjour le monde"
    assert events[-1].type == "done"
    assert events[-1].usage is None


# ---------------------------------------------------------------- fallback serveur


@pytest.mark.anyio
async def test_fallback_serveur(fake_build, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "AI_MODEL", "llama3.2")
    result = await AIClient().complete(MESSAGES, config=None)
    assert result.provider == "ollama"
    assert result.model == "llama3.2"


def test_sans_config_ni_fallback() -> None:
    with pytest.raises(HTTPException) as exc:
        AIClient().resolve_config(None)
    assert exc.value.status_code == 422


def test_fallback_provider_inconnu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "skynet")
    monkeypatch.setattr(settings, "AI_MODEL", "t-1000")
    with pytest.raises(HTTPException) as exc:
        AIClient().resolve_config(None)
    assert exc.value.status_code == 422


# ---------------------------------------------------------------- validation locale


def test_openai_compatible_sans_base_url() -> None:
    cfg = AIRequestConfig(provider=AIProvider.OPENAI_COMPATIBLE, model="x")
    with pytest.raises(HTTPException) as exc:
        build_chat_model(cfg)
    assert exc.value.status_code == 422
    assert "base_url" in exc.value.detail


def test_anthropic_sans_cle() -> None:
    cfg = AIRequestConfig(provider=AIProvider.ANTHROPIC, model="claude-opus-5")
    with pytest.raises(HTTPException) as exc:
        build_chat_model(cfg)
    assert exc.value.status_code == 422


# ---------------------------------------------------------------- mapping d'erreurs


class _ProviderError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("boom sk-super-secret boom")
        self.status_code = status_code


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_ProviderError(401), 400),
        (_ProviderError(403), 400),
        (_ProviderError(404), 422),
        (_ProviderError(400), 422),
        (_ProviderError(429), 429),
        (_ProviderError(500), 503),
        (httpx.ConnectError("down"), 503),
        (httpx.ReadTimeout("slow"), 503),
        (TimeoutError(), 503),
        (RuntimeError("mystère"), 503),
    ],
)
def test_translate_provider_error(exc: Exception, expected: int) -> None:
    http_exc = translate_provider_error(exc, "openai")
    assert http_exc.status_code == expected
    # Le detail ne relaie jamais le message brut du SDK (fragments de clé/prompt).
    assert "sk-super-secret" not in str(http_exc.detail)


class _ExplodingModel:
    """Faux chat model dont ainvoke/astream lèvent une exception provider."""

    def __init__(self, exc: Exception, tokens_avant: int = 0) -> None:
        self._exc = exc
        self._tokens_avant = tokens_avant

    async def ainvoke(self, messages, config=None):
        raise self._exc

    async def astream(self, messages, config=None):
        from langchain_core.messages import AIMessageChunk

        for _ in range(self._tokens_avant):
            yield AIMessageChunk(content="tok")
        raise self._exc


@pytest.mark.anyio
async def test_complete_erreur_provider(fake_build) -> None:
    fake_build["model"] = _ExplodingModel(_ProviderError(401))
    with pytest.raises(HTTPException) as exc:
        await AIClient().complete(MESSAGES, CONFIG)
    assert exc.value.status_code == 400


@pytest.mark.anyio
async def test_stream_erreur_en_cours(fake_build) -> None:
    """L'erreur mid-stream sort du generator APRÈS les premiers tokens."""
    fake_build["model"] = _ExplodingModel(httpx.ConnectError("down"), tokens_avant=2)
    events = []
    with pytest.raises(HTTPException) as exc:
        async for event in AIClient().stream(MESSAGES, CONFIG):
            events.append(event)
    assert [e.type for e in events] == ["token", "token"]
    assert exc.value.status_code == 503


# ---------------------------------------------------------------- Langfuse opt-in


def test_langfuse_desactive_par_defaut() -> None:
    assert observability.build_callbacks() == []
    config = observability.build_run_config(trace_name="t", user_id="prof-123")
    assert config["callbacks"] == []
    assert config["run_name"] == "t"
    assert config["metadata"] == {"langfuse_user_id": "prof-123"}


def test_langfuse_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setattr(settings, "LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(settings, "LANGFUSE_HOST", "https://langfuse.test")

    created: dict = {}

    class _FakeLangfuse:
        def __init__(self, **kwargs) -> None:
            created.update(kwargs)

        def shutdown(self) -> None:
            created["shutdown"] = True

    class _FakeHandler:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

    fake_langfuse = types.ModuleType("langfuse")
    fake_langfuse.Langfuse = _FakeLangfuse
    fake_langchain = types.ModuleType("langfuse.langchain")
    fake_langchain.CallbackHandler = _FakeHandler
    monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse)
    monkeypatch.setitem(sys.modules, "langfuse.langchain", fake_langchain)

    observability._get_langfuse_client.cache_clear()
    try:
        callbacks = observability.build_callbacks()
        assert len(callbacks) == 1
        assert isinstance(callbacks[0], _FakeHandler)
        # Credentials explicites depuis les settings, pas depuis l'env process.
        assert created == {
            "public_key": "pk-test",
            "secret_key": "sk-test",
            "host": "https://langfuse.test",
        }
        ai_module.shutdown_langfuse()
        assert created["shutdown"] is True
    finally:
        observability._get_langfuse_client.cache_clear()
