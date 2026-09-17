"""Tests des routes de l'assistant pour l'**édition globale par délégation**
(contexte ``course`` à ``allow_edit``) : tools et prompt du tour, driver du
flux — sous-assistant lancé dans le flux, ses événements tagués ``agent``, sa
proposition enregistrée avec le lien vers le parent figé —, reprise du
sous-assistant puis de l'assistant par la route de décision (compte rendu en
valeur de reprise), redélégation, plafond, questions du sous-assistant,
abandon et purge des deux threads, erreur eager d'un run enchaîné. Fakes
partagés dans ``course_assistant_fakes.py`` (contrats FIFO documentés là —
inchangés : aucun rechargement d'instantané dans un flux).
"""

import uuid

import pytest
from fastapi import HTTPException

from app.core.ai import AIStreamEvent, AIToolCall, AIUsage
from app.course_assistant import hitl
from app.course_assistant.context import build_refs
from app.course_assistant.delegation import (
    DELEGATED_NOTE,
    DELEGATIONS_EXCEEDED_NOTICE,
    INTERRUPTED_TEXT,
    MAX_DELEGATIONS_PER_TURN,
    RECAP_NO_PROPOSAL,
)
from app.course_assistant.editing.block_text import BLOCK_TEXT
from app.course_assistant.editing.module import MODULE
from app.course_assistant.prompts import COURSE_EDITING_SYSTEM_PROMPT, COURSE_SYSTEM_PROMPT
from app.course_assistant.streaming import _AgentRun, _AssistantTurn, _release_on_close
from tests.course_assistant_fakes import (
    BASE,
    BLOCK_ID,
    CONVERSATION_ID,
    MODULE_ID,
    RESOURCE_ID,
    STREAM_PATH,
    FakeAssistantAI,
    block_row,
    conversation_row,
    course_row,
    delegation_interrupt_value,
    inserted_message_rows,
    make_client,
    message_row,
    module_row,
    proposal_interrupt_value,
    questions_interrupt_value,
    resource_row,
    resume_session,
    stream_session,
    user_row,
)
from tests.fakes import FakeSession, parse_sse

DECISION_PATH = f"{BASE}/conversations/{CONVERSATION_ID}/proposals/call_c/decision"
ANSWER_PATH = f"{BASE}/conversations/{CONVERSATION_ID}/questions/call_q/answer"
DELEGATION_TOOLS = {"edit_block", "edit_module"}
ACCEPTED = "Le professeur a ACCEPTÉ la proposition et l'a appliquée au bloc."
QUESTIONS_ARGS = {
    "questions": [
        {
            "question": "Quel niveau ?",
            "multi_select": False,
            "options": [{"label": "Seconde"}, {"label": "Première"}],
        }
    ]
}
SHAPE = [{"multi_select": False, "options": 2}]


# ------------------------------------------------------------------- outils


def _usage(input_tokens, output_tokens):
    return AIUsage(input_tokens=input_tokens, output_tokens=output_tokens)


def _token(text):
    return AIStreamEvent(type="token", delta=text)


def _tool_call(call_id, name, arguments):
    return AIStreamEvent(
        type="tool_call", tool_call=AIToolCall(id=call_id, name=name, arguments=arguments)
    )


def _tool_result(call_id, name, text, error=False):
    return AIStreamEvent(
        type="tool_result",
        delta=text,
        tool_call=AIToolCall(id=call_id, name=name),
        tool_result_error=error,
    )


def _done(usage=None):
    return AIStreamEvent(type="done", usage=usage)


def _parent_delegates(usage=None, call_id="call_d", target_id=BLOCK_ID, context="block_text"):
    """Run de l'assistant : il délègue le bloc B1 (interrupt ``delegation``)."""
    return [
        _token("Je confie ce bloc à un sous-assistant. "),
        _tool_call(
            call_id,
            "edit_block",
            {"target_ref": "B1", "instructions": "Réécris l'introduction."},
        ),
        AIStreamEvent(
            type="interrupt",
            interrupt_value=delegation_interrupt_value(call_id, context, target_id),
            usage=usage,
        ),
    ]


def _child_proposes(usage=None, call_id="call_c", summary="Réécriture"):
    """Run du sous-assistant : lecture, texte, proposition (interrupt ``proposal``)."""
    return [
        _tool_call("call_r", "read_block", {"block_ref": "B1"}),
        _tool_result("call_r", "read_block", "# Intro\n\nPythagore."),
        _token("Je propose une réécriture. "),
        _tool_call(
            call_id,
            "propose_block_edit",
            {"new_markdown": "# Intro\n\n![f](oc-resource:R1)", "summary": summary},
        ),
        AIStreamEvent(
            type="interrupt", interrupt_value=proposal_interrupt_value(call_id), usage=usage
        ),
    ]


