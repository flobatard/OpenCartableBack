"""Tests des routes de l'assistant pour les **propositions structurelles**
(contexte ``course`` à ``allow_edit`` : ajout, suppression, réordonnancement de
blocs) : tools et prompt du tour, proposition relayée aux args réécrits,
``interrupt`` de genre ``proposal`` sans ``agent``, reprise par la route de
décision — autorisée hors contexte d'édition par ``allow_edit`` — sur
l'instantané rechargé (``block_refs`` rejoués jusqu'aux tools : bloc apparu
nommé, bloc disparu toléré). Fakes partagés dans ``course_assistant_fakes.py``
(contrats FIFO inchangés).
"""

import asyncio
import uuid

from app.core.ai import AIStreamEvent, AIToolCall
from app.course_assistant import hitl
from app.course_assistant.prompts import COURSE_EDITING_SYSTEM_PROMPT
from app.course_assistant.structure import STRUCTURE_TOOL_NAMES
from tests.course_assistant_fakes import (
    BASE,
    BLOCK_ID,
    CONVERSATION_ID,
    STREAM_PATH,
    FakeAssistantAI,
    block_row,
    conversation_row,
    inserted_message_rows,
    make_client,
    message_row,
    proposal_interrupt_value,
    resume_session,
    stream_session,
)
from tests.fakes import parse_sse

DECISION_PATH = f"{BASE}/conversations/{CONVERSATION_ID}/proposals/call_s/decision"
SECOND_ID = uuid.uuid4()
NEW_ID = uuid.uuid4()
ADD_ARGS = {"type": "exercise", "title": "Application", "after_ref": "B1", "summary": "Ajout"}


def _blocks():
    return [block_row(), block_row(id=SECOND_ID, title="Bilan")]


def _course_conversation():
    return conversation_row(title="T")


def _tool_call(name, arguments, call_id="call_s"):
    return AIStreamEvent(
        type="tool_call", tool_call=AIToolCall(id=call_id, name=name, arguments=arguments)
    )


def _proposes(name, arguments):
    return [
        AIStreamEvent(type="token", delta="Je vous le propose. "),
        _tool_call(name, arguments),
        AIStreamEvent(type="interrupt", interrupt_value=proposal_interrupt_value("call_s")),
    ]


def _resumed(name, text="Décision rendue."):
    return [
        AIStreamEvent(type="tool_result", delta=text, tool_call=AIToolCall(id="call_s", name=name)),
        AIStreamEvent(type="token", delta="C'est fait."),
        AIStreamEvent(type="done"),
    ]


def _existing_turn(name):
    return [
        message_row(0, role="user"),
        message_row(1, role="assistant", tool_calls=[{"id": "call_s", "name": name}]),
    ]


def _pending(blocks, **overrides):
    defaults = dict(
        thread_id="t-run",
        tool_call_id="call_s",
        provider="ollama",
        config=None,
        allow_edit=True,
        block_refs={f"B{i}": str(b.id) for i, b in enumerate(blocks, start=1)},
    )
    defaults.update(overrides)
    return hitl.PendingInterrupt(**defaults)


def _run_tool(call, name, arguments, monkeypatch, accepted=True):
    """Ré-exécute le tool de la reprise comme le ferait le graphe (le faux
    client ne l'exécute pas) : la décision est la valeur de l'interrupt."""
    monkeypatch.setattr(
        hitl, "agent_interrupt", lambda payload: {"accepted": accepted, "comment": None}
    )
    return asyncio.run(
        call["tool_executor"](AIToolCall(id="call_s", name=name, arguments=arguments))
    )


# ------------------------------------------------------ tools et prompt du tour


def test_stream_allow_edit_exposes_structure_tools_and_prompt() -> None:
    session = stream_session(conversation=_course_conversation(), blocks=_blocks())
    client, fake = make_client(session, FakeAssistantAI(events=[AIStreamEvent(type="done")]))
    response = client.post(STREAM_PATH, json={"content": "Salut", "allow_edit": True})
    assert response.status_code == 200
    [call] = fake.calls
    specs = {t.name: t for t in call["tools"]}
    assert STRUCTURE_TOOL_NAMES <= set(specs)
    assert all(specs[name].blocking for name in STRUCTURE_TOOL_NAMES)
    system = call["messages"][0].content
    assert system == COURSE_EDITING_SYSTEM_PROMPT
    assert all(name in system for name in STRUCTURE_TOOL_NAMES)
    assert "Aucun outil ne crée" not in system


