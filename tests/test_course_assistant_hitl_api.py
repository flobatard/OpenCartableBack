"""Tests des routes de l'assistant de cours pour les **contextes d'édition**
(conversations rattachées à un bloc) et le **flux HITL** — interrupt à la
proposition, reprise par la route de décision, abandon. Fakes partagés dans
``course_assistant_fakes.py`` (contrats FIFO documentés là).
"""

import uuid

from app.core.ai import AIStreamEvent, AIToolCall
from app.course_assistant import hitl
from tests.course_assistant_fakes import (
    BASE,
    BLOCK_ID,
    CONVERSATION_ID,
    NOW,
    RESOURCE_ID,
    STREAM_PATH,
    FakeAssistantAI,
    FakeSession,
    block_row,
    conversation_row,
    course_row,
    inserted_message_rows,
    make_client,
    message_row,
    parse_sse,
    resume_session,
    stream_session,
    user_row,
)

DECISION_PATH = f"{BASE}/conversations/{CONVERSATION_ID}/proposals/call_p/decision"

Q1, Q2, Q3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
EXERCISE_TOOLS = {
    "propose_statement_edit",
    "propose_question_edit",
    "propose_question_add",
    "propose_question_delete",
}


def _question(qid, statement, expected=""):
    return {"id": str(qid), "statement": statement, "type": "free_text",
            "expected_answer": expected}


def _exercise_row(questions=None):
    return block_row(
        type="exercise",
        title="Exercice",
        content={
            "statement": "Soit $x$ un réel.",
            "questions": questions
            if questions is not None
            else [_question(Q1, "Calculer $x^2$.", "x²"), _question(Q2, "Conclure.")],
        },
    )


# ------------------------------------------------- conversations d'un bloc


