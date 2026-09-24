"""Tests des routes de l'assistant pour les **questions au professeur**
(``ask_questions``, tous contextes) : interrupt et registre à l'aller, reprise
par la route de réponse (réponses ou refus), 404/422 sans consommer la
reprise, abandon par un nouveau message ou la suppression de la conversation,
et purge du thread d'un flux refermé sans suite. Fakes partagés dans
``course_assistant_fakes.py`` (contrats FIFO documentés là).
"""

import uuid

import pytest

from app.core.ai import AIStreamEvent, AIToolCall, AIUsage
from app.course_assistant import hitl
from app.course_assistant.context import build_refs
from app.course_assistant.streaming import _AssistantTurn, _release_on_close
from tests.course_assistant_fakes import (
    BASE,
    BLOCK_ID,
    CONVERSATION_ID,
    STREAM_PATH,
    FakeAssistantAI,
    conversation_row,
    course_row,
    inserted_message_rows,
    make_client,
    message_row,
    questions_interrupt_value,
    resume_session,
    stream_session,
    user_row,
)
from tests.fakes import FakeSession, parse_sse

ANSWER_PATH = f"{BASE}/conversations/{CONVERSATION_ID}/questions/call_q/answer"
DECISION_PATH = f"{BASE}/conversations/{CONVERSATION_ID}/proposals/call_p/decision"

QUESTIONS_ARGS = {
    "questions": [
        {
            "question": "Quel niveau visez-vous ?",
            "multi_select": False,
            "options": [{"label": "Seconde"}, {"label": "Première"}],
        },
        {
            "question": "Quelles notions inclure ?",
            "multi_select": True,
            "options": [{"label": "Dérivée"}, {"label": "Limites"}, {"label": "Suites"}],
        },
    ]
}
SHAPE = [{"multi_select": False, "options": 2}, {"multi_select": True, "options": 3}]


def _pending_questions(thread_id="t-run", tool_call_id="call_q"):
    return hitl.PendingInterrupt(
        thread_id=thread_id,
        tool_call_id=tool_call_id,
        provider="ollama",
        config=None,
        kind=hitl.KIND_QUESTIONS,
        answer_shape=SHAPE,
    )


def _course_conversation():
    return conversation_row(title="T")


def _block_text_conversation():
    return conversation_row(context="block_text", block_id=BLOCK_ID, title="T")


# ------------------------------------------------------- aller : interrupt


def test_stream_course_questions_interrupt_registers_resume() -> None:
    """Contexte ``course`` : l'appel de questions fige le run — ``interrupt``
    de genre ``questions``, questions relayées intactes dans les args du
    ``tool_call``, tour partiel persisté, reprise enregistrée avec la forme de
    réponse attendue, thread conservé."""
    events = [
        AIStreamEvent(type="token", delta="Quelques précisions d'abord. "),
        AIStreamEvent(
            type="tool_call",
            tool_call=AIToolCall(id="call_q", name="ask_questions", arguments=QUESTIONS_ARGS),
        ),
        AIStreamEvent(
            type="interrupt",
            interrupt_value=questions_interrupt_value(shape=SHAPE),
            usage=AIUsage(input_tokens=90, output_tokens=30),
        ),
    ]
    session = stream_session(conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(events=events))
    try:
        response = client.post(STREAM_PATH, json={"content": "Crée un exercice"})
        assert response.status_code == 200

        events_out = parse_sse(response.text)
        assert [k for k, _ in events_out] == ["token", "tool_call", "interrupt"]
        assert events_out[1][1]["args"] == QUESTIONS_ARGS
        interrupt = events_out[-1][1]
        assert interrupt["tool_call_id"] == "call_q"
        assert interrupt["kind"] == "questions"
        assert len(interrupt["message_ids"]) == 1

        rows = inserted_message_rows(session)
        assert [r["role"] for r in rows] == ["assistant"]
        assert rows[0]["tool_calls"] == [
            {"id": "call_q", "name": "ask_questions", "arguments": QUESTIONS_ARGS}
        ]

        [call] = fake.calls
        pending = hitl.peek(CONVERSATION_ID, "call_q", kind=hitl.KIND_QUESTIONS)
        assert pending is not None
        assert pending.thread_id == call["thread_id"]
        assert pending.answer_shape == SHAPE
        assert fake.dropped_threads == []  # le run attend sa reprise
    finally:
        hitl.drop(CONVERSATION_ID)


