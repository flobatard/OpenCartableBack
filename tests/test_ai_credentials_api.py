"""Routes /users/me/ai-credentials (configurations IA nommées) — aucun Postgres.

Motif test_users_api.py : fausse session FIFO (les SELECT consomment la file,
INSERT/UPDATE/DELETE tracés) + dependency_overrides. La clé maître est posée
par monkeypatch sur le singleton settings. Règle d'or vérifiée
transversalement : la clé API en clair n'apparaît dans AUCUN corps de réponse.

FIFO des routes : [user] (get_or_create_by_sub), puis [configurations] et
[usage du jour] pour les routes qui renvoient l'enveloppe ; les sondes ne
consomment que [config] quand ``config_id`` est fourni sans clé.
"""

import base64
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete, Insert, Update

from app.ai_credentials import service as ai_credentials_service
from app.core import crypto
from app.core.config import settings
from tests.fakes import FakeSession, make_client

URL = "/api/v1/users/me/ai-credentials"
ACTIVE_URL = URL + "/active"
TEST_URL = URL + "/test"
MODELS_URL = URL + "/models"
OPTIONS_URL = URL + "/reasoning-options"
MASTER_KEY = os.urandom(32)
MASTER_KEY_B64 = base64.urlsafe_b64encode(MASTER_KEY).decode()
API_KEY = "sk-ant-ma-cle-api-secrete"
NOW = datetime.now(UTC)
USER_ID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _test_master_key(monkeypatch):
    monkeypatch.setattr(settings, "AI_CREDENTIALS_MASTER_KEY", MASTER_KEY_B64)