def test_create_conversation_block_text() -> None:
    """FIFO : [user], [course], [block] (scopé), puis insert à RETURNING."""
    session = FakeSession([[user_row()], [course_row()], [block_row()], [(NOW, NOW)]])
    client, _ = make_client(session)
    response = client.post(
        f"{BASE}/conversations",
        json={"context": "block_text", "block_id": str(BLOCK_ID)},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["context"] == "block_text"
    assert payload["block_id"] == str(BLOCK_ID)
    assert payload["module_id"] is None


def test_create_conversation_block_text_unknown_block_404() -> None:
    session = FakeSession([[user_row()], [course_row()], []])
    client, _ = make_client(session)
    response = client.post(
        f"{BASE}/conversations",
        json={"context": "block_text", "block_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Bloc introuvable"


def test_create_conversation_block_text_non_text_block_422() -> None:
    session = FakeSession([[user_row()], [course_row()], [block_row(type="exercise")]])
    client, _ = make_client(session)
    response = client.post(
        f"{BASE}/conversations",
        json={"context": "block_text", "block_id": str(BLOCK_ID)},
    )
    assert response.status_code == 422
    assert "blocs texte" in response.json()["detail"]


def test_create_conversation_block_text_requires_block_id() -> None:
    session = FakeSession([[user_row()]])
    client, _ = make_client(session)
    response = client.post(f"{BASE}/conversations", json={"context": "block_text"})
    assert response.status_code == 422


def test_create_conversation_block_exercise() -> None:
    session = FakeSession([[user_row()], [course_row()], [_exercise_row()], [(NOW, NOW)]])
    client, _ = make_client(session)
    response = client.post(
        f"{BASE}/conversations",
        json={"context": "block_exercise", "block_id": str(BLOCK_ID)},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["context"] == "block_exercise"
    assert payload["block_id"] == str(BLOCK_ID)


def test_create_conversation_block_exercise_on_text_block_422() -> None:
    session = FakeSession([[user_row()], [course_row()], [block_row()]])
    client, _ = make_client(session)
    response = client.post(
        f"{BASE}/conversations",
        json={"context": "block_exercise", "block_id": str(BLOCK_ID)},
    )
    assert response.status_code == 422
    assert "blocs exercice" in response.json()["detail"]


def test_create_conversation_block_exercise_requires_block_id() -> None:
    session = FakeSession([[user_row()]])
    client, _ = make_client(session)
    response = client.post(f"{BASE}/conversations", json={"context": "block_exercise"})
    assert response.status_code == 422


def test_list_conversations_filters_by_block() -> None:
    conv = conversation_row(context="block_text", block_id=BLOCK_ID)
    session = FakeSession([[user_row()], [course_row()], [conv]])
    client, _ = make_client(session)
    response = client.get(
        f"{BASE}/conversations", params={"context": "block_text", "block_id": str(BLOCK_ID)}
    )
    assert response.status_code == 200
    [payload] = response.json()
    assert payload["context"] == "block_text"
    assert payload["block_id"] == str(BLOCK_ID)
    # Le filtre bloc est bien dans la requête (en plus du filtre contexte).
    list_stmt = session.executed[-1][0]
    assert "ai_conversations.block_id =" in str(list_stmt)


# ------------------------------------------------------- flux block_text


def test_stream_block_text_context() -> None:
    """Contexte block_text : prompt d'édition + tool ``propose_block_edit``,
    args de la proposition relayés COMPLETS sur le flux et persistés (aucun
    événement SSE dédié — décision actée), avec les références courtes des
    liens de contenu réécrites en UUID (le markdown reçu est applicable)."""
    proposal = "# Version proposée\n\nVoir le [sujet](oc-resource:R1), réécrit."
    rewritten = f"# Version proposée\n\nVoir le [sujet](oc-resource:{RESOURCE_ID}), réécrit."
    events = [
        AIStreamEvent(
            type="tool_call",
            tool_call=AIToolCall(
                id="call_p",
                name="propose_block_edit",
                arguments={"new_markdown": proposal, "summary": "Réécriture clarifiée"},
            ),
        ),
        AIStreamEvent(
            type="tool_result",
            delta="Le professeur a ACCEPTÉ la proposition et l'a appliquée au bloc.",
            tool_call=AIToolCall(id="call_p", name="propose_block_edit"),
            tool_result_error=False,
        ),
        AIStreamEvent(type="token", delta="Appliqué, très bien."),
        AIStreamEvent(type="done", usage=None),
    ]
    conv = conversation_row(context="block_text", block_id=BLOCK_ID, title="T")
    session = stream_session(conversation=conv)
    client, fake = make_client(session, FakeAssistantAI(events=events))
    response = client.post(STREAM_PATH, json={"content": "Réécris ce bloc"})
    assert response.status_code == 200

    events_out = parse_sse(response.text)
    assert [k for k, _ in events_out] == ["tool_call", "tool_result", "token", "done"]
    assert events_out[0][1]["args"]["new_markdown"] == rewritten
    assert events_out[0][1]["args"]["summary"] == "Réécriture clarifiée"

    # Persistance : la proposition (réécrite) vit dans les tool_calls du
    # segment assistant, la décision dans le contenu du tour tool.
    rows = inserted_message_rows(session)
    assert rows[0]["tool_calls"][0]["arguments"]["new_markdown"] == rewritten
    assert "ACCEPTÉ" in rows[1]["content"]

    # Prompt et tools du contexte d'édition ; run checkpointé (thread), purgé
    # au done.
    [call] = fake.calls
    system = call["messages"][0].content
    assert "## Bloc en cours d'édition" in system
    assert "propose_block_edit" in system
    assert "```mermaid" in system  # syntaxes d'édition déclarées
    assert "propose_block_edit" in {t.name for t in call["tools"]}
    assert call["thread_id"] is not None
    assert fake.dropped_threads == [call["thread_id"]]


def test_stream_block_text_interrupt_registers_resume() -> None:
    """Proposition HITL : le flux émet ``interrupt`` et se ferme SANS done, le
    tour partiel (segment assistant porteur du tool_call) est persisté et la
    reprise est enregistrée au registre in-process."""
    events = [
        AIStreamEvent(type="token", delta="Je propose ceci. "),
        AIStreamEvent(
            type="tool_call",
            tool_call=AIToolCall(
                id="call_p",
                name="propose_block_edit",
                arguments={"new_markdown": "# Proposé", "summary": "Réécriture"},
            ),
        ),
        AIStreamEvent(type="interrupt", interrupt_value={"tool_call_id": "call_p"}),
    ]
    conv = conversation_row(context="block_text", block_id=BLOCK_ID, title="T")
    session = stream_session(conversation=conv)
    client, fake = make_client(session, FakeAssistantAI(events=events))
    try:
        response = client.post(STREAM_PATH, json={"content": "Réécris ce bloc"})
        assert response.status_code == 200

        events_out = parse_sse(response.text)
        assert [k for k, _ in events_out] == ["token", "tool_call", "interrupt"]
        interrupt = events_out[-1][1]
        assert interrupt["tool_call_id"] == "call_p"
        assert len(interrupt["message_ids"]) == 1

        # Tour PARTIEL persisté : le segment assistant (texte + tool_call),
        # aucun tour tool — un abandon restera un round incomplet, replié.
        rows = inserted_message_rows(session)
        assert [r["role"] for r in rows] == ["assistant"]
        assert rows[0]["tool_calls"][0]["id"] == "call_p"

        # Reprise enregistrée : thread du run, config et provider du tour.
        [call] = fake.calls
        pending = hitl.take(CONVERSATION_ID, "call_p")
        assert pending is not None
        assert pending.thread_id == call["thread_id"]
        assert pending.provider == "ollama"
        # Le thread n'est PAS purgé (le run attend sa reprise).
        assert fake.dropped_threads == []
    finally:
        hitl.drop(CONVERSATION_ID)


def test_stream_block_text_missing_block_404() -> None:
    """Bloc de la conversation absent de l'instantané : 404 défensif AVANT
    l'insert du message user (la FK CASCADE rend le cas théorique)."""
    conv = conversation_row(context="block_text", block_id=uuid.uuid4(), title="T")
    session = stream_session(conversation=conv)
    client, _ = make_client(session)
    response = client.post(STREAM_PATH, json={"content": "Salut"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Bloc introuvable"
    assert inserted_message_rows(session) is None


def test_new_message_abandons_a_pending_resume() -> None:
    """Envoyer un nouveau message alors qu'une proposition attendait : la
    reprise est abandonnée (registre vidé, thread purgé)."""
    hitl.register(
        CONVERSATION_ID,
        hitl.PendingProposal(
            thread_id="t-stale", tool_call_id="call_old", provider="ollama", config=None
        ),
    )
    conv = conversation_row(context="block_text", block_id=BLOCK_ID, title="T")
    session = stream_session(conversation=conv)
    client, fake = make_client(
        session, FakeAssistantAI(events=[AIStreamEvent(type="done", usage=None)])
    )
    response = client.post(STREAM_PATH, json={"content": "Autre chose"})
    assert response.status_code == 200
    assert "t-stale" in fake.dropped_threads
    assert hitl.take(CONVERSATION_ID, "call_old") is None


# --------------------------------------------------- route de décision


def test_proposal_decision_resumes_the_run() -> None:
    """La décision REPREND le run figé : reprise consommée au registre, flux
    SSE de la suite du tour (tool_result → done), positions continuant le tour
    partiel persisté, thread purgé au done."""
    hitl.register(
        CONVERSATION_ID,
        hitl.PendingProposal(
            thread_id="t-run", tool_call_id="call_p", provider="ollama", config=None
        ),
    )
    events = [
        AIStreamEvent(
            type="tool_result",
            delta="Le professeur a ACCEPTÉ la proposition et l'a appliquée au bloc. "
            "Son commentaire : Très bien",
            tool_call=AIToolCall(id="call_p", name="propose_block_edit"),
            tool_result_error=False,
        ),
        AIStreamEvent(type="token", delta="Parfait, c'est appliqué."),
        AIStreamEvent(type="done", usage=None),
    ]
    # Tour partiel déjà persisté : user (0), assistant à tool_call (1).
    existing = [
        message_row(0, role="user"),
        message_row(1, role="assistant", tool_calls=[{"id": "call_p"}]),
    ]
    session = resume_session(messages=existing)
    client, fake = make_client(session, FakeAssistantAI(events=events))
    response = client.post(DECISION_PATH, json={"accepted": True, "comment": "Très bien"})
    assert response.status_code == 200

    events_out = parse_sse(response.text)
    assert [k for k, _ in events_out] == ["tool_result", "token", "done"]
    assert events_out[-1][1]["user_message_id"] is None  # pas de nouveau message user

    # Reprise : même thread, décision en valeur de resume, config du tour,
    # graphe rebâti avec les tools du contexte (dont la proposition).
    [call] = fake.calls
    assert call["thread_id"] == "t-run"
    assert call["resume"] == {"accepted": True, "comment": "Très bien"}
    assert call["messages"] == []  # l'état vit au checkpoint
    assert "propose_block_edit" in {t.name for t in call["tools"]}
    assert fake.dropped_threads == ["t-run"]  # purgé au done

    # Suite du tour persistée À LA SUITE : tour tool (2) + segment final (3).
    rows = inserted_message_rows(session)
    assert [(r["role"], r["position"]) for r in rows] == [("tool", 2), ("assistant", 3)]
    assert rows[0]["tool_call_id"] == "call_p"
    assert "ACCEPTÉ" in rows[0]["content"]

    # Registre consommé : une seconde décision → 404.
    assert hitl.take(CONVERSATION_ID, "call_p") is None


def test_proposal_decision_without_pending_404() -> None:
    conv = conversation_row(context="block_text", block_id=BLOCK_ID, title="T")
    session = FakeSession([[user_row()], [course_row()], [conv]])
    client, _ = make_client(session)
    response = client.post(DECISION_PATH, json={"accepted": False})
    assert response.status_code == 404
    assert "proposition" in response.json()["detail"].casefold()


def test_proposal_decision_on_course_context_404_keeps_registry() -> None:
    """Défensif : une conversation sans contexte d'édition n'a jamais de reprise
    — 404 sans même consulter (ni consommer) le registre."""
    hitl.register(
        CONVERSATION_ID,
        hitl.PendingProposal(
            thread_id="t-x", tool_call_id="call_p", provider="ollama", config=None
        ),
    )
    try:
        session = FakeSession([[user_row()], [course_row()], [conversation_row(title="T")]])
        client, _ = make_client(session)
        response = client.post(DECISION_PATH, json={"accepted": True})
        assert response.status_code == 404
        assert hitl.take(CONVERSATION_ID, "call_p") is not None
    finally:
        hitl.drop(CONVERSATION_ID)


def test_proposal_decision_comment_too_long_422() -> None:
    session = FakeSession([[user_row()]])
    client, _ = make_client(session)
    response = client.post(DECISION_PATH, json={"accepted": True, "comment": "x" * 2_001})
    assert response.status_code == 422


# ---------------------------------------------------- flux block_exercise


def test_stream_block_exercise_context() -> None:
    """Contexte block_exercise : prompt d'édition d'exercice, questions du
    bloc édité numérotées Q1…, quatre tools de proposition ; les args d'une
    proposition sont réécrits à l'émission (id de la question résolu, liens
    de contenu en UUID) et persistés sous cette forme ; l'interrupt enregistre
    la numérotation du tour au registre."""
    events = [
        AIStreamEvent(
            type="tool_call",
            tool_call=AIToolCall(
                id="call_p",
                name="propose_question_edit",
                arguments={
                    "question_ref": "Q2",
                    "statement": "Conclure avec la [figure](oc-resource:R1).",
                    "expected_answer": "42",
                    "summary": "Ajout du corrigé",
                },
            ),
        ),
        AIStreamEvent(type="interrupt", interrupt_value={"tool_call_id": "call_p"}),
    ]
    conv = conversation_row(context="block_exercise", block_id=BLOCK_ID, title="T")
    session = stream_session(conversation=conv, blocks=[_exercise_row()])
    client, fake = make_client(session, FakeAssistantAI(events=events))
    try:
        response = client.post(STREAM_PATH, json={"content": "Complète le corrigé de la 2"})
        assert response.status_code == 200

        events_out = parse_sse(response.text)
        assert [k for k, _ in events_out] == ["tool_call", "interrupt"]
        args = events_out[0][1]["args"]
        assert args["question_id"] == str(Q2)
        assert args["question_ref"] == "Q2"  # conservée pour le replay
        assert args["statement"] == f"Conclure avec la [figure](oc-resource:{RESOURCE_ID})."
        assert args["expected_answer"] == "42"
        assert args["summary"] == "Ajout du corrigé"
        rows = inserted_message_rows(session)
        assert rows[0]["tool_calls"][0]["arguments"] == args  # persisté réécrit

        [call] = fake.calls
        system = call["messages"][0].content
        assert "qui édite un exercice de son cours" in system
        assert "## Bloc en cours d'édition" in system
        assert "**Question 1** (ref: Q1) : Calculer $x^2$." in system
        assert "(ref: Q2)" in system
        assert str(Q1) not in system and str(Q2) not in system
        assert "propose_question_edit" in system and "propose_block_edit" not in system
        specs = {t.name: t for t in call["tools"]}
        assert EXERCISE_TOOLS <= set(specs)
        assert "propose_block_edit" not in specs
        assert specs["propose_question_edit"].parameters["properties"]["question_ref"]["enum"] == [
            "Q1",
            "Q2",
        ]
        assert call["thread_id"] is not None
        assert fake.dropped_threads == []

        pending = hitl.take(CONVERSATION_ID, "call_p")
        assert pending is not None
        assert pending.question_refs == {"Q1": str(Q1), "Q2": str(Q2)}
    finally:
        hitl.drop(CONVERSATION_ID)


def test_proposal_decision_resumes_exercise_run_with_stable_question_refs() -> None:
    """Reprise d'un run exercice : graphe rebâti avec les tools de CE contexte
    (verrou contre un ``include_propose`` codé en dur) et numérotation du tour
    rejouée — Q2 supprimée entre-temps n'est pas réattribuée, la question
    ajoutée reçoit Q4."""
    hitl.register(
        CONVERSATION_ID,
        hitl.PendingProposal(
            thread_id="t-ex",
            tool_call_id="call_p",
            provider="ollama",
            config=None,
            question_refs={"Q1": str(Q1), "Q2": str(Q2), "Q3": str(Q3)},
        ),
    )
    q4 = uuid.uuid4()
    events = [
        AIStreamEvent(
            type="tool_result",
            delta="Le professeur a ACCEPTÉ la proposition : la question a été supprimée.",
            tool_call=AIToolCall(id="call_p", name="propose_question_delete"),
            tool_result_error=False,
        ),
        AIStreamEvent(type="token", delta="Question supprimée."),
        AIStreamEvent(type="done", usage=None),
    ]
    existing = [
        message_row(0, role="user"),
        message_row(1, role="assistant", tool_calls=[{"id": "call_p"}]),
    ]
    conv = conversation_row(context="block_exercise", block_id=BLOCK_ID, title="T")
    reloaded = _exercise_row(
        questions=[_question(Q1, "a"), _question(Q3, "c"), _question(q4, "nouvelle")]
    )
    session = resume_session(messages=existing, conversation=conv, blocks=[reloaded])
    client, fake = make_client(session, FakeAssistantAI(events=events))
    response = client.post(DECISION_PATH, json={"accepted": True})
    assert response.status_code == 200
    assert [k for k, _ in parse_sse(response.text)] == ["tool_result", "token", "done"]

    [call] = fake.calls
    assert call["thread_id"] == "t-ex"
    assert call["resume"] == {"accepted": True, "comment": None}
    specs = {t.name: t for t in call["tools"]}
    assert EXERCISE_TOOLS <= set(specs)
    assert "propose_block_edit" not in specs
    assert specs["propose_question_delete"].parameters["properties"]["question_ref"]["enum"] == [
        "Q1",
        "Q3",
        "Q4",
    ]
    assert fake.dropped_threads == ["t-ex"]
    rows = inserted_message_rows(session)
    assert [(r["role"], r["position"]) for r in rows] == [("tool", 2), ("assistant", 3)]