def test_new_message_abandons_pending_questions_in_course_context() -> None:
    """Un nouveau message alors que des questions attendaient abandonne la
    reprise (registre vidé, thread purgé), et les reprises expirées d'autres
    conversations sont balayées au passage."""
    other_conversation = uuid.uuid4()
    expired = _pending_questions(thread_id="t-expired")
    expired.created_at -= hitl.PENDING_TTL_SECONDS + 1
    hitl.register(CONVERSATION_ID, _pending_questions(thread_id="t-stale"))
    hitl.register(other_conversation, expired)
    try:
        session = stream_session(conversation=_course_conversation())
        client, fake = make_client(
            session, FakeAssistantAI(events=[AIStreamEvent(type="done", usage=None)])
        )
        response = client.post(STREAM_PATH, json={"content": "Laisse tomber, autre chose"})
        assert response.status_code == 200
        [call] = fake.calls
        assert fake.dropped_threads == ["t-stale", "t-expired", call["thread_id"]]
        assert hitl.peek(CONVERSATION_ID, "call_q", kind=hitl.KIND_QUESTIONS) is None
        assert hitl.drop(other_conversation) is None
    finally:
        hitl.drop(CONVERSATION_ID)
        hitl.drop(other_conversation)


def test_delete_conversation_drops_pending_resume() -> None:
    """Supprimer une conversation abandonne sa reprise (registre + thread)."""
    hitl.register(CONVERSATION_ID, _pending_questions(thread_id="t-deleted"))
    try:
        session = FakeSession([[user_row()], [course_row()], [_course_conversation()], []])
        client, fake = make_client(session)
        response = client.delete(f"{BASE}/conversations/{CONVERSATION_ID}")
        assert response.status_code == 204
        assert fake.dropped_threads == ["t-deleted"]
        assert hitl.drop(CONVERSATION_ID) is None
    finally:
        hitl.drop(CONVERSATION_ID)


# ------------------------------------------------------- reprise : réponse


def _resumed_events(result: str):
    return [
        AIStreamEvent(
            type="tool_result",
            delta=result,
            tool_call=AIToolCall(id="call_q", name="ask_questions"),
            tool_result_error=False,
        ),
        AIStreamEvent(type="token", delta="Merci, je prépare l'exercice."),
        AIStreamEvent(type="done", usage=None),
    ]


def _existing_turn():
    """Tour partiel déjà persisté : user (0), assistant à tool_call (1)."""
    return [
        message_row(0, role="user"),
        message_row(1, role="assistant", tool_calls=[{"id": "call_q", "name": "ask_questions"}]),
    ]


