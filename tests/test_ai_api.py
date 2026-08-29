"""Tests des routes de smoke-test IA (app/ai/) — aucun réseau.

Motif ``_FakeStorage`` : un ``_FakeAIClient`` duck-typé est injecté via
``app.dependency_overrides[get_ai_client]`` (+ ``get_current_user``).
Le SSE est lu via ``client.stream(...)`` du TestClient.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.ai import AICompletion, AIStreamEvent, AIUsage, get_ai_client
from app.core.auth import AuthenticatedUser, get_current_user
from app.main import create_app

CHAT_PAYLOAD = {
    "messages": [{"role": "user", "content": "Bonjour"}],
    "config": {"provider": "ollama", "model": "llama3.2"},
}


class _FakeAIClient:
    """Faux AIClient scriptable ; enregistre les appels pour assertions."""

    def __init__(
        self,
        completion: AICompletion | None = None,
        stream_events: list[AIStreamEvent] | None = None,
        stream_error: HTTPException | None = None,
        mid_stream_error: HTTPException | None = None,
    ) -> None:
        self.completion = completion or AICompletion(
            content="Bonjour !", provider="ollama", model="llama3.2"
        )
        self.stream_events = stream_events
        self.stream_error = stream_error
        self.mid_stream_error = mid_stream_error
        self.calls: list[dict] = []

    async def complete(self, messages, config=None, *, trace_name=None, user_id=None):
        self.calls.append({"messages": messages, "config": config, "user_id": user_id})
        return self.completion

    def stream(self, messages, config=None, *, trace_name=None, user_id=None):
        # Miroir du contrat réel : validation EAGER ici, generator lazy ensuite.
        if self.stream_error is not None:
            raise self.stream_error
        self.calls.append({"messages": messages, "config": config, "user_id": user_id})
        return self._stream()

    async def _stream(self) -> AsyncIterator[AIStreamEvent]:
        events = self.stream_events or [
            AIStreamEvent(type="token", delta="Bonjour"),
            AIStreamEvent(type="token", delta=" !"),
            AIStreamEvent(type="done", usage=AIUsage(input_tokens=3, output_tokens=2)),
        ]
        for event in events:
            if self.mid_stream_error is not None and event.type == "done":
                raise self.mid_stream_error
            yield event


def _client(ai_client: _FakeAIClient | None = None) -> tuple[TestClient, _FakeAIClient]:
    app = create_app()
    fake = ai_client or _FakeAIClient()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="prof-123", email=None, roles=frozenset(), claims={}
    )
    app.dependency_overrides[get_ai_client] = lambda: fake
    return TestClient(app), fake


def _parse_sse(body: str) -> list[tuple[str, str]]:
    """Parse naïf du flux SSE en couples (event, data-json)."""
    events = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], lines["data"]))
    return events


# ---------------------------------------------------------------- auth


@pytest.mark.parametrize("path", ["/api/v1/ai/chat", "/api/v1/ai/chat/stream"])
def test_routes_requierent_un_token(client: TestClient, path: str) -> None:
    response = client.post(path, json=CHAT_PAYLOAD)
    assert response.status_code == 401


# ---------------------------------------------------------------- POST /ai/chat


def test_chat_nominal() -> None:
    client, fake = _client()
    response = client.post("/api/v1/ai/chat", json=CHAT_PAYLOAD)
    assert response.status_code == 200
    assert response.json() == {
        "content": "Bonjour !",
        "provider": "ollama",
        "model": "llama3.2",
        "usage": None,
    }
    # Le sub (jamais l'e-mail) est relayé pour la trace Langfuse.
    assert fake.calls[0]["user_id"] == "prof-123"
    assert fake.calls[0]["config"].provider.value == "ollama"


def test_chat_sans_config_utilise_le_fallback() -> None:
    client, fake = _client()
    response = client.post(
        "/api/v1/ai/chat", json={"messages": [{"role": "user", "content": "Salut"}]}
    )
    assert response.status_code == 200
    assert fake.calls[0]["config"] is None  # la résolution du fallback vit dans AIClient


@pytest.mark.parametrize(
    "payload",
    [
        {"messages": []},  # au moins un message
        {"messages": [{"role": "user", "content": ""}]},  # contenu vide
        {"messages": [{"role": "user", "content": "x"}], "inconnu": True},  # extra=forbid
        # provider hors AIProvider
        {"messages": [{"role": "user", "content": "x"}], "config": {"provider": "skynet", "model": "m"}},  # noqa: E501
    ],
)
def test_chat_payload_invalide(payload: dict) -> None:
    client, _ = _client()
    assert client.post("/api/v1/ai/chat", json=payload).status_code == 422


# ---------------------------------------------------------------- POST /ai/chat/stream


def test_stream_nominal() -> None:
    client, _ = _client()
    with client.stream("POST", "/api/v1/ai/chat/stream", json=CHAT_PAYLOAD) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        body = response.read().decode("utf-8")
    events = _parse_sse(body)
    assert events == [
        ("token", '{"delta": "Bonjour"}'),
        ("token", '{"delta": " !"}'),
        ("done", '{"usage": {"input_tokens": 3, "output_tokens": 2}}'),
    ]


def test_stream_erreur_avant_le_flux() -> None:
    """Erreur eager (config invalide) → vrai status HTTP, pas un event SSE."""
    fake = _FakeAIClient(stream_error=HTTPException(422, detail="Clé API requise"))
    client, _ = _client(fake)
    response = client.post("/api/v1/ai/chat/stream", json=CHAT_PAYLOAD)
    assert response.status_code == 422
    assert response.json()["detail"] == "Clé API requise"


def test_stream_erreur_en_cours_de_flux() -> None:
    """Erreur mid-stream → HTTP 200 (déjà parti) + événement SSE ``error``."""
    fake = _FakeAIClient(
        mid_stream_error=HTTPException(503, detail="Fournisseur IA injoignable")
    )
    client, _ = _client(fake)
    with client.stream("POST", "/api/v1/ai/chat/stream", json=CHAT_PAYLOAD) as response:
        assert response.status_code == 200
        body = response.read().decode("utf-8")
    events = _parse_sse(body)
    assert events[:2] == [("token", '{"delta": "Bonjour"}'), ("token", '{"delta": " !"}')]
    assert events[-1] == ("error", '{"status": 503, "detail": "Fournisseur IA injoignable"}')
