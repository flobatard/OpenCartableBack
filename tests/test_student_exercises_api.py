"""Routes du tuteur d'exercice élève (fil + flux SSE) — aucun réseau,
Postgres ni S3. Fakes de ``course_assistant_fakes.py`` (session FIFO, client
IA scripté) ; un faux client qui EXÉCUTE ``tool_executor`` sur les
``tool_call`` scriptés, pour que ``record_verdict`` alimente le tour.

Ordre FIFO du flux (docstring de ``sse_stream``) : [user] (router), [course]
(+ [link] si cours privé), [block], [turns], [user] (cascade), [blocks],
[resources], [modules] — puis insert du tour, puis UPDATE à la clôture.
"""

import uuid
from datetime import timedelta
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.sql.dml import Insert, Update

from app.core.ai import AIStreamEvent, AIToolCall, AIUsage
from app.student_exercises.service import MAX_TURNS_PER_QUESTION
from tests.course_assistant_fakes import (
    NOW,
    USER_ID,
    FakeAssistantAI,
    FakeSession,
    make_client,
    parse_sse,
    resource_row,
    user_row,
)

COURSE_ID = uuid.uuid4()
BLOCK_ID = uuid.uuid4()
TEXT_BLOCK_ID = uuid.uuid4()
Q1 = uuid.uuid4()
Q2 = uuid.uuid4()
SECRET = "corrigé-secret-4d2e"
SECRET_Q2 = "corrigé-q2-77aa"

BASE = f"/api/v1/student/courses/{COURSE_ID}/blocks/{BLOCK_ID}"
STREAM_PATH = f"{BASE}/questions/{Q1}/submissions/stream"


def _course_row(visibility="public"):
    return SimpleNamespace(
        id=COURSE_ID,
        owner_id=uuid.uuid4(),
        title="Géométrie",
        description=None,
        visibility=visibility,
        updated_at=NOW,
    )