def _child_finishes(text="Terminé.", usage=None, decided="call_c"):
    """Reprise du sous-assistant après une décision : résultat de la
    proposition, texte final, ``done`` (absorbé par le driver)."""
    events = []
    if decided is not None:
        events.append(_tool_result(decided, "propose_block_edit", ACCEPTED))
    return [*events, _token(text), _done(usage)]


def _parent_finishes(text="Parfait, c'est appliqué.", usage=None, call_id="call_d"):
    """Reprise de l'assistant avec le compte rendu : résultat du tool
    ``edit_*`` (le faux client ne l'exécute pas : texte scripté), réponse."""
    return [
        _tool_result(call_id, "edit_block", "Sous-assistant terminé."),
        _token(text),
        _done(usage),
    ]


def _delegation(**overrides):
    defaults = dict(
        parent_thread_id="t-parent",
        parent_call_id="call_d",
        context="block_text",
        target_id=str(BLOCK_ID),
        instructions="Réécris l'introduction.",
        pending_summary="Réécriture",
        count=1,
    )
    defaults.update(overrides)
    return hitl.Delegation(**defaults)


def _pending_child(kind=hitl.KIND_PROPOSAL, tool_call_id="call_c", delegation=None, **overrides):
    """Reprise en attente d'un sous-assistant (thread ``t-child``) derrière
    l'assistant figé (``t-parent``)."""
    defaults = dict(
        thread_id="t-child",
        tool_call_id=tool_call_id,
        provider="ollama",
        config=None,
        kind=kind,
        allow_edit=True,
        delegation=delegation or _delegation(),
    )
    defaults.update(overrides)
    return hitl.PendingInterrupt(**defaults)


def _course_conversation():
    return conversation_row(title="T")


def _existing_turn():
    """Tour partiel déjà persisté : user (0), assistant à l'appel ``edit_block`` (1)."""
    return [
        message_row(0, role="user"),
        message_row(1, role="assistant", tool_calls=[{"id": "call_d", "name": "edit_block"}]),
    ]


def _agents(events_out):
    return [data.get("agent") for _, data in events_out]


# ------------------------------------------------------ tools et prompt du tour


def test_stream_allow_edit_exposes_delegation_tools_and_prompt() -> None:
    """Contexte ``course`` à ``allow_edit`` : tools ``edit_block``/``edit_module``
    bloquants, ``enum`` des cibles éligibles, prompt d'édition globale — sans
    catalogue de syntaxes ni protocole HITL."""
    session = stream_session(conversation=_course_conversation(), modules=[module_row()])
    client, fake = make_client(session, FakeAssistantAI(events=[_done()]))
    response = client.post(STREAM_PATH, json={"content": "Salut", "allow_edit": True})
    assert response.status_code == 200
    [call] = fake.calls
    specs = {t.name: t for t in call["tools"]}
    assert DELEGATION_TOOLS <= set(specs)
    assert specs["edit_block"].blocking and specs["edit_module"].blocking
    assert specs["edit_block"].parameters["properties"]["target_ref"]["enum"] == ["B1"]
    assert specs["edit_module"].parameters["properties"]["target_ref"]["enum"] == ["M1"]
    system = call["messages"][0].content
    assert system == COURSE_EDITING_SYSTEM_PROMPT
    assert "edit_block" in system
    assert "```mermaid" not in system
    assert "Protocole de proposition" not in system
    assert fake.dropped_threads == [call["thread_id"]]


def test_stream_without_allow_edit_is_unchanged() -> None:
    session = stream_session(conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(events=[_done()]))
    response = client.post(STREAM_PATH, json={"content": "Salut"})
    assert response.status_code == 200
    [call] = fake.calls
    assert not {t.name for t in call["tools"]} & DELEGATION_TOOLS
    assert call["messages"][0].content == COURSE_SYSTEM_PROMPT


def test_allow_edit_is_ignored_in_an_editing_context() -> None:
    """Un chat d'édition ne délègue jamais : ``allow_edit`` n'y change rien."""
    conv = conversation_row(context="block_text", block_id=BLOCK_ID, title="T")
    session = stream_session(conversation=conv)
    client, fake = make_client(session, FakeAssistantAI(events=[_done()]))
    response = client.post(STREAM_PATH, json={"content": "Salut", "allow_edit": True})
    assert response.status_code == 200
    [call] = fake.calls
    assert not {t.name for t in call["tools"]} & DELEGATION_TOOLS
    assert call["messages"][0].content == BLOCK_TEXT.system_prompt


# ------------------------------------------------- aller : délégation et child


