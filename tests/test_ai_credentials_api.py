"""Routes /users/me/ai-credentials — aucun Postgres requis.

Motif test_users_api.py : fausse session FIFO (les SELECT consomment la file,
INSERT/DELETE tracés) + dependency_overrides. La clé maître est posée par
monkeypatch sur le singleton settings. Règle d'or vérifiée transversalement :
la clé API en clair n'apparaît dans AUCUN corps de réponse.
"""

import base64
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete, Insert

from app.ai_credentials import service as ai_credentials_service
from app.core import crypto
from app.core.ai import get_ai_client
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.main import create_app

URL = "/api/v1/users/me/ai-credentials"
TEST_URL = URL + "/test"
MODELS_URL = URL + "/models"
MASTER_KEY = os.urandom(32)
MASTER_KEY_B64 = base64.urlsafe_b64encode(MASTER_KEY).decode()
API_KEY = "sk-ant-ma-cle-api-secrete"


@pytest.fixture(autouse=True)
def _test_master_key(monkeypatch):
    monkeypatch.setattr(settings, "AI_CREDENTIALS_MASTER_KEY", MASTER_KEY_B64)


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


# Champs IA par défaut de la projection AICredentialsRead, valeurs du cas
# nominal des tests : pas de fallback serveur (AI_PROVIDER vide), quota
# standard (AI_DEFAULT_DAILY_QUOTA), aucune ligne d'usage aujourd'hui.
DEFAULT_QUOTA_FIELDS = {
    "default_ai_available": False,
    "daily_quota": 30,
    "calls_today": 0,
    "default_provider": None,
    "default_model": None,
}


def _user_with_key(**overrides):
    salt = crypto.new_salt()
    return _user_row(
        ai_provider="anthropic",
        ai_model="claude-sonnet-5",
        ai_api_key_encrypted=crypto.encrypt_secret(API_KEY, MASTER_KEY, salt),
        ai_encryption_salt=salt,
        **overrides,
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def one(self):
        [row] = self._rows
        return row

    def one_or_none(self):
        if not self._rows:
            return None
        [row] = self._rows
        return row


class _FakeSession:
    """FIFO des résultats de SELECT ; INSERT/DELETE tracés sans consommer."""

    def __init__(self, select_results=()):
        self._select_results = list(select_results)
        self.executed = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        if isinstance(stmt, (Insert, Delete)):
            return _FakeResult([])
        return _FakeResult(self._select_results.pop(0))

    async def commit(self):
        self.commits += 1


class _FakeAIClient:
    """``complete()`` scriptable — seul mode utilisé par le test de connexion.

    Une erreur scriptée est une HTTPException DÉJÀ traduite : c'est ce que le
    vrai AIClient laisse sortir (translate_provider_error au bord).
    """

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    async def complete(self, messages, config=None, **kwargs):
        self.calls.append((list(messages), config, kwargs))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content="ok")


def _client(session, ai_client=None) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="prof-123", email=None, roles=frozenset(), claims={}
    )
    app.dependency_overrides[get_db] = lambda: session
    if ai_client is not None:
        app.dependency_overrides[get_ai_client] = lambda: ai_client
    return TestClient(app)


# ---------------------------------------------------------------- auth


def test_routes_require_token(client: TestClient):
    cases = (
        ("get", URL, {}),
        ("put", URL, {"json": {}}),
        ("delete", URL, {}),
        ("post", TEST_URL, {"json": {}}),
        ("post", MODELS_URL, {"json": {}}),
    )
    for method, url, kwargs in cases:
        response = getattr(client, method)(url, **kwargs)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"


# ---------------------------------------------------------------- GET


def test_get_without_credential():
    response = _client(_FakeSession([[_user_row()], []])).get(URL)
    assert response.status_code == 200
    assert response.json() == {
        "provider": None,
        "model": None,
        "base_url": None,
        "api_key_set": False,
        **DEFAULT_QUOTA_FIELDS,
    }


def test_get_with_credential_never_reemits_key():
    response = _client(_FakeSession([[_user_with_key()], []])).get(URL)
    assert response.status_code == 200
    assert response.json() == {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "base_url": None,
        "api_key_set": True,
        **DEFAULT_QUOTA_FIELDS,
    }
    assert API_KEY not in response.text


