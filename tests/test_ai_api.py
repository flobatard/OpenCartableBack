"""Tests des routes de smoke-test IA (app/ai/) — aucun réseau ni Postgres.

Motif ``_FakeStorage`` : un ``_FakeAIClient`` duck-typé est injecté via
``app.dependency_overrides[get_ai_client]`` (+ ``get_current_user`` et
``get_db`` — la cascade ``effective_config`` lit le credential utilisateur
quand la requête ne porte pas de config). Le SSE est lu via
``client.stream(...)`` du TestClient.
"""

import base64
import os
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Delete, Insert, Update

from app.core import crypto
from app.core.ai import AICompletion, AIStreamEvent, AIUsage, get_ai_client
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.main import create_app

CHAT_PAYLOAD = {
    "messages": [{"role": "user", "content": "Bonjour"}],
    "config": {"provider": "ollama", "model": "llama3.2"},
}
NO_CONFIG_PAYLOAD = {"messages": [{"role": "user", "content": "Salut"}]}
MASTER_KEY = os.urandom(32)


def _user_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        sub="prof-123",
        email=None,
        ai_provider=None,
        ai_model=None,
        ai_base_url=None,
        ai_api_key_encrypted=None,
        ai_encryption_salt=None,
        ai_daily_call_quota=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class _FakeResult:
    def __init__(self, rows, rowcount=0):
        self._rows = rows
        self.rowcount = rowcount

    def scalars(self):
        return self

    def one(self):
        [row] = self._rows
        return row


class _FakeSession:
    """FIFO des SELECT (motif test_users_api.py), INSERT/DELETE/UPDATE tracés.

    ``upsert_rowcount`` scripte le résultat de l'upsert atomique du quota
    quotidien : 1 = ligne écrite (quota consommé), 0 = garde du WHERE du
    DO UPDATE non satisfaite (quota du jour épuisé). Seul le service quota
    lit le rowcount — le poser sur tous les INSERT/DELETE/UPDATE (dont
    l'update de remboursement) est sans effet ailleurs.
    """

    def __init__(self, select_results=(), upsert_rowcount=1):
        self._select_results = list(select_results)
        self.upsert_rowcount = upsert_rowcount
        self.executed = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        if isinstance(stmt, (Insert, Delete, Update)):
            return _FakeResult([], rowcount=self.upsert_rowcount)
        return _FakeResult(self._select_results.pop(0))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        """Jamais atteint en nominal — filet du remboursement best-effort."""


class _FakeAIClient:
    """Faux AIClient scriptable ; enregistre les appels pour assertions."""

    def __init__(
        self,
        completion: AICompletion | None = None,
        stream_events: list[AIStreamEvent] | None = None,
        stream_error: HTTPException | None = None,
        mid_stream_error: HTTPException | None = None,
        complete_error: HTTPException | None = None,
    ) -> None:
        self.completion = completion or AICompletion(
            content="Bonjour !", provider="ollama", model="llama3.2"
        )
        self.stream_events = stream_events
        self.stream_error = stream_error
        self.mid_stream_error = mid_stream_error
        self.complete_error = complete_error
        self.calls: list[dict] = []

    async def complete(self, messages, config=None, *, trace_name=None, user_id=None):
        self.calls.append({"messages": messages, "config": config, "user_id": user_id})
        if self.complete_error is not None:
            raise self.complete_error
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


def _client(
    ai_client: _FakeAIClient | None = None, session: _FakeSession | None = None
) -> tuple[TestClient, _FakeAIClient]:
    app = create_app()
    fake = ai_client or _FakeAIClient()
    # Par défaut : un user sans credential IA (consommé seulement si la
    # requête ne porte pas de config — la cascade fait insert + select).
    fake_session = session or _FakeSession([[_user_row()]])
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="prof-123", email=None, roles=frozenset(), claims={}
    )
    app.dependency_overrides[get_db] = lambda: fake_session
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
def test_routes_require_token(client: TestClient, path: str) -> None:
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


