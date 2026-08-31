"""Tests des routes de l'assistant de cours — aucun réseau, Postgres ni S3.

Motif ``test_ai_api.py`` : fausse session FIFO (l'ordre des ``execute`` de
chaque fonction de service est un contrat, documenté dans leurs docstrings),
``_FakeAssistantAI`` scripté injecté via ``dependency_overrides[get_ai_client]``,
SSE lu en corps complet via le TestClient.

Ordre FIFO du flux de stream (docstring de ``sse_stream``) : [user] (router),
[course], [conversation], [messages], [user] (cascade ``effective_config``),
[blocks], [resources], [modules] — puis le generator insère le tour.
"""

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete, Insert, Update

from app.core.ai import AIStreamEvent, AIToolCall, AIUsage, get_ai_client
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.storage import get_storage
from app.course_assistant.service import (
    MAX_MESSAGES_PER_CONVERSATION,
    TOOL_RESULT_EXCERPT_CHARS,
)
from app.main import create_app

NOW = datetime.now(UTC)
USER_ID = uuid.uuid4()
COURSE_ID = uuid.uuid4()
CONVERSATION_ID = uuid.uuid4()
BLOCK_ID = uuid.uuid4()
RESOURCE_ID = uuid.uuid4()

BASE = f"/api/v1/courses/{COURSE_ID}/assistant"
STREAM_PATH = f"{BASE}/conversations/{CONVERSATION_ID}/messages/stream"