def test_questions_answer_resumes_the_run() -> None:
    """La réponse REPREND le run figé (contexte ``course``) : valeur de
    reprise normalisée, graphe rebâti avec les tools du contexte (questions
    comprises, sans proposition), suite du tour persistée à la suite, thread
    purgé au ``done``, reprise consommée."""
    hitl.register(CONVERSATION_ID, _pending_questions())
    try:
        session = resume_session(messages=_existing_turn(), conversation=_course_conversation())
        client, fake = make_client(
            session, FakeAssistantAI(events=_resumed_events("Le professeur a répondu…"))
        )
        response = client.post(
            ANSWER_PATH,
            json={
                "answers": [
                    {"selected": [1]},
                    {"selected": [0, 2], "other": "  les   tangentes "},
                ]
            },
        )
        assert response.status_code == 200
        events_out = parse_sse(response.text)
        assert [k for k, _ in events_out] == ["tool_result", "token", "done"]
        assert events_out[-1][1]["user_message_id"] is None

        [call] = fake.calls
        assert call["thread_id"] == "t-run"
        assert call["resume"] == {
            "declined": False,
            "answers": [
                {"selected": [1], "other": None},
                {"selected": [0, 2], "other": "les tangentes"},
            ],
        }
        assert [m.role for m in call["messages"]] == ["system"]
        names = {t.name for t in call["tools"]}
        assert "ask_questions" in names
        assert not any(name.startswith("propose_") for name in names)
        assert fake.dropped_threads == ["t-run"]

        rows = inserted_message_rows(session)
        assert [(r["role"], r["position"]) for r in rows] == [("tool", 2), ("assistant", 3)]
        assert rows[0]["tool_call_id"] == "call_q"
        assert hitl.peek(CONVERSATION_ID, "call_q", kind=hitl.KIND_QUESTIONS) is None
    finally:
        hitl.drop(CONVERSATION_ID)


def test_questions_decline_resumes_block_text_run() -> None:
    """Le refus reprend aussi le run (ici dans un contexte d'édition, dont les
    tools de proposition restent exposés)."""
    hitl.register(CONVERSATION_ID, _pending_questions(thread_id="t-edit"))
    try:
        session = resume_session(messages=_existing_turn())
        client, fake = make_client(
            session,
            FakeAssistantAI(events=_resumed_events("Le professeur a préféré ne pas répondre…")),
        )
        response = client.post(ANSWER_PATH, json={"declined": True})
        assert response.status_code == 200
        [call] = fake.calls
        assert call["resume"] == {"declined": True, "answers": None}
        assert {"ask_questions", "propose_block_edit"} <= {t.name for t in call["tools"]}
        assert fake.dropped_threads == ["t-edit"]
    finally:
        hitl.drop(CONVERSATION_ID)


def test_questions_answer_without_pending_404() -> None:
    session = FakeSession([[user_row()], [course_row()], [_course_conversation()]])
    client, _ = make_client(session)
    response = client.post(ANSWER_PATH, json={"declined": True})
    assert response.status_code == 404
    assert response.json()["detail"] == "Aucune question en attente pour cet appel"


def test_questions_answer_on_pending_proposal_404_keeps_registry() -> None:
    """Une réponse ne reprend jamais une proposition du même appel : 404, et
    la proposition attend toujours sa décision."""
    hitl.register(
        CONVERSATION_ID,
        hitl.PendingInterrupt(
            thread_id="t-p", tool_call_id="call_q", provider="ollama", config=None
        ),
    )
    try:
        session = FakeSession([[user_row()], [course_row()], [_block_text_conversation()]])
        client, fake = make_client(session)
        response = client.post(ANSWER_PATH, json={"declined": True})
        assert response.status_code == 404
        assert fake.calls == []
        assert hitl.peek(CONVERSATION_ID, "call_q", kind=hitl.KIND_PROPOSAL) is not None
    finally:
        hitl.drop(CONVERSATION_ID)


def test_proposal_decision_on_pending_questions_404_keeps_registry() -> None:
    """Une décision ne reprend jamais des questions du même appel : 404, et
    les questions attendent toujours leur réponse."""
    hitl.register(CONVERSATION_ID, _pending_questions(tool_call_id="call_p"))
    try:
        session = FakeSession([[user_row()], [course_row()], [_block_text_conversation()]])
        client, fake = make_client(session)
        response = client.post(DECISION_PATH, json={"accepted": True})
        assert response.status_code == 404
        assert fake.calls == []
        assert hitl.peek(CONVERSATION_ID, "call_p", kind=hitl.KIND_QUESTIONS) is not None
    finally:
        hitl.drop(CONVERSATION_ID)


