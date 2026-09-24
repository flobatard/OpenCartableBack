"""Le tour d'assistant avec des pièces jointes : rattachement au message,
refus des pièces illégitimes, et reprise HITL.

Aucun réseau, Postgres ni S3 — fakes partagés de ``course_assistant_fakes.py``.
Le point gardé le plus important est le dernier : une reprise DOIT exposer les
mêmes tools que l'aller, sinon l'état checkpointé référence un tool absent du
graphe.
"""

import uuid

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlalchemy.sql.selectable import Select

from app.core.ai import AIStreamEvent, AIToolCall
from app.core.config import settings
from app.course_assistant import hitl
from app.course_assistant.attachments import MAX_ATTACHMENTS_PER_CONVERSATION
from tests.course_assistant_fakes import (
    ATTACHMENT_ID,
    BASE,
    CONVERSATION_ID,
    STREAM_PATH,
    FakeAssistantAI,
    attachment_row,
    conversation_row,
    make_client,
    message_row,
    resume_session,
    stream_session,
    updates_on,
)
from tests.fakes import parse_sse

_ANSWER = [AIStreamEvent(type="token", delta="Vu."), AIStreamEvent(type="done")]


def _unsent(**overrides):
    """Une pièce préparée mais pas encore envoyée — le seul état joignable."""
    defaults = dict(conversation_id=None, message_id=None)
    defaults.update(overrides)
    return attachment_row(**defaults)


def _post(client, ids, content="Que vois-tu ?"):
    return client.post(
        STREAM_PATH, json={"content": content, "attachment_ids": [str(i) for i in ids]}
    )


# ─────────────────────────────────────────────
# Rattachement
# ─────────────────────────────────────────────


def test_attachment_is_bound_to_the_inserted_message():
    session = stream_session(attachments=[_unsent()])
    client, fake = make_client(session, FakeAssistantAI(events=_ANSWER))

    response = _post(client, [ATTACHMENT_ID])
    assert response.status_code == 200

    # L'UPDATE de rattachement suit l'insert du message user (contrainte FK) et
    # vit dans la même transaction.
    [(stmt, _)] = [
        (s, p) for s, p in session.executed if isinstance(s, Update) and "ai_attachments" in str(s)
    ]
    values = stmt.compile().params
    assert values["message_id"] is not None
    assert values["conversation_id"] is not None


def test_a_turn_without_attachments_issues_no_update():
    """L'execute de rattachement est la seule irrégularité du contrat FIFO :
    il n'a lieu QUE si le message porte des pièces jointes."""
    session = stream_session()
    client, _ = make_client(session, FakeAssistantAI(events=_ANSWER))

    assert client.post(STREAM_PATH, json={"content": "Salut"}).status_code == 200
    assert updates_on(session, "ai_attachments") == []


def test_the_model_sees_the_attachment_and_its_tool():
    session = stream_session(attachments=[_unsent(original_name="tableau.png")])
    client, fake = make_client(session, FakeAssistantAI(events=_ANSWER))

    _post(client, [ATTACHMENT_ID])
    [call] = fake.calls

    turn = call["messages"][-1].content
    assert "## Pièces jointes de la conversation" in turn
    assert "tableau.png (ref: A1, image," in turn
    assert "jointe à CE message" in turn
    # La section vient APRÈS le sommaire, juste avant la demande du professeur.
    assert turn.index("## Pièces jointes") > turn.index("## Sommaire du cours")
    assert "read_attachment" in {t.name for t in call["tools"]}


def test_an_attachment_of_a_previous_message_stays_readable():
    """« Attachée à la conversation » : le tour suivant la voit encore, sans
    que le professeur ait à la joindre de nouveau."""
    session = stream_session(attachments=[attachment_row(original_name="ancien.pdf", kind="pdf")])
    client, fake = make_client(session, FakeAssistantAI(events=_ANSWER))

    assert client.post(STREAM_PATH, json={"content": "Et la suite ?"}).status_code == 200
    [call] = fake.calls
    turn = call["messages"][-1].content
    assert "ancien.pdf (ref: A1, PDF," in turn
    assert "jointe à un message précédent" in turn
    assert "read_attachment" in {t.name for t in call["tools"]}


# ─────────────────────────────────────────────
# Refus
# ─────────────────────────────────────────────