def test_stream_delegation_runs_the_child_until_its_proposal() -> None:
    """L'assistant délègue : son interrupt ``delegation`` n'est pas relayé, le
    sous-assistant démarre dans le flux (descripteur du bloc, cible en entier
    + consignes, thread propre), ses événements sont tagués ``agent``, sa
    proposition (args réécrits) ferme le flux sur un ``interrupt`` porteur
    d'``agent`` ; seul le segment de l'assistant (appel ``edit_block`` aux
    args réécrits, usage cumulé) est persisté ; la reprise enregistrée pointe
    le thread du sous-assistant et le parent figé ; aucun thread purgé."""
    scripts = [_parent_delegates(usage=_usage(100, 20)), _child_proposes(usage=_usage(50, 10))]
    session = stream_session(conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(scripts=scripts))
    try:
        response = client.post(
            STREAM_PATH, json={"content": "Améliore l'intro", "allow_edit": True}
        )
        assert response.status_code == 200

        events_out = parse_sse(response.text)
        assert [k for k, _ in events_out] == [
            "token",
            "tool_call",
            "tool_call",
            "tool_result",
            "token",
            "tool_call",
            "interrupt",
        ]
        assert _agents(events_out) == [None, None, "call_d", "call_d", "call_d", "call_d", "call_d"]
        delegation_call = events_out[1][1]
        assert delegation_call["args"] == {
            "target_ref": "B1",
            "instructions": "Réécris l'introduction.",
            "block_id": str(BLOCK_ID),
            "context": "block_text",
            "target_title": "Intro",
        }
        proposal_call = events_out[5][1]
        assert proposal_call["name"] == "propose_block_edit"
        assert (
            proposal_call["args"]["new_markdown"] == f"# Intro\n\n![f](oc-resource:{RESOURCE_ID})"
        )
        interrupt = events_out[-1][1]
        assert interrupt["tool_call_id"] == "call_c"
        assert interrupt["kind"] == "proposal"
        assert interrupt["agent"] == "call_d"
        assert len(interrupt["message_ids"]) == 1
        assert interrupt["usage"] == {
            "input_tokens": 150,
            "output_tokens": 30,
            "cached_input_tokens": None,
        }

        # Seul le segment de l'assistant est persisté (transcript du
        # sous-assistant jamais en base), porteur de l'usage cumulé du flux.
        rows = inserted_message_rows(session)
        assert [r["role"] for r in rows] == ["assistant"]
        assert rows[0]["content"] == "Je confie ce bloc à un sous-assistant. "
        assert [c["name"] for c in rows[0]["tool_calls"]] == ["edit_block"]
        assert rows[0]["tool_calls"][0]["arguments"]["block_id"] == str(BLOCK_ID)
        assert rows[0]["input_tokens"] == 150
        assert rows[0]["output_tokens"] == 30

        parent_call, child_call = fake.calls
        assert child_call["thread_id"] != parent_call["thread_id"]
        assert child_call["trace_name"] == "course-assistant-delegate"
        assert child_call["messages"][0].content == BLOCK_TEXT.system_prompt
        turn = child_call["messages"][-1].content
        assert "## Bloc en cours d'édition" in turn
        assert DELEGATED_NOTE in turn
        assert "Réécris l'introduction." in turn
        child_tools = {t.name for t in child_call["tools"]}
        assert {"propose_block_edit", "ask_questions", "read_block"} <= child_tools
        assert not child_tools & DELEGATION_TOOLS

        pending = hitl.peek(CONVERSATION_ID, "call_c", kind=hitl.KIND_PROPOSAL)
        assert pending is not None
        assert pending.thread_id == child_call["thread_id"]
        assert pending.allow_edit is True
        assert pending.carried_usage is None
        assert pending.delegation == hitl.Delegation(
            parent_thread_id=parent_call["thread_id"],
            parent_call_id="call_d",
            context="block_text",
            target_id=str(BLOCK_ID),
            instructions="Réécris l'introduction.",
            outcomes=(),
            pending_summary="Réécriture",
            count=1,
        )
        assert fake.dropped_threads == []
    finally:
        hitl.drop(CONVERSATION_ID)


def test_stream_child_without_proposal_resumes_the_parent_in_the_same_stream() -> None:
    """Sous-assistant terminé sans proposer : son ``done`` est absorbé, son
    thread purgé, l'assistant repris (même thread, compte rendu en valeur de
    reprise) et le flux se clôt sur UN ``done`` à l'usage cumulé des trois
    runs ; segments persistés à la suite."""
    scripts = [
        _parent_delegates(usage=_usage(100, 20)),
        _child_finishes("Rien à changer.", usage=_usage(10, 2), decided=None),
        _parent_finishes("OK.", usage=_usage(5, 1)),
    ]
    session = stream_session(conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(scripts=scripts))
    response = client.post(STREAM_PATH, json={"content": "Améliore l'intro", "allow_edit": True})
    assert response.status_code == 200

    events_out = parse_sse(response.text)
    assert [k for k, _ in events_out] == [
        "token",
        "tool_call",
        "token",
        "tool_result",
        "token",
        "done",
    ]
    assert _agents(events_out) == [None, None, "call_d", None, None, None]
    done = events_out[-1][1]
    assert done["usage"] == {"input_tokens": 115, "output_tokens": 23, "cached_input_tokens": None}
    assert len(done["message_ids"]) == 3

    parent_call, child_call, resumed = fake.calls
    assert resumed["thread_id"] == parent_call["thread_id"]
    assert resumed["resume"]["ok"] is True
    assert RECAP_NO_PROPOSAL in resumed["resume"]["text"]
    assert "Rien à changer." in resumed["resume"]["text"]
    assert [m.role for m in resumed["messages"]] == ["system"]
    assert resumed["messages"][0].content == COURSE_EDITING_SYSTEM_PROMPT
    assert DELEGATION_TOOLS <= {t.name for t in resumed["tools"]}
    assert fake.dropped_threads == [child_call["thread_id"], parent_call["thread_id"]]

    rows = inserted_message_rows(session)
    assert [(r["role"], r["position"]) for r in rows] == [
        ("assistant", 1),
        ("tool", 2),
        ("assistant", 3),
    ]
    assert rows[1]["tool_call_id"] == "call_d"
    assert rows[2]["content"] == "OK."
    assert rows[2]["input_tokens"] == 115