@pytest.mark.parametrize(
    ("answers", "detail"),
    [
        ([{"selected": [0]}], "2 réponse(s) attendue(s), 1 reçue(s)"),
        ([{"selected": [2]}, {"selected": [0]}], "Question 1 : choix inconnu"),
        ([{"selected": [0, 1]}, {"selected": [0]}], "Question 1 : un seul choix attendu"),
        (
            [{"selected": [0], "other": "Terminale"}, {"selected": [0]}],
            "Question 1 : un seul choix attendu",
        ),
        ([{"selected": [0]}, {"selected": []}], "Question 2 : réponse manquante"),
        ([{"selected": [0]}, {"selected": [], "other": "   "}], "Question 2 : réponse manquante"),
        ([{"selected": [0]}, {"selected": [1, 1]}], "Question 2 : choix en double"),
    ],
)
def test_questions_answer_invalid_against_shape_422_keeps_registry(answers, detail) -> None:
    """Réponse incohérente avec les questions posées : 422 avant toute
    consommation — la reprise reste disponible pour une réponse corrigée."""
    hitl.register(CONVERSATION_ID, _pending_questions())
    try:
        session = FakeSession([[user_row()], [course_row()], [_course_conversation()]])
        client, fake = make_client(session)
        response = client.post(ANSWER_PATH, json={"answers": answers})
        assert response.status_code == 422
        assert response.json()["detail"] == detail
        assert fake.calls == []
        assert hitl.peek(CONVERSATION_ID, "call_q", kind=hitl.KIND_QUESTIONS) is not None
    finally:
        hitl.drop(CONVERSATION_ID)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"declined": False, "answers": []},
        {"declined": True, "answers": [{"selected": [0]}]},
        {"answers": [{"selected": ["0"]}]},
        {"answers": [{"selected": [True]}]},
        {"answers": [{"selected": [-1]}]},
        {"answers": [{"selected": [0, 1, 2, 3, 4, 5, 6]}]},
        {"answers": [{"other": "x" * 1_001}]},
        {"answers": [{"selected": [0], "comment": "x"}]},
        {"answers": [{"selected": [0]}] * 5},
    ],
)
def test_questions_answer_schema_422(body) -> None:
    session = FakeSession([[user_row()]])
    client, _ = make_client(session)
    assert client.post(ANSWER_PATH, json=body).status_code == 422


# ------------------------------------------------ flux refermé sans suite


def _turn(fake: FakeAssistantAI) -> _AssistantTurn:
    return _AssistantTurn(
        client=fake,
        db=None,
        conversation=_course_conversation(),
        refs=build_refs([], [], []),
        edit=None,
        provider="ollama",
        config=None,
        thread_id="t-turn",
        base_position=1,
        user_message_id=None,
        title_set=None,
    )


async def _chunks():
    for chunk in ("a", "b", "c"):
        yield chunk


@pytest.mark.anyio
async def test_release_on_close_drops_the_thread_of_an_aborted_turn() -> None:
    """Client parti en plein flux (generator refermé) : le thread du tour est
    purgé, une seule fois — même si le tour se clôt ensuite."""
    fake = FakeAssistantAI()
    sink = _turn(fake)
    stream = _release_on_close(_chunks(), sink)
    assert await anext(stream) == "a"
    await stream.aclose()
    assert fake.dropped_threads == ["t-turn"]
    sink._drop_thread()
    sink.release()
    assert fake.dropped_threads == ["t-turn"]


@pytest.mark.anyio
async def test_release_on_close_keeps_a_suspended_run() -> None:
    """Un tour figé (reprise enregistrée) garde son thread à la fermeture du
    flux ; un tour déjà clos (``done``/``failed``) n'est pas purgé deux fois."""
    fake = FakeAssistantAI()
    suspended = _turn(fake)
    suspended._suspended = True
    assert [chunk async for chunk in _release_on_close(_chunks(), suspended)] == ["a", "b", "c"]
    assert fake.dropped_threads == []

    closed = _turn(fake)
    closed._drop_thread()  # ``done``
    _ = [chunk async for chunk in _release_on_close(_chunks(), closed)]
    assert fake.dropped_threads == ["t-turn"]