def test_an_unknown_attachment_refuses_the_turn_and_refunds(monkeypatch):
    """Jamais un tour silencieusement amputé de son fichier : 422 AVANT le flux,
    avec remboursement du ticket de quota déjà réservé."""
    monkeypatch.setattr(settings, "AI_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "AI_MODEL", "llama3.2")
    session = stream_session(config=None, attachments=[])
    client, fake = make_client(session, FakeAssistantAI(events=_ANSWER))

    response = _post(client, [uuid.uuid4()])
    assert response.status_code == 422
    assert response.json()["detail"] == "Pièce jointe introuvable ou déjà envoyée"
    assert fake.calls == []  # le provider n'a jamais été appelé
    assert any(isinstance(stmt, Update) for stmt, _ in session.executed)  # remboursement


def test_only_unbound_confirmed_attachments_of_the_owner_are_candidates():
    """Le filtre qui empêche de joindre la pièce d'un autre, ou de rejouer une
    pièce déjà envoyée, vit dans le WHERE — testé sur le SQL compilé (motif des
    tests de purge), la fausse session ne filtrant rien."""
    session = stream_session(attachments=[_unsent()])
    client, _ = make_client(session, FakeAssistantAI(events=_ANSWER))
    _post(client, [ATTACHMENT_ID])

    [stmt] = [
        s
        for s, _ in session.executed
        if isinstance(s, Select) and "ai_attachments" in str(s)
    ]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ai_attachments.owner_id =" in sql
    assert "ai_attachments.course_id =" in sql
    assert "ai_attachments.conversation_id IS NULL" in sql
    assert "ai_attachments.status =" in sql


def test_duplicate_attachment_ids_are_rejected_by_the_schema():
    session = stream_session()
    client, _ = make_client(session, FakeAssistantAI(events=_ANSWER))
    assert _post(client, [ATTACHMENT_ID, ATTACHMENT_ID]).status_code == 422


def test_too_many_attachments_in_one_message_is_rejected():
    session = stream_session()
    client, _ = make_client(session, FakeAssistantAI(events=_ANSWER))
    assert _post(client, [uuid.uuid4() for _ in range(6)]).status_code == 422


def test_a_saturated_conversation_is_refused():
    crowd = [attachment_row(id=uuid.uuid4()) for _ in range(MAX_ATTACHMENTS_PER_CONVERSATION + 1)]
    session = stream_session(config=None, attachments=crowd)
    client, _ = make_client(session, FakeAssistantAI(events=_ANSWER))

    response = client.post(STREAM_PATH, json={"content": "Salut"})
    assert response.status_code == 422
    assert response.json()["detail"] == "Trop de pièces jointes dans cette conversation"


def test_a_message_without_text_is_still_refused():
    """Décision produit : un texte reste obligatoire, même avec un fichier."""
    session = stream_session()
    client, _ = make_client(session, FakeAssistantAI(events=_ANSWER))
    assert _post(client, [ATTACHMENT_ID], content="").status_code == 422


# ─────────────────────────────────────────────
# Le flux reste lisible
# ─────────────────────────────────────────────


def test_the_sse_contract_is_unchanged():
    """Aucun événement ni champ nouveau : le front sait ce qu'il a envoyé."""
    session = stream_session(attachments=[_unsent()])
    client, _ = make_client(session, FakeAssistantAI(events=_ANSWER))

    response = _post(client, [ATTACHMENT_ID])
    events = parse_sse(response.text)
    assert [name for name, _ in events] == ["token", "done"]
    assert "attachments" not in events[-1][1]


# ─────────────────────────────────────────────
# Reprise HITL
# ─────────────────────────────────────────────


def test_a_resume_exposes_the_same_tools_as_the_outward_turn():
    """Contrainte DURE de ``stream_agent`` : le graphe d'une reprise doit être
    bâti avec les MÊMES tools. Sans rechargement des pièces jointes,
    ``read_attachment`` disparaîtrait du ToolNode alors que l'état checkpointé
    le référence."""
    hitl.register(
        CONVERSATION_ID,
        hitl.PendingInterrupt(
            thread_id="t-run",
            tool_call_id="call_q",
            provider="ollama",
            config=None,
            kind=hitl.KIND_QUESTIONS,
            answer_shape=[{"multi_select": False, "options": 2}],
        ),
    )
    try:
        session = resume_session(
            messages=[
                message_row(0, role="user"),
                message_row(
                    1, role="assistant", tool_calls=[{"id": "call_q", "name": "ask_questions"}]
                ),
            ],
            conversation=conversation_row(title="T"),
            attachments=[attachment_row()],
        )
        client, fake = make_client(
            session,
            FakeAssistantAI(
                events=[
                    AIStreamEvent(
                        type="tool_result",
                        delta="Réponse du professeur",
                        tool_call=AIToolCall(id="call_q", name="ask_questions"),
                        tool_result_error=False,
                    ),
                    AIStreamEvent(type="done"),
                ]
            ),
        )
        response = client.post(
            f"{BASE}/conversations/{CONVERSATION_ID}/questions/call_q/answer",
            json={"answers": [{"selected": [0]}]},
        )
        assert response.status_code == 200

        [call] = fake.calls
        assert "read_attachment" in {t.name for t in call["tools"]}
        spec = next(t for t in call["tools"] if t.name == "read_attachment")
        # Identique au bit près : une pièce envoyée étant indélébile et le tri
        # stable, la numérotation A… ne bouge pas d'un tour à sa reprise.
        assert spec.parameters["properties"]["attachment_ref"]["enum"] == ["A1"]
    finally:
        hitl.drop(CONVERSATION_ID)