def _user_row(**overrides):
    defaults = dict(
        id=USER_ID,
        sub="prof-123",
        email=None,
        ai_provider="ollama",
        ai_model="llama3.2",
        ai_base_url=None,
        ai_api_key_encrypted=None,
        ai_encryption_salt=None,
        ai_daily_call_quota=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _course_row():
    return SimpleNamespace(
        id=COURSE_ID,
        owner_id=USER_ID,
        title="Géométrie",
        description=None,
        updated_at=NOW,
    )


def _conversation_row(**overrides):
    defaults = dict(
        id=CONVERSATION_ID,
        course_id=COURSE_ID,
        owner_id=USER_ID,
        context="course",
        block_id=None,
        module_id=None,
        title=None,
        created_at=NOW,
        updated_at=NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _message_row(position, role="user", **overrides):
    defaults = dict(
        id=uuid.uuid4(),
        conversation_id=CONVERSATION_ID,
        role=role,
        position=position,
        content=f"Message {position}",
        tool_calls=[],
        tool_call_id="call_x" if role == "tool" else None,
        is_error=False,
        provider=None,
        sources={},
        input_tokens=None,
        output_tokens=None,
        created_at=NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _block_row():
    return SimpleNamespace(
        id=BLOCK_ID,
        course_id=COURSE_ID,
        type="text",
        title="Intro",
        description=None,
        content={"markdown": "Pythagore."},
        resource_id=None,
        module_id=None,
    )


def _resource_row():
    return SimpleNamespace(
        id=RESOURCE_ID,
        course_id=COURSE_ID,
        original_name="cours.pdf",
        type="document",
        mime="application/pdf",
        size=1234,
        status="available",
        s3_key="k",
    )


class _FakeResult:
    def __init__(self, rows, rowcount=1):
        self._rows = rows
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def one(self):
        [row] = self._rows
        return row

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """FIFO des SELECT ; un Insert porteur de RETURNING consomme aussi la file
    (motif test_courses_api.py) ; les écritures sont tracées."""

    def __init__(self, select_results=(), upsert_rowcount=1):
        self._select_results = list(select_results)
        self.upsert_rowcount = upsert_rowcount
        self.executed = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        self.executed.append((stmt, params))
        if isinstance(stmt, Insert):
            if stmt._returning:
                return _FakeResult(self._select_results.pop(0))
            return _FakeResult([], rowcount=self.upsert_rowcount)
        if isinstance(stmt, (Delete, Update)):
            return _FakeResult([], rowcount=self.upsert_rowcount)
        return _FakeResult(self._select_results.pop(0))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        """Filet best-effort du remboursement — jamais atteint en nominal."""


class _FakeAssistantAI:
    """Faux AIClient pour stream_agent : validation eager scriptable."""

    def __init__(self, events=None, eager_error=None, mid_stream_error=None):
        self.events = events or []
        self.eager_error = eager_error
        self.mid_stream_error = mid_stream_error
        self.calls = []

    def stream_agent(
        self,
        messages,
        config=None,
        *,
        tools,
        tool_executor,
        max_tool_rounds=5,
        trace_name=None,
        user_id=None,
    ):
        if self.eager_error is not None:
            raise self.eager_error
        self.calls.append(
            {"messages": messages, "config": config, "tools": tools, "user_id": user_id}
        )
        return self._gen()

    async def _gen(self):
        for event in self.events:
            if self.mid_stream_error is not None and event.type == "done":
                raise self.mid_stream_error
            yield event


def _client(session, ai_client=None):
    app = create_app()
    fake_ai = ai_client or _FakeAssistantAI()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        sub="prof-123", email=None, roles=frozenset(), claims={}
    )
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_ai_client] = lambda: fake_ai
    app.dependency_overrides[get_storage] = lambda: SimpleNamespace()
    return TestClient(app), fake_ai


def _parse_sse(body: str):
    events = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def _inserted_message_rows(session):
    """Les params de l'insert executemany du tour (liste de dicts), ou None."""
    for stmt, params in session.executed:
        if isinstance(stmt, Insert) and isinstance(params, list):
            return params
    return None


# ---------------------------------------------------------------- auth


def test_routes_require_token(client: TestClient) -> None:
    assert client.get(f"{BASE}/conversations").status_code == 401
    assert client.post(STREAM_PATH, json={"content": "Salut"}).status_code == 401


# ---------------------------------------------------------------- CRUD


def test_list_conversations() -> None:
    conv = _conversation_row(title="Synthèse")
    session = _FakeSession([[_user_row()], [_course_row()], [conv]])
    client, _ = _client(session)
    response = client.get(f"{BASE}/conversations")
    assert response.status_code == 200
    [payload] = response.json()
    assert payload["id"] == str(CONVERSATION_ID)
    assert payload["title"] == "Synthèse"
    assert payload["context"] == "course"


def test_list_conversations_foreign_course_404() -> None:
    session = _FakeSession([[_user_row()], []])
    client, _ = _client(session)
    response = client.get(f"{BASE}/conversations")
    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_create_conversation() -> None:
    session = _FakeSession([[_user_row()], [_course_row()], [(NOW, NOW)]])
    client, _ = _client(session)
    response = client.post(f"{BASE}/conversations", json={"context": "course"})
    assert response.status_code == 201
    payload = response.json()
    assert payload["context"] == "course"
    assert payload["title"] is None
    assert session.commits == 2  # get_or_create + create


def test_create_conversation_rejects_unshipped_context() -> None:
    session = _FakeSession([[_user_row()]])
    client, _ = _client(session)
    response = client.post(f"{BASE}/conversations", json={"context": "module"})
    assert response.status_code == 422


def test_get_conversation_detail_with_tool_turns() -> None:
    messages = [
        _message_row(0, role="user", content="Question"),
        _message_row(
            1,
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "name": "read_block", "arguments": {}}],
            provider="ollama",
        ),
        _message_row(2, role="tool", content="CONTENU", tool_call_id="call_1"),
        _message_row(
            3, role="assistant", content="Réponse", sources={"blocks": [str(BLOCK_ID)]}
        ),
    ]
    session = _FakeSession(
        [[_user_row()], [_course_row()], [_conversation_row(title="T")], messages]
    )
    client, _ = _client(session)
    response = client.get(f"{BASE}/conversations/{CONVERSATION_ID}")
    assert response.status_code == 200
    payload = response.json()
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert payload["messages"][1]["tool_calls"][0]["id"] == "call_1"
    assert payload["messages"][3]["sources"] == {"blocks": [str(BLOCK_ID)]}


