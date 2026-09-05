"""Tests des routes de l'assistant de cours (CRUD des conversations + flux SSE
du contexte ``course``) — aucun réseau, Postgres ni S3. Fakes partagés dans
``course_assistant_fakes.py`` ; les contextes d'édition et le flux HITL
(interrupt/reprise) sont couverts par ``test_course_assistant_hitl_api.py``.
"""

import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Delete, Update

from app.core.ai import AIStreamEvent, AIToolCall, AIUsage
from app.core.config import settings
from app.course_assistant.streaming import (
    MAX_MESSAGES_PER_CONVERSATION,
    TOOL_RESULT_EXCERPT_CHARS,
)
from tests.course_assistant_fakes import (
    BASE,
    BLOCK_ID,
    CONVERSATION_ID,
    NOW,
    RESOURCE_ID,
    STREAM_PATH,
    FakeAssistantAI,
    block_row,
    conversation_row,
    course_row,
    inserted_message_rows,
    make_client,
    message_row,
    resource_row,
    stream_session,
    user_row,
)
from tests.fakes import FakeSession, parse_sse

# ---------------------------------------------------------------- auth


def test_routes_require_token(client: TestClient) -> None:
    assert client.get(f"{BASE}/conversations").status_code == 401
    assert client.post(STREAM_PATH, json={"content": "Salut"}).status_code == 401


# ---------------------------------------------------------------- CRUD


def test_list_conversations() -> None:
    conv = conversation_row(title="Synthèse")
    session = FakeSession([[user_row()], [course_row()], [conv]])
    client, _ = make_client(session)
    response = client.get(f"{BASE}/conversations")
    assert response.status_code == 200
    [payload] = response.json()
    assert payload["id"] == str(CONVERSATION_ID)
    assert payload["title"] == "Synthèse"
    assert payload["context"] == "course"


def test_list_conversations_foreign_course_404() -> None:
    session = FakeSession([[user_row()], []])
    client, _ = make_client(session)
    response = client.get(f"{BASE}/conversations")
    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_create_conversation() -> None:
    session = FakeSession([[user_row()], [course_row()], [(NOW, NOW)]])
    client, _ = make_client(session)
    response = client.post(f"{BASE}/conversations", json={"context": "course"})
    assert response.status_code == 201
    payload = response.json()
    assert payload["context"] == "course"
    assert payload["title"] is None
    assert session.commits == 2  # get_or_create + create


def test_create_conversation_rejects_unshipped_context() -> None:
    session = FakeSession([[user_row()]])
    client, _ = make_client(session)
    response = client.post(f"{BASE}/conversations", json={"context": "module"})
    assert response.status_code == 422


def test_create_conversation_course_rejects_block_id() -> None:
    session = FakeSession([[user_row()]])
    client, _ = make_client(session)
    response = client.post(
        f"{BASE}/conversations",
        json={"context": "course", "block_id": str(BLOCK_ID)},
    )
    assert response.status_code == 422


def test_get_conversation_detail_with_tool_turns() -> None:
    messages = [
        message_row(0, role="user", content="Question"),
        message_row(
            1,
            role="assistant",
            content="",
            tool_calls=[{"id": "call_1", "name": "read_block", "arguments": {}}],
            provider="ollama",
        ),
        message_row(2, role="tool", content="CONTENU", tool_call_id="call_1"),
        message_row(
            3, role="assistant", content="Réponse", sources={"blocks": [str(BLOCK_ID)]}
        ),
    ]
    session = FakeSession(
        [[user_row()], [course_row()], [conversation_row(title="T")], messages]
    )
    client, _ = make_client(session)
    response = client.get(f"{BASE}/conversations/{CONVERSATION_ID}")
    assert response.status_code == 200
    payload = response.json()
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert payload["messages"][1]["tool_calls"][0]["id"] == "call_1"
    assert payload["messages"][3]["sources"] == {"blocks": [str(BLOCK_ID)]}


def test_rename_conversation() -> None:
    conv = conversation_row()
    session = FakeSession([[user_row()], [course_row()], [conv]])
    client, _ = make_client(session)
    response = client.patch(
        f"{BASE}/conversations/{CONVERSATION_ID}", json={"title": "Nouveau titre"}
    )
    assert response.status_code == 200
    assert response.json()["title"] == "Nouveau titre"
    assert conv.title == "Nouveau titre"


def test_delete_conversation() -> None:
    session = FakeSession([[user_row()], [course_row()], [conversation_row()]])
    client, _ = make_client(session)
    response = client.delete(f"{BASE}/conversations/{CONVERSATION_ID}")
    assert response.status_code == 204
    assert any(isinstance(stmt, Delete) for stmt, _ in session.executed)


def test_conversation_of_other_owner_404() -> None:
    session = FakeSession([[user_row()], [course_row()], []])
    client, _ = make_client(session)
    response = client.get(f"{BASE}/conversations/{CONVERSATION_ID}")
    assert response.status_code == 404
    assert response.json()["detail"] == "Conversation introuvable"


# ---------------------------------------------------------------- stream


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
    conv = conversation_row()
    session = stream_session(conversation=conv)
    client, fake = make_client(session, FakeAssistantAI(events=_nominal_events()))
    response = client.post(STREAM_PATH, json={"content": "Fais une synthèse du cours"})
    assert response.status_code == 200

    events = parse_sse(response.text)
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
    rows = inserted_message_rows(session)
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
    # Contexte course : pas de run checkpointé, rien à purger.
    assert call["thread_id"] is None
    assert fake.dropped_threads == []