@pytest.mark.parametrize(
    ("interrupt_value", "text"),
    [
        (delegation_interrupt_value(target_id=uuid.uuid4()), "n'existe plus"),
        ({"tool_call_id": "call_d", "kind": "delegation"}, INTERRUPTED_TEXT),
    ],
)
def test_stream_unlaunchable_delegation_resumes_the_parent_with_an_error(
    interrupt_value, text
) -> None:
    """Défensif : cible absente de l'instantané ou demande malformée — aucun
    sous-assistant, l'assistant est repris avec un résultat en erreur."""
    scripts = [
        [
            _tool_call("call_d", "edit_block", {"target_ref": "B1", "instructions": "x"}),
            AIStreamEvent(type="interrupt", interrupt_value=interrupt_value),
        ],
        [_tool_result("call_d", "edit_block", text, error=True), _token("Désolé."), _done()],
    ]
    session = stream_session(conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(scripts=scripts))
    response = client.post(STREAM_PATH, json={"content": "Go", "allow_edit": True})
    assert response.status_code == 200
    assert [k for k, _ in parse_sse(response.text)] == ["tool_call", "tool_result", "token", "done"]
    first, resumed = fake.calls
    assert resumed["thread_id"] == first["thread_id"]
    assert resumed["resume"]["ok"] is False
    assert text in resumed["resume"]["text"]


def test_stream_child_eager_error_emits_error_and_purges() -> None:
    """Erreur eager au lancement du sous-assistant (le 200 est parti) :
    événement ``error``, partiel de l'assistant persisté, thread purgé."""
    scripts = [
        _parent_delegates(),
        HTTPException(status_code=503, detail="Fournisseur IA injoignable"),
    ]
    session = stream_session(conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(scripts=scripts))
    response = client.post(STREAM_PATH, json={"content": "Go", "allow_edit": True})
    assert response.status_code == 200
    events_out = parse_sse(response.text)
    assert [k for k, _ in events_out] == ["token", "tool_call", "error"]
    assert events_out[-1][1]["status"] == 503
    [call] = fake.calls
    assert fake.dropped_threads == [call["thread_id"]]
    rows = inserted_message_rows(session)
    assert [r["role"] for r in rows] == ["assistant"]
    assert hitl.drop(CONVERSATION_ID) is None


def test_stream_child_questions_interrupt_registers_the_delegation() -> None:
    """Le sous-assistant pose des questions : ``interrupt`` de genre
    ``questions`` tagué ``agent``, reprise enregistrée derrière le parent
    (aucun résumé de proposition en attente)."""
    scripts = [
        _parent_delegates(),
        [
            _tool_call("call_q", "ask_questions", QUESTIONS_ARGS),
            AIStreamEvent(
                type="interrupt", interrupt_value=questions_interrupt_value("call_q", SHAPE)
            ),
        ],
    ]
    session = stream_session(conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(scripts=scripts))
    try:
        response = client.post(STREAM_PATH, json={"content": "Go", "allow_edit": True})
        assert response.status_code == 200
        events_out = parse_sse(response.text)
        assert [k for k, _ in events_out] == ["token", "tool_call", "tool_call", "interrupt"]
        interrupt = events_out[-1][1]
        assert interrupt["kind"] == "questions"
        assert interrupt["tool_call_id"] == "call_q"
        assert interrupt["agent"] == "call_d"
        pending = hitl.peek(CONVERSATION_ID, "call_q", kind=hitl.KIND_QUESTIONS)
        assert pending is not None
        assert pending.answer_shape == SHAPE
        assert pending.delegation is not None
        assert pending.delegation.pending_summary is None
        assert pending.delegation.parent_call_id == "call_d"
        assert fake.dropped_threads == []
    finally:
        hitl.drop(CONVERSATION_ID)


# ---------------------------------------------- reprises : décision et réponse