def test_chat_without_config_or_credential_uses_fallback() -> None:
    client, fake = _client()
    response = client.post("/api/v1/ai/chat", json=NO_CONFIG_PAYLOAD)
    assert response.status_code == 200
    assert fake.calls[0]["config"] is None  # la résolution du fallback vit dans AIClient


# ------------------------------------------------- cascade credential utilisateur


def test_explicit_config_does_not_read_credential() -> None:
    session = _FakeSession()  # file vide : tout execute ferait sauter le test
    client, fake = _client(session=session)
    assert client.post("/api/v1/ai/chat", json=CHAT_PAYLOAD).status_code == 200
    assert session.executed == []
    assert fake.calls[0]["config"].provider.value == "ollama"


def test_chat_without_config_uses_decrypted_credential(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "AI_CREDENTIALS_MASTER_KEY", base64.urlsafe_b64encode(MASTER_KEY).decode()
    )
    salt = crypto.new_salt()
    user = _user_row(
        ai_provider="anthropic",
        ai_model="claude-sonnet-5",
        ai_api_key_encrypted=crypto.encrypt_secret("sk-user", MASTER_KEY, salt),
        ai_encryption_salt=salt,
    )
    client, fake = _client(session=_FakeSession([[user]]))
    assert client.post("/api/v1/ai/chat", json=NO_CONFIG_PAYLOAD).status_code == 200
    config = fake.calls[0]["config"]
    assert config.provider.value == "anthropic"
    assert config.model == "claude-sonnet-5"
    assert config.api_key.get_secret_value() == "sk-user"


def test_chat_unreadable_credential_422(monkeypatch) -> None:
    """Clé maître changée depuis l'enregistrement → 422 explicite, pas de fallback."""
    monkeypatch.setattr(
        settings, "AI_CREDENTIALS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode()
    )
    salt = crypto.new_salt()
    user = _user_row(
        ai_provider="anthropic",
        ai_model="claude-sonnet-5",
        ai_api_key_encrypted=crypto.encrypt_secret("sk-user", MASTER_KEY, salt),
        ai_encryption_salt=salt,
    )
    client, fake = _client(session=_FakeSession([[user]]))
    response = client.post("/api/v1/ai/chat", json=NO_CONFIG_PAYLOAD)
    assert response.status_code == 422
    assert "ré-enregistrez" in response.json()["detail"]
    assert fake.calls == []


def test_stream_without_config_unreadable_credential_422_eager(monkeypatch) -> None:
    """La cascade se résout AVANT le flux : vrai status HTTP, pas un event SSE."""
    monkeypatch.setattr(
        settings, "AI_CREDENTIALS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode()
    )
    salt = crypto.new_salt()
    user = _user_row(
        ai_provider="anthropic",
        ai_model="claude-sonnet-5",
        ai_api_key_encrypted=crypto.encrypt_secret("sk-user", MASTER_KEY, salt),
        ai_encryption_salt=salt,
    )
    client, _ = _client(session=_FakeSession([[user]]))
    response = client.post("/api/v1/ai/chat/stream", json=NO_CONFIG_PAYLOAD)
    assert response.status_code == 422


# -------------------------------------- quota quotidien de l'IA par défaut


def _quota_upserts(session: _FakeSession) -> list:
    """Les upserts du compteur quotidien (INSERT … ON CONFLICT sur ai_daily_usage)."""
    return [
        stmt
        for stmt, _ in session.executed
        if isinstance(stmt, Insert) and stmt.table.name == "ai_daily_usage"
    ]


def _refunds(session: _FakeSession) -> list:
    """Les updates de remboursement du quota (UPDATE ai_daily_usage)."""
    return [
        stmt
        for stmt, _ in session.executed
        if isinstance(stmt, Update) and stmt.table.name == "ai_daily_usage"
    ]


def _server_fallback(monkeypatch) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "AI_MODEL", "llama3.2")