def _link_row(**overrides):
    defaults = dict(
        course_id=COURSE_ID,
        token="tok",
        revoked=False,
        expires_at=NOW + timedelta(days=30),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _exercise_row():
    return SimpleNamespace(
        id=BLOCK_ID,
        course_id=COURSE_ID,
        position=1,
        type="exercise",
        title="Pythagore",
        description=None,
        content={
            "statement": "Triangle rectangle ABC.",
            "questions": [
                {"id": str(Q1), "statement": "Hypoténuse ?", "type": "free_text",
                 "expected_answer": SECRET},
                {"id": str(Q2), "statement": "Aire ?", "type": "free_text",
                 "expected_answer": SECRET_Q2},
            ],
        },
        resource_id=None,
        module_id=None,
    )


def _text_row():
    return SimpleNamespace(
        id=TEXT_BLOCK_ID,
        course_id=COURSE_ID,
        position=0,
        type="text",
        title="Cours",
        description=None,
        content={"markdown": "Le carré de l'hypoténuse…"},
        resource_id=None,
        module_id=None,
    )


def _turn_row(question_id=Q1, **overrides):
    defaults = dict(
        id=uuid.uuid4(),
        user_id=USER_ID,
        course_id=COURSE_ID,
        block_id=BLOCK_ID,
        question_id=question_id,
        kind="answer",
        content="5",
        feedback="Relis le cours.",
        verdict="incorrect",
        effort="insufficient",
        revealed=False,
        input_tokens=None,
        output_tokens=None,
        created_at=NOW,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeTutorAI(FakeAssistantAI):
    """Exécute ``tool_executor`` sur chaque ``tool_call`` scripté et émet le
    ``tool_result`` correspondant (le tour dépend du verdict enregistré)."""

    async def _gen(self):
        executor = self.calls[-1]["tool_executor"]
        for event in self.events:
            if self.mid_stream_error is not None and event.type == "done":
                raise self.mid_stream_error
            yield event
            if event.type == "tool_call":
                result = await executor(event.tool_call)
                yield AIStreamEvent(
                    type="tool_result",
                    delta=result.content,
                    tool_call=event.tool_call,
                    tool_result_error=result.is_error,
                )


def _verdict_call(verdict="incorrect", effort="insufficient", reveal=False):
    return AIStreamEvent(
        type="tool_call",
        tool_call=AIToolCall(
            id="call_v",
            name="record_verdict",
            arguments={"verdict": verdict, "effort": effort, "reveal": reveal},
        ),
    )


def _events(*, verdict_call=None, text="Relis [Cours](oc-block:B1)."):
    events = [AIStreamEvent(type="thinking", delta="hmm")]
    if verdict_call is not None:
        events.append(verdict_call)
    events.append(AIStreamEvent(type="token", delta=text))
    events.append(AIStreamEvent(type="done", usage=AIUsage(input_tokens=10, output_tokens=5)))
    return events


def _stream_session(*, course=None, link=None, turns=(), blocks=None):
    fifo = [[user_row()], [course or _course_row()]]
    if link is not None:
        fifo.append([link] if link != "missing" else [])
    fifo.extend(
        [
            [_exercise_row()],
            list(turns),
            [user_row()],
            list(blocks) if blocks is not None else [_text_row(), _exercise_row()],
            [resource_row()],
            [],
        ]
    )
    return FakeSession(fifo)


def _post(client, **kwargs):
    body = {"kind": "answer", "content": "Je pense 5"}
    body.update(kwargs.pop("body", {}))
    return client.post(kwargs.pop("path", STREAM_PATH), json=body, **kwargs)


def _inserted(session):
    for stmt, _ in session.executed:
        if isinstance(stmt, Insert) and stmt.table.name == "exercise_submissions":
            return stmt.compile().params
    return None


def _updated(session):
    for stmt, _ in session.executed:
        if isinstance(stmt, Update) and stmt.table.name == "exercise_submissions":
            return stmt.compile().params
    return None


# ---------------------------------------------------------------- auth / accès


def test_routes_require_token(client: TestClient) -> None:
    assert client.get(f"{BASE}/submissions").status_code == 401
    assert client.post(STREAM_PATH, json={"kind": "answer", "content": "x"}).status_code == 401


def test_stream_draft_course_is_404() -> None:
    session = FakeSession([[user_row()], [_course_row("draft")]])
    client, _ = make_client(session, FakeTutorAI())
    response = _post(client)
    assert response.status_code == 404
    assert response.json()["detail"] == "Cours introuvable"


def test_stream_private_course_requires_valid_token() -> None:
    session = FakeSession([[user_row()], [_course_row("private")], [_link_row(revoked=True)]])
    client, _ = make_client(session, FakeTutorAI())
    assert _post(client, path=f"{STREAM_PATH}?token=tok").status_code == 404

    session = _stream_session(course=_course_row("private"), link=_link_row())
    client, fake = make_client(session, FakeTutorAI(_events(verdict_call=_verdict_call())))
    response = _post(client, path=f"{STREAM_PATH}?token=tok")
    assert response.status_code == 200
    assert fake.calls


def test_stream_block_must_be_an_exercise() -> None:
    session = FakeSession([[user_row()], [_course_row()], []])
    client, _ = make_client(session, FakeTutorAI())
    response = _post(client)
    assert response.status_code == 404
    assert response.json()["detail"] == "Bloc introuvable"


def test_stream_unknown_question_is_404() -> None:
    session = FakeSession([[user_row()], [_course_row()], [_exercise_row()]])
    client, _ = make_client(session, FakeTutorAI())
    response = _post(client, path=f"{BASE}/questions/{uuid.uuid4()}/submissions/stream")
    assert response.status_code == 404
    assert response.json()["detail"] == "Question introuvable"


def test_stream_full_thread_is_422() -> None:
    turns = [_turn_row() for _ in range(MAX_TURNS_PER_QUESTION)]
    session = FakeSession([[user_row()], [_course_row()], [_exercise_row()], turns])
    client, _ = make_client(session, FakeTutorAI())
    assert _post(client).status_code == 422


def test_stream_payload_validation() -> None:
    session = _stream_session()
    client, _ = make_client(session, FakeTutorAI())
    assert _post(client, body={"kind": "hint"}).status_code == 422
    assert _post(client, body={"content": ""}).status_code == 422


def test_stream_quota_exhausted_is_429_before_stream(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "AI_PROVIDER", "ollama")
    fifo = [
        [user_row()],
        [_course_row()],
        [_exercise_row()],
        [],
        [user_row(ai_provider=None, ai_model=None)],
    ]
    session = FakeSession(fifo, upsert_rowcount=0)
    client, fake = make_client(session, FakeTutorAI())
    assert _post(client).status_code == 429
    assert not fake.calls


def test_stream_eager_error_refunds_and_no_insert() -> None:
    session = _stream_session()
    client, _ = make_client(
        session, FakeTutorAI(eager_error=HTTPException(status_code=422, detail="Config IA"))
    )
    response = _post(client)
    assert response.status_code == 422
    # La ligne du tour est insérée avant l'appel provider (durable) ; l'échec
    # eager la laisse sans feedback.
    assert _inserted(session) is not None
    assert _updated(session) is None


# ---------------------------------------------------------------- flux nominal


def test_stream_nominal_without_reveal() -> None:
    session = _stream_session(turns=[_turn_row()])
    fake = FakeTutorAI(_events(verdict_call=_verdict_call()))
    client, _ = make_client(session, fake)
    response = _post(client)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    names = [name for name, _ in events]
    assert names == ["thinking", "tool_call", "tool_result", "token", "done"]

    # Le modèle a reçu : system (contexte tuteur), le fil, le tour courant.
    [call] = fake.calls
    system, *rest = call["messages"]
    assert system.role == "system"
    assert SECRET in system.content
    assert SECRET_Q2 not in system.content
    assert "Réponse attendue" not in system.content
    assert "## Corrigé confidentiel de la question 1" in system.content
    assert [m.role for m in rest] == ["user", "assistant", "user"]
    assert rest[-1].content == "Réponse de l'élève :\n\nJe pense 5"
    assert any(spec.name == "record_verdict" for spec in call["tools"])
    assert call["user_id"] == "prof-123"

    # Citation réécrite en UUID au fil du flux.
    token_events = [data for name, data in events if name == "token"]
    assert token_events[0]["delta"] == f"Relis [Cours](oc-block:{TEXT_BLOCK_ID})."

    done = events[-1][1]
    assert done["verdict"] == "incorrect"
    assert done["effort"] == "insufficient"
    assert done["revealed"] is False
    assert done["expected_answer"] is None
    assert done["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert uuid.UUID(done["submission_id"])

    inserted = _inserted(session)
    assert inserted["kind"] == "answer"
    assert inserted["content"] == "Je pense 5"
    assert inserted["question_id"] == Q1
    assert inserted["user_id"] == USER_ID
    updated = _updated(session)
    assert updated["feedback"] == f"Relis [Cours](oc-block:{TEXT_BLOCK_ID})."
    assert updated["verdict"] == "incorrect"
    assert updated["revealed"] is False
    assert updated["input_tokens"] == 10
    assert session.commits >= 2


def test_stream_reveals_expected_answer_when_correct() -> None:
    session = _stream_session()
    fake = FakeTutorAI(
        _events(verdict_call=_verdict_call("correct", "sufficient", True), text="Bravo !")
    )
    client, _ = make_client(session, fake)
    events = parse_sse(_post(client).text)
    done = events[-1][1]
    assert done["revealed"] is True
    assert done["expected_answer"] == SECRET
    assert _updated(session)["revealed"] is True
    # Le résultat du tool a informé le modèle de la révélation.
    [tool_result] = [data for name, data in events if name == "tool_result"]
    assert tool_result["is_error"] is False
    assert "affichera le corrigé" in tool_result["excerpt"]


def test_stream_reveal_is_guarded_server_side() -> None:
    session = _stream_session()
    fake = FakeTutorAI(_events(verdict_call=_verdict_call("incorrect", "insufficient", True)))
    client, _ = make_client(session, fake)
    done = parse_sse(_post(client).text)[-1][1]
    assert done["revealed"] is False
    assert done["expected_answer"] is None


def test_stream_without_verdict_call_is_safe() -> None:
    session = _stream_session()
    client, _ = make_client(session, FakeTutorAI(_events(text="Réfléchis encore.")))
    done = parse_sse(_post(client).text)[-1][1]
    assert done["verdict"] == "none"
    assert done["effort"] is None
    assert done["revealed"] is False
    assert done["expected_answer"] is None
    assert _updated(session)["verdict"] == "none"


def test_stream_message_kind() -> None:
    session = _stream_session()
    fake = FakeTutorAI(_events(verdict_call=_verdict_call("none", "insufficient", False)))
    client, _ = make_client(session, fake)
    response = _post(client, body={"kind": "message", "content": "Donne-moi la réponse"})
    assert response.status_code == 200
    assert fake.calls[0]["messages"][-1].content.startswith("Message de l'élève :")
    assert _inserted(session)["kind"] == "message"


def test_stream_mid_stream_error_persists_partial() -> None:
    session = _stream_session()
    fake = FakeTutorAI(
        _events(verdict_call=_verdict_call(), text="Début"),
        mid_stream_error=HTTPException(status_code=503, detail="Fournisseur IA injoignable"),
    )
    client, _ = make_client(session, fake)
    events = parse_sse(_post(client).text)
    assert events[-1] == ("error", {"status": 503, "detail": "Fournisseur IA injoignable"})
    assert "done" not in [name for name, _ in events]
    assert _updated(session)["feedback"] == "Début"


# ---------------------------------------------------------------- fil (GET)


def test_list_submissions_groups_by_question_and_reveals_once() -> None:
    turns = [
        _turn_row(),
        _turn_row(kind="message", content="Aide", feedback=None, verdict=None, effort=None),
        _turn_row(question_id=Q2, verdict="correct", effort="sufficient", revealed=True),
    ]
    session = FakeSession([[user_row()], [_course_row()], [_exercise_row()], turns])
    client, _ = make_client(session)
    response = client.get(f"{BASE}/submissions")
    assert response.status_code == 200
    questions = response.json()["questions"]
    assert set(questions) == {str(Q1), str(Q2)}
    q1 = questions[str(Q1)]
    assert [t["kind"] for t in q1["turns"]] == ["answer", "message"]
    assert q1["turns"][1]["feedback"] is None
    assert q1["revealed_answer"] is None
    q2 = questions[str(Q2)]
    assert q2["turns"][0]["revealed"] is True
    assert q2["revealed_answer"] == SECRET_Q2


def test_list_submissions_empty_and_404s() -> None:
    session = FakeSession([[user_row()], [_course_row()], [_exercise_row()], []])
    client, _ = make_client(session)
    assert client.get(f"{BASE}/submissions").json() == {"questions": {}}

    session = FakeSession([[user_row()], [_course_row("private")]])
    client, _ = make_client(session)
    assert client.get(f"{BASE}/submissions").status_code == 404