def test_proposal_decision_resumes_the_child_then_the_parent() -> None:
    """La décision reprend le SOUS-ASSISTANT (son thread, son descripteur,
    valeur = décision) ; son ``done`` est absorbé (thread purgé, ligne de
    compte rendu) ; l'assistant est repris (son thread, prompt et tools
    d'édition globale, valeur = compte rendu) et le flux se clôt : lignes
    persistées à la suite du tour partiel, usage cumulé, reprise consommée."""
    hitl.register(CONVERSATION_ID, _pending_child())
    scripts = [
        _child_finishes("Terminé.", usage=_usage(30, 5)),
        _parent_finishes(usage=_usage(40, 8)),
    ]
    session = resume_session(messages=_existing_turn(), conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(scripts=scripts))
    response = client.post(DECISION_PATH, json={"accepted": True, "comment": "Très bien"})
    assert response.status_code == 200

    events_out = parse_sse(response.text)
    assert [k for k, _ in events_out] == ["tool_result", "token", "tool_result", "token", "done"]
    assert _agents(events_out) == ["call_d", "call_d", None, None, None]
    assert events_out[2][1]["name"] == "edit_block"
    done = events_out[-1][1]
    assert done["usage"] == {"input_tokens": 70, "output_tokens": 13, "cached_input_tokens": None}
    assert done["user_message_id"] is None
    assert len(done["message_ids"]) == 2

    child_call, parent_call = fake.calls
    assert child_call["thread_id"] == "t-child"
    assert child_call["resume"] == {"accepted": True, "comment": "Très bien"}
    assert [m.role for m in child_call["messages"]] == ["system"]
    assert child_call["messages"][0].content == BLOCK_TEXT.system_prompt
    assert "propose_block_edit" in {t.name for t in child_call["tools"]}
    assert parent_call["thread_id"] == "t-parent"
    assert parent_call["messages"][0].content == COURSE_EDITING_SYSTEM_PROMPT
    assert DELEGATION_TOOLS <= {t.name for t in parent_call["tools"]}
    resume = parent_call["resume"]
    assert resume["ok"] is True
    assert f"1. propose_block_edit — « Réécriture » → {ACCEPTED}" in resume["text"]
    assert "Terminé." in resume["text"]
    assert fake.dropped_threads == ["t-child", "t-parent"]

    rows = inserted_message_rows(session)
    assert [(r["role"], r["position"]) for r in rows] == [("tool", 2), ("assistant", 3)]
    assert rows[0]["tool_call_id"] == "call_d"
    assert rows[0]["content"] == "Sous-assistant terminé."
    assert rows[1]["input_tokens"] == 70
    assert hitl.take(CONVERSATION_ID, "call_c", kind=hitl.KIND_PROPOSAL) is None


def test_proposal_decision_child_reproposes_carries_the_usage() -> None:
    """Le sous-assistant repris propose à nouveau : nouvel ``interrupt`` (autre
    appel, toujours tagué), rien à persister dans ce flux — son usage est
    reporté au registre —, ligne de compte rendu de la première décision
    conservée, résumé de la nouvelle proposition retenu."""
    hitl.register(CONVERSATION_ID, _pending_child())
    scripts = [
        [
            _tool_result("call_c", "propose_block_edit", ACCEPTED),
            _tool_call("call_c2", "propose_block_edit", {"new_markdown": "# V2", "summary": "V2"}),
            AIStreamEvent(
                type="interrupt",
                interrupt_value=proposal_interrupt_value("call_c2"),
                usage=_usage(20, 4),
            ),
        ]
    ]
    session = resume_session(messages=_existing_turn(), conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(scripts=scripts))
    try:
        response = client.post(DECISION_PATH, json={"accepted": True})
        assert response.status_code == 200
        events_out = parse_sse(response.text)
        assert [k for k, _ in events_out] == ["tool_result", "tool_call", "interrupt"]
        interrupt = events_out[-1][1]
        assert interrupt["tool_call_id"] == "call_c2"
        assert interrupt["agent"] == "call_d"
        assert interrupt["message_ids"] == []
        assert interrupt["usage"] == {
            "input_tokens": 20,
            "output_tokens": 4,
            "cached_input_tokens": None,
        }
        assert inserted_message_rows(session) is None

        pending = hitl.peek(CONVERSATION_ID, "call_c2", kind=hitl.KIND_PROPOSAL)
        assert pending is not None
        assert pending.thread_id == "t-child"
        assert pending.carried_usage == {
            "input_tokens": 20,
            "output_tokens": 4,
            "cached_input_tokens": None,
        }
        assert pending.delegation is not None
        assert pending.delegation.parent_thread_id == "t-parent"
        assert pending.delegation.outcomes == (f"propose_block_edit — « Réécriture » → {ACCEPTED}",)
        assert pending.delegation.pending_summary == "V2"
        assert fake.dropped_threads == []
    finally:
        hitl.drop(CONVERSATION_ID)