def test_stream_without_allow_edit_has_no_structure_tool() -> None:
    session = stream_session(conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(events=[AIStreamEvent(type="done")]))
    assert client.post(STREAM_PATH, json={"content": "Salut"}).status_code == 200
    [call] = fake.calls
    assert not {t.name for t in call["tools"]} & STRUCTURE_TOOL_NAMES


# ---------------------------------------------------------- aller : proposition


def test_stream_structure_proposal_is_relayed_and_registered() -> None:
    """La proposition de l'assistant global ferme le flux sur un ``interrupt``
    de genre ``proposal`` SANS ``agent`` ; ses args sont réécrits (ids
    résolus) sur le flux comme en base ; la reprise enregistrée retient
    ``allow_edit`` et la numérotation des blocs."""
    session = stream_session(conversation=_course_conversation(), blocks=_blocks())
    client, fake = make_client(
        session, FakeAssistantAI(events=_proposes("propose_block_add", ADD_ARGS))
    )
    try:
        response = client.post(
            STREAM_PATH, json={"content": "Ajoute un exercice", "allow_edit": True}
        )
        assert response.status_code == 200
        events_out = parse_sse(response.text)
        assert [k for k, _ in events_out] == ["token", "tool_call", "interrupt"]
        assert all("agent" not in data for _, data in events_out)
        relayed = events_out[1][1]["args"]
        assert relayed["after_id"] == str(BLOCK_ID)
        assert relayed["after_ref"] == "B1" and relayed["resource_id"] is None
        interrupt = events_out[-1][1]
        assert interrupt["tool_call_id"] == "call_s" and interrupt["kind"] == "proposal"

        [row] = inserted_message_rows(session)
        assert row["role"] == "assistant"
        assert row["tool_calls"][0]["name"] == "propose_block_add"
        assert row["tool_calls"][0]["arguments"]["after_id"] == str(BLOCK_ID)

        pending = hitl.peek(CONVERSATION_ID, "call_s", kind=hitl.KIND_PROPOSAL)
        assert pending is not None
        assert pending.allow_edit is True and pending.delegation is None
        assert pending.block_refs == {"B1": str(BLOCK_ID), "B2": str(SECOND_ID)}
        assert pending.thread_id == fake.calls[0]["thread_id"]
        assert fake.dropped_threads == []
    finally:
        hitl.drop(CONVERSATION_ID)


def test_stream_reorder_proposal_relays_the_block_ids() -> None:
    session = stream_session(conversation=_course_conversation(), blocks=_blocks())
    events = _proposes("propose_blocks_reorder", {"order": ["B2", "B1"], "summary": "Ordre"})
    client, _ = make_client(session, FakeAssistantAI(events=events))
    try:
        response = client.post(STREAM_PATH, json={"content": "Inverse", "allow_edit": True})
        relayed = parse_sse(response.text)[1][1]["args"]
        assert relayed["block_ids"] == [str(SECOND_ID), str(BLOCK_ID)]
    finally:
        hitl.drop(CONVERSATION_ID)


# ------------------------------------------------------------ retour : décision