def test_chat_fallback_consumes_default_quota(monkeypatch) -> None:
    """Repli sur l'IA serveur → un upsert atomique, gardé par le quota config."""
    _server_fallback(monkeypatch)
    session = _FakeSession([[_user_row()]])
    client, fake = _client(session=session)
    assert client.post("/api/v1/ai/chat", json=NO_CONFIG_PAYLOAD).status_code == 200
    assert fake.calls[0]["config"] is None
    [stmt] = _quota_upserts(session)
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert "ON CONFLICT (user_id, day) DO UPDATE" in str(compiled)
    # Plafond DANS le WHERE du DO UPDATE (atomique).
    assert "ai_daily_usage.calls < " in str(compiled)
    assert settings.AI_DEFAULT_DAILY_QUOTA in compiled.params.values()
    assert _refunds(session) == []  # succès : la réservation reste consommée


def test_user_quota_overrides_default(monkeypatch) -> None:
    _server_fallback(monkeypatch)
    session = _FakeSession([[_user_row(ai_daily_call_quota=5)]])
    client, _ = _client(session=session)
    assert client.post("/api/v1/ai/chat", json=NO_CONFIG_PAYLOAD).status_code == 200
    [stmt] = _quota_upserts(session)
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert 5 in compiled.params.values()
    assert settings.AI_DEFAULT_DAILY_QUOTA not in compiled.params.values()


def test_quota_zero_unlimited_but_counted(monkeypatch) -> None:
    """0 = illimité : l'upsert (statistique) part SANS garde de plafond."""
    _server_fallback(monkeypatch)
    session = _FakeSession([[_user_row(ai_daily_call_quota=0)]])
    client, fake = _client(session=session)
    assert client.post("/api/v1/ai/chat", json=NO_CONFIG_PAYLOAD).status_code == 200
    assert fake.calls != []
    [stmt] = _quota_upserts(session)
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (user_id, day) DO UPDATE" in compiled
    assert "ai_daily_usage.calls < " not in compiled


def test_chat_quota_exhausted_429(monkeypatch) -> None:
    _server_fallback(monkeypatch)
    session = _FakeSession([[_user_row()]], upsert_rowcount=0)
    client, fake = _client(session=session)
    response = client.post("/api/v1/ai/chat", json=NO_CONFIG_PAYLOAD)
    assert response.status_code == 429
    assert "Quota quotidien" in response.json()["detail"]
    assert fake.calls == []  # jamais d'appel provider quota épuisé


def test_stream_quota_exhausted_429_eager(monkeypatch) -> None:
    """La cascade se résout AVANT le flux : vrai 429 HTTP, pas un event SSE."""
    _server_fallback(monkeypatch)
    session = _FakeSession([[_user_row()]], upsert_rowcount=0)
    client, fake = _client(session=session)
    assert client.post("/api/v1/ai/chat/stream", json=NO_CONFIG_PAYLOAD).status_code == 429
    assert fake.calls == []


def test_chat_provider_failure_refunds_quota(monkeypatch) -> None:
    """Échec de l'appel provider : la réservation est remboursée — net-zéro."""
    _server_fallback(monkeypatch)
    session = _FakeSession([[_user_row()]])
    fake = _FakeAIClient(complete_error=HTTPException(503, detail="Fournisseur IA injoignable"))
    client, _ = _client(fake, session=session)
    assert client.post("/api/v1/ai/chat", json=NO_CONFIG_PAYLOAD).status_code == 503
    assert len(_quota_upserts(session)) == 1
    [refund] = _refunds(session)
    compiled = str(refund.compile(dialect=postgresql.dialect()))
    assert "calls - " in compiled  # décrément
    assert "calls > " in compiled  # garde : jamais négatif


def test_chat_byo_token_failure_refunds_nothing(monkeypatch) -> None:
    """Échec en config explicite : rien n'a été consommé, rien à rembourser."""
    _server_fallback(monkeypatch)
    session = _FakeSession()  # file vide : tout execute ferait sauter le test
    fake = _FakeAIClient(complete_error=HTTPException(503, detail="Fournisseur IA injoignable"))
    client, _ = _client(fake, session=session)
    assert client.post("/api/v1/ai/chat", json=CHAT_PAYLOAD).status_code == 503
    assert session.executed == []