def test_carried_usage_lands_on_the_next_persisted_segment() -> None:
    """L'usage reporté d'un flux sans ligne s'ajoute au segment persisté
    suivant — mais ``done`` ne relaie que l'usage de SON flux (le front a
    déjà cumulé l'autre à l'``interrupt``)."""
    carried = {"input_tokens": 20, "output_tokens": 4, "cached_input_tokens": None}
    hitl.register(CONVERSATION_ID, _pending_child(carried_usage=carried))
    scripts = [_child_finishes(usage=_usage(10, 2)), _parent_finishes(usage=_usage(5, 1))]
    session = resume_session(messages=_existing_turn(), conversation=_course_conversation())
    client, _ = make_client(session, FakeAssistantAI(scripts=scripts))
    response = client.post(DECISION_PATH, json={"accepted": False})
    assert response.status_code == 200
    done = parse_sse(response.text)[-1][1]
    assert done["usage"] == {"input_tokens": 15, "output_tokens": 3, "cached_input_tokens": None}
    rows = inserted_message_rows(session)
    assert rows[-1]["input_tokens"] == 35
    assert rows[-1]["output_tokens"] == 7


def test_parent_redelegates_in_a_decision_stream() -> None:
    """Sous-assistant terminé, l'assistant repris délègue un module : second
    sous-assistant dans le flux de décision (descripteur du module, cible en
    entier), ``interrupt`` d'un autre appel ; le compte de délégations du
    tour progresse ; seul le premier sous-assistant est purgé."""
    hitl.register(CONVERSATION_ID, _pending_child())
    scripts = [
        _child_finishes("Fini.", decided="call_c"),
        [
            _tool_result("call_d", "edit_block", "Sous-assistant terminé."),
            _token("Passons au module. "),
            _tool_call(
                "call_d2", "edit_module", {"target_ref": "M1", "instructions": "Un bouton."}
            ),
            AIStreamEvent(
                type="interrupt",
                interrupt_value=delegation_interrupt_value(
                    "call_d2", "module", MODULE_ID, "Un bouton."
                ),
            ),
        ],
        [
            _tool_call("call_c2", "propose_js_edit", {"new_code": "x", "summary": "JS"}),
            AIStreamEvent(type="interrupt", interrupt_value=proposal_interrupt_value("call_c2")),
        ],
    ]
    session = resume_session(
        messages=_existing_turn(), conversation=_course_conversation(), modules=[module_row()]
    )
    client, fake = make_client(session, FakeAssistantAI(scripts=scripts))
    try:
        response = client.post(DECISION_PATH, json={"accepted": True})
        assert response.status_code == 200
        events_out = parse_sse(response.text)
        assert [k for k, _ in events_out] == [
            "tool_result",
            "token",
            "tool_result",
            "token",
            "tool_call",
            "tool_call",
            "interrupt",
        ]
        assert _agents(events_out) == ["call_d", "call_d", None, None, None, "call_d2", "call_d2"]
        module_call = events_out[4][1]
        assert module_call["args"] == {
            "target_ref": "M1",
            "instructions": "Un bouton.",
            "module_id": str(MODULE_ID),
            "context": "module",
            "target_title": "Compteur",
        }
        interrupt = events_out[-1][1]
        assert interrupt["tool_call_id"] == "call_c2"
        assert len(interrupt["message_ids"]) == 2

        rows = inserted_message_rows(session)
        assert [(r["role"], r["position"]) for r in rows] == [("tool", 2), ("assistant", 3)]
        assert rows[1]["content"] == "Passons au module. "
        assert [c["name"] for c in rows[1]["tool_calls"]] == ["edit_module"]

        _child, _parent, second_child = fake.calls
        assert second_child["thread_id"] not in {"t-child", "t-parent"}
        assert second_child["messages"][0].content == MODULE.system_prompt
        assert "## Module en cours d'édition" in second_child["messages"][-1].content
        assert "Un bouton." in second_child["messages"][-1].content

        pending = hitl.peek(CONVERSATION_ID, "call_c2", kind=hitl.KIND_PROPOSAL)
        assert pending is not None
        assert pending.thread_id == second_child["thread_id"]
        assert pending.delegation == hitl.Delegation(
            parent_thread_id="t-parent",
            parent_call_id="call_d2",
            context="module",
            target_id=str(MODULE_ID),
            instructions="Un bouton.",
            outcomes=(),
            pending_summary="JS",
            count=2,
        )
        assert fake.dropped_threads == ["t-child"]
    finally:
        hitl.drop(CONVERSATION_ID)