def test_get_exposes_daily_quota(monkeypatch):
    """Fallback serveur configuré + quota individuel + usage du jour servis au front."""
    monkeypatch.setattr(settings, "AI_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "AI_MODEL", "llama3.2:latest")
    user = _user_row(ai_daily_call_quota=5)
    response = _client(_FakeSession([[user], [3]])).get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["default_ai_available"] is True
    assert body["daily_quota"] == 5
    assert body["calls_today"] == 3
    # Le modèle du fallback est affiché par le panneau assistant du front —
    # jamais AI_API_KEY ni AI_BASE_URL.
    assert body["default_provider"] == "ollama"
    assert body["default_model"] == "llama3.2:latest"


# ---------------------------------------------------------------- PUT


def test_put_nominal_encrypts_key():
    user = _user_row()
    session = _FakeSession([[user], []])
    response = _client(session).put(
        URL, json={"provider": "anthropic", "model": "claude-sonnet-5", "api_key": API_KEY}
    )
    assert response.status_code == 200
    assert response.json() == {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "base_url": None,
        "api_key_set": True,
        **DEFAULT_QUOTA_FIELDS,
    }
    assert API_KEY not in response.text
    assert user.ai_provider == "anthropic" and user.ai_model == "claude-sonnet-5"
    assert user.ai_api_key_encrypted is not None and user.ai_encryption_salt is not None
    assert API_KEY.encode() not in user.ai_api_key_encrypted
    assert (
        crypto.decrypt_secret(user.ai_api_key_encrypted, MASTER_KEY, user.ai_encryption_salt)
        == API_KEY
    )
    assert session.commits >= 2  # get_or_create + update


def test_put_without_key_keeps_blob_and_salt():
    user = _user_with_key()
    blob, salt = user.ai_api_key_encrypted, user.ai_encryption_salt
    response = _client(_FakeSession([[user], []])).put(
        URL, json={"provider": "anthropic", "model": "claude-opus-5"}
    )
    assert response.status_code == 200
    assert response.json()["model"] == "claude-opus-5"
    assert response.json()["api_key_set"] is True
    assert user.ai_api_key_encrypted is blob and user.ai_encryption_salt is salt


def test_put_new_key_regenerates_salt():
    user = _user_with_key()
    blob, salt = user.ai_api_key_encrypted, user.ai_encryption_salt
    response = _client(_FakeSession([[user], []])).put(
        URL, json={"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-nouvelle"}
    )
    assert response.status_code == 200
    assert user.ai_encryption_salt != salt and user.ai_api_key_encrypted != blob


@pytest.mark.parametrize(
    "payload",
    [
        # Clé requise pour un provider cloud (ni fournie ni en base).
        {"provider": "anthropic", "model": "claude-sonnet-5"},
        # Clé blanche interdite (omettre le champ pour conserver).
        {"provider": "anthropic", "model": "m", "api_key": "   "},
        # base_url requise pour openai_compatible.
        {"provider": "openai_compatible", "model": "m", "api_key": "k"},
        # base_url interdite hors ollama/openai_compatible.
        {"provider": "anthropic", "model": "m", "api_key": "k", "base_url": "https://x"},
        # extra=forbid.
        {"provider": "anthropic", "model": "m", "api_key": "k", "inconnu": True},
        # Provider hors AIProvider.
        {"provider": "skynet", "model": "m", "api_key": "k"},
    ],
)
def test_put_invalid(payload: dict):
    response = _client(_FakeSession([[_user_row()]])).put(URL, json=payload)
    assert response.status_code == 422


def test_put_ollama_without_key():
    user = _user_row()
    response = _client(_FakeSession([[user], []])).put(
        URL, json={"provider": "ollama", "model": "llama3.2", "base_url": "http://pi:11434"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "provider": "ollama",
        "model": "llama3.2",
        "base_url": "http://pi:11434",
        "api_key_set": False,
        **DEFAULT_QUOTA_FIELDS,
    }
    assert user.ai_api_key_encrypted is None and user.ai_encryption_salt is None


def test_put_503_without_master_key(monkeypatch):
    monkeypatch.setattr(settings, "AI_CREDENTIALS_MASTER_KEY", "")
    response = _client(_FakeSession([[_user_row()]])).put(
        URL, json={"provider": "anthropic", "model": "m", "api_key": API_KEY}
    )
    assert response.status_code == 503


# ---------------------------------------------------------------- DELETE


def test_delete_clears_everything():
    user = _user_with_key(ai_base_url=None)
    session = _FakeSession([[user]])
    response = _client(session).delete(URL)
    assert response.status_code == 204
    assert user.ai_provider is None and user.ai_model is None and user.ai_base_url is None
    assert user.ai_api_key_encrypted is None and user.ai_encryption_salt is None
    assert session.commits >= 2


def test_delete_idempotent():
    assert _client(_FakeSession([[_user_row()]])).delete(URL).status_code == 204


# ---------------------------------------------------------------- POST /test


def test_connection_with_explicit_key():
    ai = _FakeAIClient()
    session = _FakeSession([[_user_row()]])
    response = _client(session, ai).post(
        TEST_URL, json={"provider": "anthropic", "model": "claude-sonnet-5", "api_key": API_KEY}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert API_KEY not in response.text
    [(messages, config, _)] = ai.calls
    assert [m.role for m in messages] == ["user"]
    assert config.provider.value == "anthropic" and config.model == "claude-sonnet-5"
    assert config.api_key.get_secret_value() == API_KEY
    # BYO token intégral : aucun upsert de quota, seul get_or_create commite.
    assert session.commits == 1


def test_connection_uses_stored_key_when_omitted():
    ai = _FakeAIClient()
    response = _client(_FakeSession([[_user_with_key()]]), ai).post(
        TEST_URL, json={"provider": "anthropic", "model": "claude-opus-5"}
    )
    assert response.status_code == 200
    [(_, config, _)] = ai.calls
    assert config.api_key.get_secret_value() == API_KEY


def test_connection_ollama_without_key():
    ai = _FakeAIClient()
    response = _client(_FakeSession([[_user_row()]]), ai).post(
        TEST_URL, json={"provider": "ollama", "model": "llama3.2", "base_url": "http://pi:11434"}
    )
    assert response.status_code == 200
    [(_, config, _)] = ai.calls
    assert config.api_key is None and config.base_url == "http://pi:11434"


def test_connection_requires_key_when_none_stored():
    ai = _FakeAIClient()
    response = _client(_FakeSession([[_user_row()]]), ai).post(
        TEST_URL, json={"provider": "anthropic", "model": "claude-sonnet-5"}
    )
    assert response.status_code == 422
    assert ai.calls == []  # 422 AVANT tout appel provider


def test_connection_unreadable_credential():
    """Clé enregistrée illisible (clé maître changée) → 422, jamais d'appel."""
    user = _user_with_key()
    user.ai_encryption_salt = crypto.new_salt()  # sel ≠ celui du blob
    ai = _FakeAIClient()
    response = _client(_FakeSession([[user]]), ai).post(
        TEST_URL, json={"provider": "anthropic", "model": "claude-sonnet-5"}
    )
    assert response.status_code == 422
    assert ai.calls == []


def test_connection_provider_error_passthrough():
    """L'HTTPException traduite par app/core/ai remonte telle quelle (400 clé refusée)."""
    ai = _FakeAIClient(error=HTTPException(400, detail="Clé API refusée par le fournisseur IA"))
    response = _client(_FakeSession([[_user_row()]]), ai).post(
        TEST_URL, json={"provider": "openai", "model": "gpt-4o", "api_key": API_KEY}
    )
    assert response.status_code == 400
    assert API_KEY not in response.text


# ---------------------------------------------------------------- POST /models


@pytest.fixture
def fake_list_models(monkeypatch):
    """Remplace le list_models importé par le service ; enregistre les appels."""
    calls = []

    async def fake(provider, api_key, base_url):
        calls.append((provider, api_key, base_url))
        return ["modele-recent", "modele-ancien"]

    monkeypatch.setattr(ai_credentials_service, "list_models", fake)
    return calls


def test_models_with_explicit_key(fake_list_models):
    response = _client(_FakeSession([[_user_row()]])).post(
        MODELS_URL, json={"provider": "openai", "api_key": API_KEY}
    )
    assert response.status_code == 200
    assert response.json() == {"models": ["modele-recent", "modele-ancien"]}
    assert API_KEY not in response.text
    [(provider, api_key, base_url)] = fake_list_models
    assert provider.value == "openai"
    assert api_key.get_secret_value() == API_KEY
    assert base_url is None


def test_models_uses_stored_key_when_omitted(fake_list_models):
    response = _client(_FakeSession([[_user_with_key()]])).post(
        MODELS_URL, json={"provider": "anthropic"}
    )
    assert response.status_code == 200
    [(_, api_key, _)] = fake_list_models
    assert api_key.get_secret_value() == API_KEY


def test_models_ollama_without_key(fake_list_models):
    response = _client(_FakeSession([[_user_row()]])).post(
        MODELS_URL, json={"provider": "ollama", "base_url": "http://pi:11434"}
    )
    assert response.status_code == 200
    [(provider, api_key, base_url)] = fake_list_models
    assert provider.value == "ollama" and api_key is None and base_url == "http://pi:11434"


def test_models_requires_key_when_none_stored(fake_list_models):
    response = _client(_FakeSession([[_user_row()]])).post(MODELS_URL, json={"provider": "google"})
    assert response.status_code == 422
    assert fake_list_models == []


@pytest.mark.parametrize(
    "payload",
    [
        # base_url requise pour openai_compatible.
        {"provider": "openai_compatible"},
        # base_url interdite hors ollama/openai_compatible.
        {"provider": "anthropic", "api_key": "k", "base_url": "https://x"},
        # extra=forbid : pas de champ model sur ce payload.
        {"provider": "anthropic", "api_key": "k", "model": "m"},
        # Clé blanche interdite (omettre le champ pour la clé enregistrée).
        {"provider": "anthropic", "api_key": "   "},
    ],
)
def test_models_invalid_payload(fake_list_models, payload: dict):
    response = _client(_FakeSession([[_user_row()]])).post(MODELS_URL, json=payload)
    assert response.status_code == 422
    assert fake_list_models == []