def test_stream_eager_error_refunds(monkeypatch) -> None:
    """Erreur AVANT le flux (validation eager de stream) : remboursée aussi."""
    _server_fallback(monkeypatch)
    session = _FakeSession([[_user_row()]])
    fake = _FakeAIClient(stream_error=HTTPException(422, detail="Config IA invalide"))
    client, _ = _client(fake, session=session)
    assert client.post("/api/v1/ai/chat/stream", json=NO_CONFIG_PAYLOAD).status_code == 422
    assert len(_refunds(session)) == 1


def test_stream_failure_before_any_token_refunds(monkeypatch) -> None:
    """Erreur mid-stream sans aucun token émis : rien reçu → remboursé."""
    _server_fallback(monkeypatch)
    session = _FakeSession([[_user_row()]])
    fake = _FakeAIClient(
        stream_events=[AIStreamEvent(type="done")],
        mid_stream_error=HTTPException(503, detail="Fournisseur IA injoignable"),
    )
    client, _ = _client(fake, session=session)
    with client.stream("POST", "/api/v1/ai/chat/stream", json=NO_CONFIG_PAYLOAD) as response:
        assert response.status_code == 200  # le 200 est déjà parti, l'erreur est SSE
        body = response.read().decode("utf-8")
    assert _parse_sse(body)[-1][0] == "error"
    assert len(_refunds(session)) == 1


def test_stream_failure_after_tokens_stays_counted(monkeypatch) -> None:
    """Un flux qui a déjà produit du contenu reste compté (décision actée)."""
    _server_fallback(monkeypatch)
    session = _FakeSession([[_user_row()]])
    fake = _FakeAIClient(mid_stream_error=HTTPException(503, detail="Fournisseur IA injoignable"))
    client, _ = _client(fake, session=session)
    with client.stream("POST", "/api/v1/ai/chat/stream", json=NO_CONFIG_PAYLOAD) as response:
        body = response.read().decode("utf-8")
    assert _parse_sse(body)[-1][0] == "error"
    assert len(_quota_upserts(session)) == 1
    assert _refunds(session) == []


def test_explicit_config_never_quota(monkeypatch) -> None:
    """BYO token (config explicite) : aucun execute — donc aucun comptage."""
    _server_fallback(monkeypatch)
    session = _FakeSession()  # file vide : tout execute ferait sauter le test
    client, _ = _client(session=session)
    assert client.post("/api/v1/ai/chat", json=CHAT_PAYLOAD).status_code == 200
    assert session.executed == []


def test_user_credential_never_quota(monkeypatch) -> None:
    _server_fallback(monkeypatch)
    monkeypatch.setattr(
        settings, "AI_CREDENTIALS_MASTER_KEY", base64.urlsafe_b64encode(MASTER_KEY).decode()
    )
    salt = crypto.new_salt()
    user = _user_row(
        ai_provider="anthropic",
        ai_model="claude-sonnet-5",
        ai_api_key_encrypted=crypto.encrypt_secret("sk-user", MASTER_KEY, salt),
        ai_encryption_salt=salt,
    )
    session = _FakeSession([[user]])
    client, _ = _client(session=session)
    assert client.post("/api/v1/ai/chat", json=NO_CONFIG_PAYLOAD).status_code == 200
    assert _quota_upserts(session) == []


def test_without_server_fallback_no_quota() -> None:
    """AI_PROVIDER vide : le vrai AIClient répondra 422 — rien n'est consommé."""
    session = _FakeSession([[_user_row()]])
    client, _ = _client(session=session)
    assert client.post("/api/v1/ai/chat", json=NO_CONFIG_PAYLOAD).status_code == 200
    assert _quota_upserts(session) == []


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
def test_chat_invalid_payload(payload: dict) -> None:
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


def test_stream_error_before_flow() -> None:
    """Erreur eager (config invalide) → vrai status HTTP, pas un event SSE."""
    fake = _FakeAIClient(stream_error=HTTPException(422, detail="Clé API requise"))
    client, _ = _client(fake)
    response = client.post("/api/v1/ai/chat/stream", json=CHAT_PAYLOAD)
    assert response.status_code == 422
    assert response.json()["detail"] == "Clé API requise"


def test_stream_mid_stream_error() -> None:
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