def _user_row(**overrides):
    defaults = dict(id=USER_ID, sub="prof-123", email=None, ai_daily_call_quota=None)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _config_row(**overrides):
    """Une ligne ai_configurations : anthropic / claude-sonnet-5, clé chiffrée, active."""
    salt = crypto.new_salt()
    defaults = dict(
        id=uuid.uuid4(),
        user_id=USER_ID,
        name="Claude",
        provider="anthropic",
        model="claude-sonnet-5",
        base_url=None,
        api_key_encrypted=crypto.encrypt_secret(API_KEY, MASTER_KEY, salt),
        encryption_salt=salt,
        reasoning=None,
        reasoning_effort=None,
        is_active=True,
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _keyless(**overrides):
    return _config_row(api_key_encrypted=None, encryption_salt=None, **overrides)


# Champs IA par défaut de l'enveloppe, valeurs du cas nominal des tests : pas
# de fallback serveur (AI_PROVIDER vide), quota standard
# (AI_DEFAULT_DAILY_QUOTA), aucune ligne d'usage aujourd'hui.
DEFAULT_QUOTA_FIELDS = {
    "default_ai_available": False,
    "daily_quota": 30,
    "calls_today": 0,
    "default_provider": None,
    "default_model": None,
}
# Options du catalogue : pour claude-sonnet-5 (niveaux déclarés par le profil
# embarqué de langchain-anthropic) et pour un modèle Ollama quelconque
# (bascule think, sans niveau, non reconnu).
SONNET5_OPTIONS = {
    "toggle": ["on", "off"],
    "efforts": ["low", "medium", "high", "xhigh", "max"],
    "known": True,
}
OLLAMA_OPTIONS = {"toggle": ["on", "off"], "efforts": [], "known": False}


def _item(config, **overrides):
    """Projection AIConfigurationRead attendue d'une ligne."""
    item = {
        "id": str(config.id),
        "name": config.name,
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "api_key_set": config.api_key_encrypted is not None,
        "reasoning": config.reasoning,
        "reasoning_effort": config.reasoning_effort,
        "reasoning_options": SONNET5_OPTIONS,
    }
    item.update(overrides)
    return item


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


def _stmts(session, kind):
    return [stmt for stmt, _ in session.executed if isinstance(stmt, kind)]


# ---------------------------------------------------------------- auth


def test_routes_require_token(client: TestClient):
    some_id = uuid.uuid4()
    cases = (
        ("get", URL, {}),
        ("post", URL, {"json": {}}),
        ("put", ACTIVE_URL, {"json": {}}),
        ("put", f"{URL}/{some_id}", {"json": {}}),
        ("delete", f"{URL}/{some_id}", {}),
        ("post", TEST_URL, {"json": {}}),
        ("post", MODELS_URL, {"json": {}}),
        ("post", OPTIONS_URL, {"json": {}}),
    )
    for method, url, kwargs in cases:
        response = getattr(client, method)(url, **kwargs)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"


# ---------------------------------------------------------------- GET


def test_get_without_configuration():
    response = make_client(FakeSession([[_user_row()], [], []])).get(URL)
    assert response.status_code == 200
    assert response.json() == {"configurations": [], "active_id": None, **DEFAULT_QUOTA_FIELDS}


def test_get_lists_configurations_never_reemits_key():
    active = _config_row()
    other = _config_row(name="Ollama", provider="ollama", model="llama3.2", is_active=False)
    other.api_key_encrypted = None
    other.encryption_salt = None
    response = make_client(FakeSession([[_user_row()], [active, other], []])).get(URL)
    assert response.status_code == 200
    assert response.json() == {
        "configurations": [
            _item(active),
            _item(other, reasoning_options=OLLAMA_OPTIONS),
        ],
        "active_id": str(active.id),
        **DEFAULT_QUOTA_FIELDS,
    }
    assert API_KEY not in response.text


def test_get_no_active_when_all_inactive():
    config = _config_row(is_active=False)
    body = make_client(FakeSession([[_user_row()], [config], []])).get(URL).json()
    assert body["active_id"] is None
    assert [c["id"] for c in body["configurations"]] == [str(config.id)]


def test_get_exposes_reasoning_preferences():
    config = _config_row(reasoning=False, reasoning_effort="low")
    body = make_client(FakeSession([[_user_row()], [config], []])).get(URL).json()
    [item] = body["configurations"]
    assert item["reasoning"] is False and item["reasoning_effort"] == "low"


def test_get_exposes_daily_quota(monkeypatch):
    """Fallback serveur configuré + quota individuel + usage du jour servis au front."""
    monkeypatch.setattr(settings, "AI_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "AI_MODEL", "llama3.2:latest")
    user = _user_row(ai_daily_call_quota=5)
    response = make_client(FakeSession([[user], [], [3]])).get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["default_ai_available"] is True
    assert body["daily_quota"] == 5
    assert body["calls_today"] == 3
    # Le modèle du fallback est affiché par le panneau assistant du front —
    # jamais AI_API_KEY ni AI_BASE_URL.
    assert body["default_provider"] == "ollama"
    assert body["default_model"] == "llama3.2:latest"


# ---------------------------------------------------------------- POST (création)


def _create(session, **fields):
    payload = {"name": "Claude", "provider": "anthropic", "model": "claude-sonnet-5"}
    payload.update(fields)
    return make_client(session).post(URL, json=payload)


def test_post_creates_encrypts_and_activates():
    previous = _config_row(name="Ancienne")
    session = FakeSession([[_user_row()], [previous], []])
    response = _create(session, api_key=API_KEY)
    assert response.status_code == 201
    body = response.json()
    assert API_KEY not in response.text
    assert [c["name"] for c in body["configurations"]] == ["Ancienne", "Claude"]
    new = body["configurations"][1]
    assert new["api_key_set"] is True and new["reasoning_options"] == SONNET5_OPTIONS
    assert body["active_id"] == new["id"] != str(previous.id)
    assert {**body, "configurations": []} == {
        "configurations": [],
        "active_id": new["id"],
        **DEFAULT_QUOTA_FIELDS,
    }
    # Désactivation des autres AVANT l'insert de la nouvelle ligne active.
    [deactivate] = _stmts(session, Update)
    [inserted] = [s for s in _stmts(session, Insert) if s.table.name == "ai_configurations"]
    assert session.executed.index((deactivate, None)) < session.executed.index((inserted, None))
    params = inserted.compile().params
    assert params["is_active"] is True and params["name"] == "Claude"
    assert params["api_key_encrypted"] is not None and params["encryption_salt"] is not None
    assert API_KEY.encode() not in params["api_key_encrypted"]
    assert (
        crypto.decrypt_secret(params["api_key_encrypted"], MASTER_KEY, params["encryption_salt"])
        == API_KEY
    )
    assert session.commits >= 2  # get_or_create + création


def test_post_name_is_trimmed():
    session = FakeSession([[_user_row()], [], []])
    body = _create(session, name="  Mon Claude  ", api_key=API_KEY).json()
    assert body["configurations"][0]["name"] == "Mon Claude"


def test_post_ollama_without_key():
    session = FakeSession([[_user_row()], [], []])
    response = _create(
        session, name="Pi", provider="ollama", model="llama3.2", base_url="http://pi:11434"
    )
    assert response.status_code == 201
    [item] = response.json()["configurations"]
    assert item["api_key_set"] is False and item["base_url"] == "http://pi:11434"
    assert item["reasoning_options"] == OLLAMA_OPTIONS
    params = _stmts(session, Insert)[-1].compile().params
    assert params["api_key_encrypted"] is None and params["encryption_salt"] is None


def test_post_reasoning_preferences_roundtrip():
    session = FakeSession([[_user_row()], [], []])
    response = _create(session, api_key=API_KEY, reasoning=True, reasoning_effort="high")
    assert response.status_code == 201
    [item] = response.json()["configurations"]
    assert item["reasoning"] is True and item["reasoning_effort"] == "high"


def test_post_limit_reached():
    limit = ai_credentials_service.MAX_CONFIGURATIONS
    configs = [_config_row(is_active=False) for _ in range(limit)]
    session = FakeSession([[_user_row()], configs, []])
    response = _create(session, api_key=API_KEY)
    assert response.status_code == 422
    assert _stmts(session, Update) == []
    assert [s for s in _stmts(session, Insert) if s.table.name == "ai_configurations"] == []


@pytest.mark.parametrize(
    "payload",
    [
        # Nom requis / blanc.
        {"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "k"},
        {"name": "   ", "provider": "anthropic", "model": "claude-sonnet-5", "api_key": "k"},
        # Clé requise pour un provider cloud à la création.
        {"name": "n", "provider": "anthropic", "model": "claude-sonnet-5"},
        # Clé blanche interdite (omettre le champ pour conserver).
        {"name": "n", "provider": "anthropic", "model": "m", "api_key": "   "},
        # base_url requise pour openai_compatible.
        {"name": "n", "provider": "openai_compatible", "model": "m", "api_key": "k"},
        # base_url interdite hors ollama/openai_compatible.
        {"name": "n", "provider": "anthropic", "model": "m", "api_key": "k", "base_url": "https://x"},
        # extra=forbid.
        {"name": "n", "provider": "anthropic", "model": "m", "api_key": "k", "inconnu": True},
        # Provider hors AIProvider.
        {"name": "n", "provider": "skynet", "model": "m", "api_key": "k"},
        # Bascule et effort hors providers capables (mistral : rien).
        {"name": "n", "provider": "mistral", "model": "m", "api_key": "k", "reasoning": True},
        {"name": "n", "provider": "mistral", "model": "m", "api_key": "k", "reasoning_effort": "h"},
        # Niveau qui n'est pas un niveau NATIF du provider.
        {
            "name": "n",
            "provider": "anthropic",
            "model": "m",
            "api_key": "k",
            "reasoning_effort": "turbo",
        },
        {"name": "n", "provider": "openai", "model": "o3", "api_key": "k", "reasoning_effort": "z"},
        {"name": "n", "provider": "ollama", "model": "gpt-oss", "reasoning_effort": "xhigh"},
    ],
)
def test_post_invalid(payload: dict):
    response = make_client(FakeSession([[_user_row()], []])).post(URL, json=payload)
    assert response.status_code == 422


def test_post_503_without_master_key(monkeypatch):
    monkeypatch.setattr(settings, "AI_CREDENTIALS_MASTER_KEY", "")
    assert _create(FakeSession([[_user_row()], []]), api_key=API_KEY).status_code == 503


# ---------------------------------------------------------------- PUT /{id}


def _update(session, config_id, **fields):
    payload = {"name": "Claude", "provider": "anthropic", "model": "claude-opus-5"}
    payload.update(fields)
    return make_client(session).put(f"{URL}/{config_id}", json=payload)


def test_put_without_key_keeps_blob_and_salt():
    config = _config_row()
    blob, salt = config.api_key_encrypted, config.encryption_salt
    session = FakeSession([[_user_row()], [config], []])
    response = _update(session, config.id, name="Opus")
    assert response.status_code == 200
    [item] = response.json()["configurations"]
    assert item["name"] == "Opus" and item["model"] == "claude-opus-5"
    assert item["api_key_set"] is True
    assert response.json()["active_id"] == str(config.id)
    assert config.api_key_encrypted is blob and config.encryption_salt is salt
    assert config.model == "claude-opus-5" and config.updated_at != NOW
    assert session.commits >= 2


def test_put_new_key_regenerates_salt():
    config = _config_row()
    blob, salt = config.api_key_encrypted, config.encryption_salt
    response = _update(FakeSession([[_user_row()], [config], []]), config.id, api_key="sk-nouvelle")
    assert response.status_code == 200
    assert config.encryption_salt != salt and config.api_key_encrypted != blob
    assert crypto.decrypt_secret(config.api_key_encrypted, MASTER_KEY, config.encryption_salt) == (
        "sk-nouvelle"
    )


def test_put_keeps_inactive_status():
    config = _config_row(is_active=False)
    body = _update(FakeSession([[_user_row()], [config], []]), config.id).json()
    assert body["active_id"] is None and config.is_active is False


def test_put_requires_key_when_none_stored():
    config = _keyless(provider="ollama", model="llama3.2")
    response = _update(FakeSession([[_user_row()], [config], []]), config.id)
    assert response.status_code == 422


def test_put_unknown_or_foreign_is_404():
    session = FakeSession([[_user_row()], [_config_row()], []])
    response = _update(session, uuid.uuid4())
    assert response.status_code == 404
    assert response.json()["detail"] == "Configuration introuvable"


def test_put_native_level_and_options_follow_the_model():
    """Un niveau natif du provider est accepté même hors des options proposées
    (le catalogue propose, le provider tranche) ; la réponse porte les options
    du nouveau couple."""
    config = _config_row()
    response = _update(
        FakeSession([[_user_row()], [config], []]),
        config.id,
        model="claude-opus-4-5",
        reasoning_effort="xhigh",
    )
    assert response.status_code == 200
    [item] = response.json()["configurations"]
    assert item["reasoning_effort"] == "xhigh"
    assert item["reasoning_options"] == {
        "toggle": ["on", "off"],
        "efforts": ["low", "medium", "high"],
        "known": True,
    }


def test_put_replaces_reasoning_preferences():
    """Un PUT sans préférences remet les colonnes à NULL (remplacement, pas patch)."""
    config = _config_row(reasoning=True, reasoning_effort="high")
    response = _update(FakeSession([[_user_row()], [config], []]), config.id)
    assert response.status_code == 200
    assert config.reasoning is None and config.reasoning_effort is None


def test_put_explicit_null_preferences_accepted_for_any_provider():
    """Le front envoie toujours les deux champs (null hors capacités du provider)."""
    config = _config_row()
    response = _update(
        FakeSession([[_user_row()], [config], []]),
        config.id,
        provider="mistral",
        model="magistral-medium-latest",
        api_key=API_KEY,
        reasoning=None,
        reasoning_effort=None,
    )
    assert response.status_code == 200


def test_put_invalid_payload():
    config = _config_row()
    response = make_client(FakeSession([[_user_row()], [config]])).put(
        f"{URL}/{config.id}", json={"provider": "anthropic", "model": "m"}  # nom manquant
    )
    assert response.status_code == 422


def test_put_non_uuid_id_is_422():
    response = make_client(FakeSession([[_user_row()]])).put(
        f"{URL}/pas-un-uuid", json={"name": "n", "provider": "anthropic", "model": "m"}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- PUT /active


def test_activate_configuration():
    old = _config_row(name="Ancienne")
    new = _config_row(name="Nouvelle", is_active=False)
    session = FakeSession([[_user_row()], [old, new], []])
    response = make_client(session).put(ACTIVE_URL, json={"id": str(new.id)})
    assert response.status_code == 200
    assert response.json()["active_id"] == str(new.id)
    assert [c["name"] for c in response.json()["configurations"]] == ["Ancienne", "Nouvelle"]
    # Désactivation Core exécutée AVANT l'activation ORM (index partiel unique).
    [deactivate] = _stmts(session, Update)
    assert deactivate.table.name == "ai_configurations"
    assert new.is_active is True and new.updated_at != NOW
    assert session.commits >= 2


def test_activate_none_returns_to_default_ai():
    config = _config_row()
    session = FakeSession([[_user_row()], [config], []])
    response = make_client(session).put(ACTIVE_URL, json={"id": None})
    assert response.status_code == 200
    assert response.json()["active_id"] is None
    assert len(_stmts(session, Update)) == 1
    # Les configurations sont conservées.
    assert [c["id"] for c in response.json()["configurations"]] == [str(config.id)]


def test_activate_unknown_is_404():
    session = FakeSession([[_user_row()], [_config_row()], []])
    response = make_client(session).put(ACTIVE_URL, json={"id": str(uuid.uuid4())})
    assert response.status_code == 404
    assert _stmts(session, Update) == []


def test_activate_invalid_payload():
    response = make_client(FakeSession([[_user_row()]])).put(ACTIVE_URL, json={"inconnu": 1})
    assert response.status_code == 422


# ---------------------------------------------------------------- DELETE /{id}


def test_delete_configuration():
    config = _config_row()
    session = FakeSession([[_user_row()], [config]])
    response = make_client(session).delete(f"{URL}/{config.id}")
    assert response.status_code == 204
    [stmt] = _stmts(session, Delete)
    assert stmt.table.name == "ai_configurations"
    assert session.commits >= 2


def test_delete_unknown_is_404():
    session = FakeSession([[_user_row()], [_config_row()]])
    response = make_client(session).delete(f"{URL}/{uuid.uuid4()}")
    assert response.status_code == 404
    assert _stmts(session, Delete) == []


# ---------------------------------------------------------------- POST /test


def test_connection_with_explicit_key():
    ai = _FakeAIClient()
    session = FakeSession([[_user_row()]])
    response = make_client(session, ai_client=ai).post(
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


def test_connection_uses_stored_key_of_configuration():
    ai = _FakeAIClient()
    config = _config_row()
    response = make_client(FakeSession([[_user_row()], [config]]), ai_client=ai).post(
        TEST_URL,
        json={"provider": "anthropic", "model": "claude-opus-5", "config_id": str(config.id)},
    )
    assert response.status_code == 200
    [(_, config_used, _)] = ai.calls
    assert config_used.api_key.get_secret_value() == API_KEY


def test_connection_unknown_configuration_is_404():
    ai = _FakeAIClient()
    response = make_client(FakeSession([[_user_row()], []]), ai_client=ai).post(
        TEST_URL,
        json={"provider": "anthropic", "model": "claude-opus-5", "config_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404
    assert ai.calls == []


def test_connection_passes_reasoning_preferences():
    """Le test valide exactement ce que l'écriture enregistrerait, raisonnement compris."""
    ai = _FakeAIClient()
    response = make_client(FakeSession([[_user_row()]]), ai_client=ai).post(
        TEST_URL,
        json={
            "provider": "anthropic",
            "model": "claude-opus-5",
            "api_key": API_KEY,
            "reasoning": True,
            "reasoning_effort": "low",
        },
    )
    assert response.status_code == 200
    [(_, config, _)] = ai.calls
    assert config.reasoning is True and config.reasoning_effort == "low"


def test_connection_rejects_reasoning_for_incapable_provider():
    ai = _FakeAIClient()
    response = make_client(FakeSession([[_user_row()]]), ai_client=ai).post(
        TEST_URL,
        json={"provider": "mistral", "model": "m", "api_key": API_KEY, "reasoning": True},
    )
    assert response.status_code == 422
    assert ai.calls == []


def test_connection_rejects_name():
    """Le test ne porte pas de nom (extra=forbid) : le front l'en retire."""
    ai = _FakeAIClient()
    response = make_client(FakeSession([[_user_row()]]), ai_client=ai).post(
        TEST_URL, json={"name": "n", "provider": "anthropic", "model": "m", "api_key": API_KEY}
    )
    assert response.status_code == 422


def test_connection_ollama_without_key():
    ai = _FakeAIClient()
    response = make_client(FakeSession([[_user_row()]]), ai_client=ai).post(
        TEST_URL, json={"provider": "ollama", "model": "llama3.2", "base_url": "http://pi:11434"}
    )
    assert response.status_code == 200
    [(_, config, _)] = ai.calls
    assert config.api_key is None and config.base_url == "http://pi:11434"


def test_connection_requires_key_without_configuration():
    ai = _FakeAIClient()
    session = FakeSession([[_user_row()]])
    response = make_client(session, ai_client=ai).post(
        TEST_URL, json={"provider": "anthropic", "model": "claude-sonnet-5"}
    )
    assert response.status_code == 422
    assert ai.calls == []  # 422 AVANT tout appel provider
    assert len(session.executed) == 2  # get_or_create seulement : aucun select de config


def test_connection_unreadable_configuration():
    """Clé enregistrée illisible (clé maître changée) → 422, jamais d'appel."""
    config = _config_row()
    config.encryption_salt = crypto.new_salt()  # sel ≠ celui du blob
    ai = _FakeAIClient()
    response = make_client(FakeSession([[_user_row()], [config]]), ai_client=ai).post(
        TEST_URL,
        json={"provider": "anthropic", "model": "claude-sonnet-5", "config_id": str(config.id)},
    )
    assert response.status_code == 422
    assert ai.calls == []


def test_connection_provider_error_passthrough():
    """L'HTTPException traduite par app/core/ai remonte telle quelle (400 clé refusée)."""
    ai = _FakeAIClient(error=HTTPException(400, detail="Clé API refusée par le fournisseur IA"))
    response = make_client(FakeSession([[_user_row()]]), ai_client=ai).post(
        TEST_URL, json={"provider": "openai", "model": "gpt-4o", "api_key": API_KEY}
    )
    assert response.status_code == 400
    assert API_KEY not in response.text


# ---------------------------------------------------------------- POST /reasoning-options


def test_reasoning_options_known_model():
    session = FakeSession()  # sonde pure : aucun execute
    response = make_client(session).post(
        OPTIONS_URL, json={"provider": "openai", "model": "gpt-5.2"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "toggle": ["on", "off"],
        "efforts": ["low", "medium", "high", "xhigh"],
        "known": True,
    }
    assert session.executed == []


def test_reasoning_options_unknown_model_falls_back_to_provider():
    response = make_client(FakeSession()).post(
        OPTIONS_URL, json={"provider": "openai_compatible", "model": "llama-3.3-70b-versatile"}
    )
    assert response.status_code == 200
    assert response.json() == {"toggle": [], "efforts": ["low", "medium", "high"], "known": False}


def test_reasoning_options_non_reasoning_model():
    response = make_client(FakeSession()).post(
        OPTIONS_URL, json={"provider": "openai", "model": "gpt-4o"}
    )
    assert response.status_code == 200
    assert response.json() == {"toggle": [], "efforts": [], "known": True}


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "openai"},  # modèle requis
        {"provider": "skynet", "model": "m"},
        {"provider": "openai", "model": "gpt-5", "api_key": "k"},  # extra=forbid
    ],
)
def test_reasoning_options_invalid_payload(payload: dict):
    assert make_client(FakeSession()).post(OPTIONS_URL, json=payload).status_code == 422


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
    response = make_client(FakeSession([[_user_row()]])).post(
        MODELS_URL, json={"provider": "openai", "api_key": API_KEY}
    )
    assert response.status_code == 200
    assert response.json() == {"models": ["modele-recent", "modele-ancien"]}
    assert API_KEY not in response.text
    [(provider, api_key, base_url)] = fake_list_models
    assert provider.value == "openai"
    assert api_key.get_secret_value() == API_KEY
    assert base_url is None


def test_models_uses_stored_key_of_configuration(fake_list_models):
    config = _config_row()
    response = make_client(FakeSession([[_user_row()], [config]])).post(
        MODELS_URL, json={"provider": "anthropic", "config_id": str(config.id)}
    )
    assert response.status_code == 200
    [(_, api_key, _)] = fake_list_models
    assert api_key.get_secret_value() == API_KEY


def test_models_unknown_configuration_is_404(fake_list_models):
    response = make_client(FakeSession([[_user_row()], []])).post(
        MODELS_URL, json={"provider": "anthropic", "config_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404
    assert fake_list_models == []


def test_models_ollama_without_key(fake_list_models):
    response = make_client(FakeSession([[_user_row()]])).post(
        MODELS_URL, json={"provider": "ollama", "base_url": "http://pi:11434"}
    )
    assert response.status_code == 200
    [(provider, api_key, base_url)] = fake_list_models
    assert provider.value == "ollama" and api_key is None and base_url == "http://pi:11434"


def test_models_requires_key_without_configuration(fake_list_models):
    response = make_client(FakeSession([[_user_row()]])).post(
        MODELS_URL, json={"provider": "google"}
    )
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
        # extra=forbid : les préférences de raisonnement n'ont rien à faire
        # dans un listing (le front les retire du payload d'écriture).
        {"provider": "anthropic", "api_key": "k", "reasoning": True},
        {"provider": "anthropic", "api_key": "k", "reasoning_effort": "high"},
        # extra=forbid : ni le nom.
        {"provider": "anthropic", "api_key": "k", "name": "n"},
    ],
)
def test_models_invalid_payload(fake_list_models, payload: dict):
    response = make_client(FakeSession([[_user_row()]])).post(MODELS_URL, json=payload)
    assert response.status_code == 422
    assert fake_list_models == []