def test_stream_rewrites_short_ref_citations_across_chunks() -> None:
    """Le modèle cite par référence courte (oc-block:B1), coupée entre plusieurs
    tokens : le flux, les sources et le texte persisté portent l'UUID."""
    events = [
        AIStreamEvent(type="token", delta="Voir [Intro](oc-"),
        AIStreamEvent(type="token", delta="block:B"),
        AIStreamEvent(type="token", delta="1) et [PDF](oc-resource:R1"),
        AIStreamEvent(type="token", delta=") puis oc-block:B9"),  # ref inconnue : inerte
        AIStreamEvent(type="done", usage=None),
    ]
    session = stream_session(conversation=conversation_row(title="T"))
    client, fake = make_client(session, FakeAssistantAI(events=events))
    response = client.post(STREAM_PATH, json={"content": "Cite tes sources"})
    assert response.status_code == 200

    out = parse_sse(response.text)
    expected = (
        f"Voir [Intro](oc-block:{BLOCK_ID}) et [PDF](oc-resource:{RESOURCE_ID}) "
        "puis oc-block:B9"
    )
    assert "".join(d["delta"] for k, d in out if k == "token") == expected
    assert "B1)" not in response.text  # jamais une référence brute côté front
    assert out[-1][1]["sources"] == {"blocks": [str(BLOCK_ID)], "resources": [str(RESOURCE_ID)]}
    rows = inserted_message_rows(session)
    assert rows[-1]["content"] == expected

    # Le modèle, lui, ne voit que des références courtes ; les specs des tools
    # portent l'enum de l'instantané.
    [call] = fake.calls
    system = call["messages"][0].content
    assert "(ref: B1)" in system and "(ref: R1," in system
    assert str(BLOCK_ID) not in system and str(RESOURCE_ID) not in system
    specs = {t.name: t for t in call["tools"]}
    assert specs["read_block"].parameters["properties"]["block_ref"]["enum"] == ["B1"]
    assert specs["read_resource_pdf"].parameters["properties"]["resource_ref"]["enum"] == ["R1"]
    assert "enum" not in specs["read_resource_image"].parameters["properties"]["resource_ref"]


def test_stream_replays_history() -> None:
    history = [
        message_row(0, role="user", content="Première question"),
        message_row(1, role="assistant", content="Première réponse", provider="ollama"),
    ]
    session = stream_session(messages=history, conversation=conversation_row(title="T"))
    client, fake = make_client(
        session,
        FakeAssistantAI(events=[AIStreamEvent(type="done", usage=None)]),
    )
    response = client.post(STREAM_PATH, json={"content": "Suite"})
    assert response.status_code == 200
    [call] = fake.calls
    roles = [m.role for m in call["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert call["messages"][1].content == "Première question"


def test_stream_course_not_found() -> None:
    session = FakeSession([[user_row()], []])
    client, _ = make_client(session)
    response = client.post(STREAM_PATH, json={"content": "Salut"})
    assert response.status_code == 404


def test_stream_conversation_full_422() -> None:
    messages = [message_row(i) for i in range(MAX_MESSAGES_PER_CONVERSATION)]
    session = FakeSession([[user_row()], [course_row()], [conversation_row()], messages])
    client, _ = make_client(session)
    response = client.post(STREAM_PATH, json={"content": "Encore"})
    assert response.status_code == 422
    assert "pleine" in response.json()["detail"]


def test_stream_default_quota_exhausted_429(monkeypatch) -> None:
    monkeypatch.setattr(settings, "AI_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "AI_MODEL", "llama3.2")
    no_credential = user_row(ai_provider=None, ai_model=None)
    session = FakeSession(
        [
            [no_credential],
            [course_row()],
            [conversation_row()],
            [],
            [no_credential],
        ],
        upsert_rowcount=0,  # garde du DO UPDATE non satisfaite : quota épuisé
    )
    client, _ = make_client(session)
    response = client.post(STREAM_PATH, json={"content": "Salut"})
    assert response.status_code == 429


def test_stream_eager_error_refunds_quota(monkeypatch) -> None:
    """Erreur eager de stream_agent : vraie HTTPException + remboursement."""
    monkeypatch.setattr(settings, "AI_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "AI_MODEL", "llama3.2")
    no_credential = user_row(ai_provider=None, ai_model=None)
    session = FakeSession(
        [
            [no_credential],
            [course_row()],
            [conversation_row()],
            [],
            [no_credential],
            [block_row()],
            [resource_row()],
            [],
        ]
    )
    client, _ = make_client(
        session,
        FakeAssistantAI(eager_error=HTTPException(status_code=422, detail="Config invalide")),
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
    session = stream_session(conversation=conversation_row(title="T"))
    client, _ = make_client(
        session,
        FakeAssistantAI(
            events=events,
            mid_stream_error=HTTPException(status_code=503, detail="Fournisseur IA injoignable"),
        ),
    )
    response = client.post(STREAM_PATH, json={"content": "Salut"})
    assert response.status_code == 200
    events_out = parse_sse(response.text)
    assert [k for k, _ in events_out] == ["token", "error"]
    assert events_out[-1][1] == {"status": 503, "detail": "Fournisseur IA injoignable"}
    rows = inserted_message_rows(session)
    assert [r["role"] for r in rows] == ["assistant"]
    assert rows[0]["content"] == "Début de réponse"
