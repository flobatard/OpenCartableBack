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
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete, Insert

from app.core import crypto
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.main import create_app

URL = "/api/v1/users/me/ai-credentials"
CLE_MAITRE = os.urandom(32)
CLE_MAITRE_B64 = base64.urlsafe_b64encode(CLE_MAITRE).decode()
CLE_API = "sk-ant-ma-cle-api-secrete"


@pytest.fixture(autouse=True)
def _cle_maitre_de_test(monkeypatch):
    monkeypatch.setattr(settings, "AI_CREDENTIALS_MASTER_KEY", CLE_MAITRE_B64)


def _user_row(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        sub="prof-123",
        email=None,
        ai_provider=None,
        ai_model=None,
        ai_base_url=None,
        ai_api_key_chiffree=None,
        ai_chiffrement_sel=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _user_avec_cle(**overrides):
    sel = crypto.nouveau_sel()
    return _user_row(
        ai_provider="anthropic",
        ai_model="claude-sonnet-5",
        ai_api_key_chiffree=crypto.chiffrer_secret(CLE_API, CLE_MAITRE, sel),
        ai_chiffrement_sel=sel,
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


def _client(session) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="prof-123", email=None, roles=frozenset(), claims={}
    )
    app.dependency_overrides[get_db] = lambda: session
    return TestClient(app)


# ---------------------------------------------------------------- auth


def test_routes_requierent_un_token(client: TestClient):
    for method, kwargs in (("get", {}), ("put", {"json": {}}), ("delete", {})):
        response = getattr(client, method)(URL, **kwargs)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"


# ---------------------------------------------------------------- GET


def test_get_sans_credential():
    response = _client(_FakeSession([[_user_row()]])).get(URL)
    assert response.status_code == 200
    assert response.json() == {
        "provider": None,
        "model": None,
        "base_url": None,
        "api_key_definie": False,
    }


def test_get_avec_credential_ne_reemet_jamais_la_cle():
    response = _client(_FakeSession([[_user_avec_cle()]])).get(URL)
    assert response.status_code == 200
    assert response.json() == {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "base_url": None,
        "api_key_definie": True,
    }
    assert CLE_API not in response.text


# ---------------------------------------------------------------- PUT


def test_put_nominal_chiffre_la_cle():
    user = _user_row()
    session = _FakeSession([[user]])
    response = _client(session).put(
        URL, json={"provider": "anthropic", "model": "claude-sonnet-5", "api_key": CLE_API}
    )
    assert response.status_code == 200
    assert response.json() == {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "base_url": None,
        "api_key_definie": True,
    }
    assert CLE_API not in response.text
    assert user.ai_provider == "anthropic" and user.ai_model == "claude-sonnet-5"
    assert user.ai_api_key_chiffree is not None and user.ai_chiffrement_sel is not None
    assert CLE_API.encode() not in user.ai_api_key_chiffree
    assert (
        crypto.dechiffrer_secret(user.ai_api_key_chiffree, CLE_MAITRE, user.ai_chiffrement_sel)
        == CLE_API
    )
    assert session.commits >= 2  # get_or_create + update


def test_put_sans_cle_conserve_blob_et_sel():
    user = _user_avec_cle()
    blob, sel = user.ai_api_key_chiffree, user.ai_chiffrement_sel
    response = _client(_FakeSession([[user]])).put(
        URL, json={"provider": "anthropic", "model": "claude-opus-5"}
    )
    assert response.status_code == 200
    assert response.json()["model"] == "claude-opus-5"
    assert response.json()["api_key_definie"] is True
    assert user.ai_api_key_chiffree is blob and user.ai_chiffrement_sel is sel


def test_put_nouvelle_cle_regenere_le_sel():
    user = _user_avec_cle()
    blob, sel = user.ai_api_key_chiffree, user.ai_chiffrement_sel
    response = _client(_FakeSession([[user]])).put(
        URL, json={"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-nouvelle"}
    )
    assert response.status_code == 200
    assert user.ai_chiffrement_sel != sel and user.ai_api_key_chiffree != blob


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
def test_put_invalide(payload: dict):
    response = _client(_FakeSession([[_user_row()]])).put(URL, json=payload)
    assert response.status_code == 422


def test_put_ollama_sans_cle():
    user = _user_row()
    response = _client(_FakeSession([[user]])).put(
        URL, json={"provider": "ollama", "model": "llama3.2", "base_url": "http://pi:11434"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "provider": "ollama",
        "model": "llama3.2",
        "base_url": "http://pi:11434",
        "api_key_definie": False,
    }
    assert user.ai_api_key_chiffree is None and user.ai_chiffrement_sel is None


def test_put_503_sans_cle_maitre(monkeypatch):
    monkeypatch.setattr(settings, "AI_CREDENTIALS_MASTER_KEY", "")
    response = _client(_FakeSession([[_user_row()]])).put(
        URL, json={"provider": "anthropic", "model": "m", "api_key": CLE_API}
    )
    assert response.status_code == 503


# ---------------------------------------------------------------- DELETE


def test_delete_efface_tout():
    user = _user_avec_cle(ai_base_url=None)
    session = _FakeSession([[user]])
    response = _client(session).delete(URL)
    assert response.status_code == 204
    assert user.ai_provider is None and user.ai_model is None and user.ai_base_url is None
    assert user.ai_api_key_chiffree is None and user.ai_chiffrement_sel is None
    assert session.commits >= 2


def test_delete_idempotent():
    assert _client(_FakeSession([[_user_row()]])).delete(URL).status_code == 204