def test_rename_conversation() -> None:
    conv = _conversation_row()
    session = _FakeSession([[_user_row()], [_course_row()], [conv]])
    client, _ = _client(session)
    response = client.patch(
        f"{BASE}/conversations/{CONVERSATION_ID}", json={"title": "Nouveau titre"}
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Nouveau titre"
    assert conv.title == "Nouveau titre"


def test_delete_conversation() -> None:
    session = _FakeSession([[_user_row()], [_course_row()], [_conversation_row()]])
    client, _ = _client(session)
    response = client.delete(f"{BASE}/conversations/{CONVERSATION_ID}")
    assert response.status_code == 204
    assert any(isinstance(stmt, Delete) for stmt, _ in session.executed)


def test_conversation_of_other_owner_404() -> None:
    session = _FakeSession([[_user_row()], [_course_row()], []])
    client, _ = _client(session)
    response = client.get(f"{BASE}/conversations/{CONVERSATION_ID}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Conversation introuvable"


# ---------------------------------------------------------------- stream


def _stream_session(messages=(), conversation=None, user=None):
    return _FakeSession(
        [
            [user or _user_row()],  # router : get_or_create_by_sub
            [_course_row()],
            [conversation or _conversation_row()],
            list(messages),
            [user or _user_row()],  # cascade effective_config
            [_block_row()],
            [_resource_row()],
            [],  # modules
        ]
    )


# Résultat d'outil plus long que l'extrait relayé sur le flux.
TOOL_CONTENT = "CONTENU DU BLOC " * 100


def _nominal_events(final_text: str | None = None):
    final = final_text or f"Voir [Intro](oc-block:{BLOCK_ID}) et oc-block:{uuid.uuid4()}."
    return [
        AIStreamEvent(type="thinking", delta="je réfléchis"),
        AIStreamEvent(
            type="tool_call",
            tool_call=AIToolCall(id="call_1", name="read_block", arguments={"block_id": "x"}),
        ),
        AIStreamEvent(
            type="tool_result",
            delta=TOOL_CONTENT,
            tool_call=AIToolCall(id="call_1", name="read_block"),
            tool_result_error=False,
        ),
        AIStreamEvent(type="token", delta=final),
        AIStreamEvent(type="done", usage=AIUsage(input_tokens=30, output_tokens=8)),
    ]


def test_stream_nominal() -> None:
    conv = _conversation_row()
    session = _stream_session(conversation=conv)
    client, fake = _client(session, _FakeAssistantAI(events=_nominal_events()))
    response = client.post(STREAM_PATH, json={"content": "Fais une synthèse du cours"})
    assert response.status_code == 200

    events = _parse_sse(response.text)
    kinds = [k for k, _ in events]
    assert kinds == ["thinking", "tool_call", "tool_result", "token", "done"]

    done = events[-1][1]
    assert done["usage"] == {"input_tokens": 30, "output_tokens": 8}
    assert done["sources"]["blocks"] == [str(BLOCK_ID)]  # l'id halluciné est filtré
    assert done["title"] == "Fais une synthèse du cours"
    assert len(done["message_ids"]) == 3  # segment à tool_calls + tour tool + final

    # Seul un extrait borné du résultat d'outil part sur le flux (+ longueur).
    tool_result = events[2][1]
    assert tool_result["excerpt"] == TOOL_CONTENT[:TOOL_RESULT_EXCERPT_CHARS]
    assert tool_result["length"] == len(TOOL_CONTENT)
    assert TOOL_CONTENT not in response.text

    # Persistance du tour : rôles/positions/données de replay.
    rows = _inserted_message_rows(session)
    assert [r["role"] for r in rows] == ["assistant", "tool", "assistant"]
    assert [r["position"] for r in rows] == [1, 2, 3]
    assert rows[0]["tool_calls"] == [
        {"id": "call_1", "name": "read_block", "arguments": {"block_id": "x"}}
    ]
    assert rows[0]["provider"] == "ollama"
    assert rows[1]["tool_call_id"] == "call_1"
    assert rows[1]["content"] == TOOL_CONTENT  # persistance intégrale
    assert rows[2]["sources"]["blocks"] == [str(BLOCK_ID)]
    assert rows[2]["input_tokens"] == 30

    # Titre posé au premier message + contexte cours envoyé au modèle.
    assert conv.title == "Fais une synthèse du cours"
    [call] = fake.calls
    assert call["messages"][0].role == "system"
    assert "Pythagore." in call["messages"][0].content
    assert call["messages"][-1].content == "Fais une synthèse du cours"
    assert {t.name for t in call["tools"]} == {
        "read_block",
        "read_resource_pdf",
        "read_resource_image",
        "read_module",
    }


def test_stream_replays_history() -> None:
    history = [
        _message_row(0, role="user", content="Première question"),
        _message_row(1, role="assistant", content="Première réponse", provider="ollama"),
    ]
    session = _stream_session(messages=history, conversation=_conversation_row(title="T"))
    client, fake = _client(
        session,
        _FakeAssistantAI(events=[AIStreamEvent(type="done", usage=None)]),
    )
    response = client.post(STREAM_PATH, json={"content": "Suite"})
    assert response.status_code == 200
    [call] = fake.calls
    roles = [m.role for m in call["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert call["messages"][1].content == "Première question"


def test_stream_course_not_found() -> None:
    session = _FakeSession([[_user_row()], []])
    client, _ = _client(session)
    response = client.post(STREAM_PATH, json={"content": "Salut"})
    assert response.status_code == 404


def test_stream_conversation_full_422() -> None:
    messages = [_message_row(i) for i in range(MAX_MESSAGES_PER_CONVERSATION)]
    session = _FakeSession(
        [[_user_row()], [_course_row()], [_conversation_row()], messages]
    )
    client, _ = _client(session)
    response = client.post(STREAM_PATH, json={"content": "Encore"})
    assert response.status_code == 422
    assert "pleine" in response.json()["detail"]


def test_stream_default_quota_exhausted_429(monkeypatch) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "AI_MODEL", "llama3.2")
    no_credential = _user_row(ai_provider=None, ai_model=None)
    session = _FakeSession(
        [
            [no_credential],
            [_course_row()],
            [_conversation_row()],
            [],
            [no_credential],
        ],
        upsert_rowcount=0,  # garde du DO UPDATE non satisfaite : quota épuisé
    )
    client, _ = _client(session)
    response = client.post(STREAM_PATH, json={"content": "Salut"})
    assert response.status_code == 429


def test_stream_eager_error_refunds_quota(monkeypatch) -> None:
    """Erreur eager de stream_agent : vraie HTTPException + remboursement."""
    monkeypatch.setattr(settings, "AI_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "AI_MODEL", "llama3.2")
    no_credential = _user_row(ai_provider=None, ai_model=None)
    session = _FakeSession(
        [
            [no_credential],
            [_course_row()],
            [_conversation_row()],
            [],
            [no_credential],
            [_block_row()],
            [_resource_row()],
            [],
        ]
    )
    client, _ = _client(
        session,
        _FakeAssistantAI(eager_error=HTTPException(status_code=422, detail="Config invalide")),
    )
    response = client.post(STREAM_PATH, json={"content": "Salut"})
    assert response.status_code == 422
    assert any(isinstance(stmt, Update) for stmt, _ in session.executed)  # remboursement


def test_stream_mid_stream_error_persists_partial() -> None:
    """Erreur après premiers tokens : événement error + partiel persisté."""
    events = [
        AIStreamEvent(type="token", delta="Début de réponse"),
        AIStreamEvent(type="done"),  # jamais atteint (mid_stream_error)
    ]
    session = _stream_session(conversation=_conversation_row(title="T"))
    client, _ = _client(
        session,
        _FakeAssistantAI(
            events=events,
            mid_stream_error=HTTPException(status_code=503, detail="Fournisseur IA injoignable"),
        ),
    )
    response = client.post(STREAM_PATH, json={"content": "Salut"})
    assert response.status_code == 200
    events_out = _parse_sse(response.text)
    assert [k for k, _ in events_out] == ["token", "error"]
    assert events_out[-1][1] == {"status": 503, "detail": "Fournisseur IA injoignable"}
    rows = _inserted_message_rows(session)
    assert [r["role"] for r in rows] == ["assistant"]
    assert rows[0]["content"] == "Début de réponse"