def test_delegation_cap_closes_the_turn() -> None:
    """Au-delà du plafond de délégations du tour, aucun sous-assistant n'est
    lancé : notice, ``done``, threads purgés, run parent abandonné (son appel
    reste sans résultat)."""
    hitl.register(
        CONVERSATION_ID, _pending_child(delegation=_delegation(count=MAX_DELEGATIONS_PER_TURN))
    )
    scripts = [
        _child_finishes(),
        [
            _tool_result("call_d", "edit_block", "Sous-assistant terminé."),
            _token("Encore un. "),
            _tool_call("call_d2", "edit_block", {"target_ref": "B1", "instructions": "x"}),
            AIStreamEvent(type="interrupt", interrupt_value=delegation_interrupt_value("call_d2")),
        ],
    ]
    session = resume_session(messages=_existing_turn(), conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(scripts=scripts))
    response = client.post(DECISION_PATH, json={"accepted": True})
    assert response.status_code == 200
    events_out = parse_sse(response.text)
    assert [k for k, _ in events_out] == [
        "tool_result",
        "token",
        "tool_result",
        "token",
        "tool_call",
        "token",
        "done",
    ]
    assert events_out[5][1] == {"delta": DELEGATIONS_EXCEEDED_NOTICE}
    assert len(fake.calls) == 2
    assert fake.dropped_threads == ["t-child", "t-parent"]
    rows = inserted_message_rows(session)
    assert [(r["role"], r["position"]) for r in rows] == [("tool", 2), ("assistant", 3)]
    assert rows[1]["content"] == "Encore un. " + DELEGATIONS_EXCEEDED_NOTICE
    assert [c["id"] for c in rows[1]["tool_calls"]] == ["call_d2"]
    assert hitl.drop(CONVERSATION_ID) is None


def test_child_questions_answer_resumes_child_then_parent() -> None:
    """La réponse aux questions du sous-assistant reprend son run, puis
    l'assistant ; les questions n'entrent pas dans le compte rendu."""
    hitl.register(
        CONVERSATION_ID,
        _pending_child(
            kind=hitl.KIND_QUESTIONS,
            tool_call_id="call_q",
            answer_shape=SHAPE,
            delegation=_delegation(pending_summary=None),
        ),
    )
    scripts = [
        [
            _tool_result("call_q", "ask_questions", "Le professeur a répondu à vos questions :"),
            _token("Merci."),
            _done(),
        ],
        _parent_finishes(),
    ]
    session = resume_session(messages=_existing_turn(), conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(scripts=scripts))
    response = client.post(ANSWER_PATH, json={"answers": [{"selected": [0]}]})
    assert response.status_code == 200
    events_out = parse_sse(response.text)
    assert [k for k, _ in events_out] == ["tool_result", "token", "tool_result", "token", "done"]
    assert _agents(events_out) == ["call_d", "call_d", None, None, None]
    child_call, parent_call = fake.calls
    assert child_call["thread_id"] == "t-child"
    assert child_call["resume"] == {
        "declined": False,
        "answers": [{"selected": [0], "other": None}],
    }
    assert parent_call["thread_id"] == "t-parent"
    assert RECAP_NO_PROPOSAL in parent_call["resume"]["text"]
    assert "Merci." in parent_call["resume"]["text"]
    assert fake.dropped_threads == ["t-child", "t-parent"]


@pytest.mark.parametrize("allow_edit", [True, False])
def test_parent_questions_resume_replays_allow_edit(allow_edit) -> None:
    """Questions posées par l'assistant lui-même en édition globale : la
    reprise rebâtit les mêmes tools et le même prompt (``allow_edit`` rejoué
    depuis le registre, jamais depuis le corps de la réponse)."""
    hitl.register(
        CONVERSATION_ID,
        hitl.PendingInterrupt(
            thread_id="t-run",
            tool_call_id="call_q",
            provider="ollama",
            config=None,
            kind=hitl.KIND_QUESTIONS,
            answer_shape=SHAPE,
            allow_edit=allow_edit,
        ),
    )
    events = [
        _tool_result("call_q", "ask_questions", "Le professeur a répondu à vos questions :"),
        _token("Merci."),
        _done(),
    ]
    existing = [
        message_row(0, role="user"),
        message_row(1, role="assistant", tool_calls=[{"id": "call_q", "name": "ask_questions"}]),
    ]
    session = resume_session(messages=existing, conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(events=events))
    response = client.post(ANSWER_PATH, json={"answers": [{"selected": [1]}]})
    assert response.status_code == 200
    [call] = fake.calls
    assert call["thread_id"] == "t-run"
    assert (DELEGATION_TOOLS <= {t.name for t in call["tools"]}) is allow_edit
    expected = COURSE_EDITING_SYSTEM_PROMPT if allow_edit else COURSE_SYSTEM_PROMPT
    assert call["messages"][0].content == expected
    assert fake.dropped_threads == ["t-run"]


def test_proposal_decision_on_course_context_without_delegation_404() -> None:
    """Une proposition hors contexte d'édition n'existe que derrière une
    délégation : sans elle, 404 sans consommer le registre."""
    hitl.register(
        CONVERSATION_ID,
        hitl.PendingInterrupt(
            thread_id="t-x", tool_call_id="call_c", provider="ollama", config=None, allow_edit=True
        ),
    )
    try:
        session = FakeSession([[user_row()], [course_row()], [_course_conversation()]])
        client, _ = make_client(session)
        response = client.post(DECISION_PATH, json={"accepted": True})
        assert response.status_code == 404
        assert hitl.peek(CONVERSATION_ID, "call_c", kind=hitl.KIND_PROPOSAL) is not None
    finally:
        hitl.drop(CONVERSATION_ID)