def test_decision_resumes_the_global_assistant_on_the_reloaded_snapshot(monkeypatch) -> None:
    """Décision acceptée d'un ajout : le run de l'assistant global reprend
    (son thread, prompt et tools d'édition globale, valeur = décision) sur
    l'instantané rechargé — le tool ré-exécuté nomme le bloc créé dans la
    numérotation courante et rend le nouveau sommaire."""
    origin = _blocks()
    hitl.register(CONVERSATION_ID, _pending(origin))
    new = block_row(id=NEW_ID, type="exercise", title="Application", content={})
    session = resume_session(
        messages=_existing_turn("propose_block_add"),
        conversation=_course_conversation(),
        blocks=[origin[0], new, origin[1]],
    )
    client, fake = make_client(session, FakeAssistantAI(events=_resumed("propose_block_add")))
    response = client.post(DECISION_PATH, json={"accepted": True, "comment": "Oui"})
    assert response.status_code == 200
    events_out = parse_sse(response.text)
    assert [k for k, _ in events_out] == ["tool_result", "token", "done"]

    [call] = fake.calls
    assert call["thread_id"] == "t-run"
    assert call["resume"] == {"accepted": True, "comment": "Oui"}
    assert call["messages"][0].content == COURSE_EDITING_SYSTEM_PROMPT
    specs = {t.name: t for t in call["tools"]}
    assert STRUCTURE_TOOL_NAMES <= set(specs)
    assert specs["edit_block"].parameters["properties"]["target_ref"]["enum"] == [
        "B1",
        "B2",
        "B3",
    ]
    result = _run_tool(call, "propose_block_add", ADD_ARGS, monkeypatch)
    assert not result.is_error
    assert "Sa référence est B2." in result.content
    assert "B3 · Bilan (text)" in result.content

    rows = inserted_message_rows(session)
    assert [(r["role"], r["position"]) for r in rows] == [("tool", 2), ("assistant", 3)]
    assert hitl.take(CONVERSATION_ID, "call_s", kind=hitl.KIND_PROPOSAL) is None
    assert fake.dropped_threads == ["t-run"]


def test_accepted_delete_is_tolerated_on_the_reloaded_snapshot(monkeypatch) -> None:
    """Le bloc supprimé par le front a disparu de l'instantané : le tool
    ré-exécuté ne rend pas une erreur mais la décision et le sommaire."""
    origin = _blocks()
    hitl.register(CONVERSATION_ID, _pending(origin))
    session = resume_session(
        messages=_existing_turn("propose_block_delete"),
        conversation=_course_conversation(),
        blocks=[origin[1]],
    )
    client, fake = make_client(session, FakeAssistantAI(events=_resumed("propose_block_delete")))
    assert client.post(DECISION_PATH, json={"accepted": True}).status_code == 200
    [call] = fake.calls
    result = _run_tool(
        call, "propose_block_delete", {"target_ref": "B1", "summary": "Retrait"}, monkeypatch
    )
    assert not result.is_error
    assert "supprimé" in result.content and "B1 · Bilan (text)" in result.content


def test_rejected_proposal_resumes_with_the_refusal(monkeypatch) -> None:
    origin = _blocks()
    hitl.register(CONVERSATION_ID, _pending(origin))
    session = resume_session(
        messages=_existing_turn("propose_block_delete"),
        conversation=_course_conversation(),
        blocks=origin,
    )
    client, fake = make_client(session, FakeAssistantAI(events=_resumed("propose_block_delete")))
    response = client.post(DECISION_PATH, json={"accepted": False, "comment": "Non"})
    assert response.status_code == 200
    [call] = fake.calls
    assert call["resume"] == {"accepted": False, "comment": "Non"}
    result = _run_tool(
        call,
        "propose_block_delete",
        {"target_ref": "B1", "summary": "Retrait"},
        monkeypatch,
        accepted=False,
    )
    assert "REJETÉ" in result.content and "inchangée" in result.content


def test_decision_without_global_edit_is_404() -> None:
    """Hors édition globale (et hors délégation), une proposition en contexte
    ``course`` n'existe pas : 404 sans consommer le registre."""
    hitl.register(CONVERSATION_ID, _pending(_blocks(), allow_edit=False))
    try:
        session = resume_session(conversation=_course_conversation())
        client, fake = make_client(session, FakeAssistantAI(events=[]))
        assert client.post(DECISION_PATH, json={"accepted": True}).status_code == 404
        assert fake.calls == []
        assert hitl.peek(CONVERSATION_ID, "call_s", kind=hitl.KIND_PROPOSAL) is not None
    finally:
        hitl.drop(CONVERSATION_ID)