# ------------------------------------------------------- abandon et purge


def test_new_message_abandons_a_delegation_and_purges_both_threads() -> None:
    hitl.register(CONVERSATION_ID, _pending_child())
    session = stream_session(conversation=_course_conversation())
    client, fake = make_client(session, FakeAssistantAI(events=[_done()]))
    response = client.post(STREAM_PATH, json={"content": "Autre chose"})
    assert response.status_code == 200
    [call] = fake.calls
    assert fake.dropped_threads == ["t-child", "t-parent", call["thread_id"]]
    assert hitl.drop(CONVERSATION_ID) is None


def test_delete_conversation_purges_both_threads_of_a_delegation() -> None:
    hitl.register(CONVERSATION_ID, _pending_child())
    try:
        session = FakeSession([[user_row()], [course_row()], [_course_conversation()]])
        client, fake = make_client(session)
        response = client.delete(f"{BASE}/conversations/{CONVERSATION_ID}")
        assert response.status_code == 204
        assert fake.dropped_threads == ["t-child", "t-parent"]
        assert hitl.drop(CONVERSATION_ID) is None
    finally:
        hitl.drop(CONVERSATION_ID)


def _sink(fake, conversation=None):
    refs = build_refs([block_row()], [resource_row()], [])
    return _AssistantTurn(
        client=fake,
        db=None,
        conversation=conversation or _course_conversation(),
        refs=refs,
        edit=None,
        provider="ollama",
        config=None,
        thread_id="t-parent",
        base_position=1,
        user_message_id=None,
        title_set=None,
        allow_edit=True,
    )


def _agent_run(thread_id="t-child"):
    refs = build_refs([block_row()], [resource_row()], [], focus_block=block_row())
    return _AgentRun(
        call_id="call_d",
        thread_id=thread_id,
        edit=BLOCK_TEXT,
        refs=refs,
        context="block_text",
        target_id=BLOCK_ID,
        instructions="x",
        count=1,
    )


async def _chunks():
    for chunk in ("a", "b"):
        yield chunk


@pytest.mark.anyio
async def test_release_on_close_in_a_child_purges_both_threads() -> None:
    """Client parti pendant le sous-assistant : ses deux threads sont purgés,
    une seule fois."""
    fake = FakeAssistantAI()
    sink = _sink(fake)
    sink.enter_agent(_agent_run())
    stream = _release_on_close(_chunks(), sink)
    assert await anext(stream) == "a"
    await stream.aclose()
    assert fake.dropped_threads == ["t-child", "t-parent"]
    sink.release()
    assert fake.dropped_threads == ["t-child", "t-parent"]


@pytest.mark.anyio
async def test_release_on_close_keeps_a_suspended_child() -> None:
    fake = FakeAssistantAI()
    sink = _sink(fake)
    sink.enter_agent(_agent_run())
    sink._suspended = True
    assert [chunk async for chunk in _release_on_close(_chunks(), sink)] == ["a", "b"]
    assert fake.dropped_threads == []


@pytest.mark.anyio
async def test_sink_agent_mode_never_persists_the_child() -> None:
    """En mode agent, texte et appels du sous-assistant ne rejoignent jamais
    les segments persistés : ils nourrissent le compte rendu (résumés des
    propositions, décisions), et son ``done`` est absorbé."""
    fake = FakeAssistantAI()
    sink = _sink(fake)
    sink.text("Assistant. ")
    sink.enter_agent(_agent_run())
    sink.text("Sous-assistant. ")
    args = sink.tool_call(
        AIToolCall(
            id="call_c",
            name="propose_block_edit",
            arguments={"new_markdown": "![f](oc-resource:R1)", "summary": "S"},
        )
    )
    assert args["new_markdown"] == f"![f](oc-resource:{RESOURCE_ID})"
    sink.tool_call(AIToolCall(id="call_r", name="read_block", arguments={"block_ref": "B1"}))
    sink.tool_result(_tool_result("call_r", "read_block", "…"))
    sink.tool_result(_tool_result("call_c", "propose_block_edit", ACCEPTED))
    assert await sink.done({"input_tokens": 3, "output_tokens": 1}) is None
    assert sink.agent is None
    assert fake.dropped_threads == ["t-child"]
    resume = sink.take_parent_resume()
    assert resume["ok"] is True
    assert f"1. propose_block_edit — « S » → {ACCEPTED}" in resume["text"]
    assert "Sous-assistant." in resume["text"]
    assert sink.take_parent_resume() is None
    assert sink._segment_text == ["Assistant. "]
    assert sink._segment_tool_calls == []
    assert sink.stream_usage == {"input_tokens": 3, "output_tokens": 1, "cached_input_tokens": None}
