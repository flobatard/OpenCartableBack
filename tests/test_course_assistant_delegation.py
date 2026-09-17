"""Tests des helpers purs de la délégation (``app/course_assistant/delegation.py``) :
specs et ``enum`` des tools ``edit_block``/``edit_module``, handlers
(validation avant l'interrupt, payload de délégation, résultat depuis la
valeur de reprise), réécriture des args à l'émission, message des consignes
et compte rendu. Le driver et le flux sont couverts par
``test_course_assistant_delegation_api.py``."""

import uuid

import pytest

from app.core.ai import AIToolCall
from app.course_assistant import hitl
from app.course_assistant.context import TEACHER_LABEL, build_refs
from app.course_assistant.delegation import (
    DELEGATED_NOTE,
    EDIT_BLOCK,
    EDIT_MODULE,
    INTERRUPTED_TEXT,
    MAX_DELEGATION_INSTRUCTIONS_CHARS,
    RECAP_FOOTER,
    RECAP_NO_PROPOSAL,
    RECAP_TEXT_CHARS,
    DelegationRequest,
    context_for_block,
    delegated_message,
    delegation_result,
    outcome_line,
    recap,
    resume_value,
)
from app.course_assistant.tools import build_tool_executor, build_tool_specs
from tests.course_assistant_fakes import (
    BLOCK_ID,
    MODULE_ID,
    RESOURCE_ID,
    block_row,
    module_row,
    resource_row,
)
from tests.fakes import FakeStorage

EXERCISE_ID = uuid.uuid4()
DOCUMENT_ID = uuid.uuid4()
POINTER_ID = uuid.uuid4()


def _exercise_row():
    return block_row(
        id=EXERCISE_ID,
        type="exercise",
        title="Exercice",
        content={"statement": "Soit $x$.", "questions": []},
    )


def _blocks():
    """Un bloc de chaque type : texte (B1), exercice (B2), document (B3),
    pointeur de module (B4)."""
    return [
        block_row(),
        _exercise_row(),
        block_row(id=DOCUMENT_ID, type="document", title="Fiche", content={}),
        block_row(id=POINTER_ID, type="module", title="Appli", content={}, module_id=MODULE_ID),
    ]


def _refs(blocks=None, modules=None):
    return build_refs(
        blocks if blocks is not None else _blocks(),
        [resource_row()],
        modules if modules is not None else [module_row()],
    )


def _executor(refs=None):
    return build_tool_executor(FakeStorage(), refs or _refs(), delegation=True)


def _no_interrupt(monkeypatch) -> None:
    monkeypatch.setattr(
        hitl,
        "agent_interrupt",
        lambda payload: pytest.fail("interrupt inattendu sur des args invalides"),
    )


# ------------------------------------------------------------------- specs


def test_specs_list_editable_blocks_and_modules() -> None:
    """``edit_block`` n'énumère que les blocs texte et exercice, ``edit_module``
    les modules ; les deux sont bloquants ; absents sans ``delegation``."""
    specs = {s.name: s for s in build_tool_specs(_refs(), delegation=True)}
    assert {EDIT_BLOCK, EDIT_MODULE} <= set(specs)
    assert specs[EDIT_BLOCK].blocking and specs[EDIT_MODULE].blocking
    block_params = specs[EDIT_BLOCK].parameters
    assert block_params["properties"]["target_ref"]["enum"] == ["B1", "B2"]
    assert block_params["required"] == ["target_ref", "instructions"]
    assert specs[EDIT_MODULE].parameters["properties"]["target_ref"]["enum"] == ["M1"]

    names = {s.name for s in build_tool_specs(_refs())}
    assert not names & {EDIT_BLOCK, EDIT_MODULE}


def test_specs_omit_the_enum_when_nothing_is_eligible() -> None:
    refs = _refs(blocks=[block_row(id=DOCUMENT_ID, type="document", content={})], modules=[])
    specs = {s.name: s for s in build_tool_specs(refs, delegation=True)}
    assert "enum" not in specs[EDIT_BLOCK].parameters["properties"]["target_ref"]
    assert "enum" not in specs[EDIT_MODULE].parameters["properties"]["target_ref"]


# ---------------------------------------------------------------- handlers


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "target_ref", "context", "target_id"),
    [
        (EDIT_BLOCK, "B1", "block_text", BLOCK_ID),
        (EDIT_BLOCK, "Exercice", "block_exercise", EXERCISE_ID),
        (EDIT_MODULE, "M1", "module", MODULE_ID),
    ],
)
async def test_handler_suspends_with_the_resolved_target(
    monkeypatch, name, target_ref, context, target_id
) -> None:
    """Après validation, le handler fige le run parent avec la cible résolue
    (contexte du descripteur, id) et les consignes ; à la reprise, la valeur
    est le compte rendu du sous-assistant — le résultat du tool."""
    seen: list[dict] = []
    monkeypatch.setattr(
        hitl,
        "agent_interrupt",
        lambda payload: seen.append(payload) or {"ok": True, "text": "Compte rendu"},
    )
    result = await _executor()(
        AIToolCall(
            id="call_d", name=name, arguments={"target_ref": target_ref, "instructions": "Go"}
        )
    )
    assert not result.is_error
    assert result.content == "Compte rendu"
    assert seen == [
        {
            "tool_call_id": "call_d",
            "kind": hitl.KIND_DELEGATION,
            "context": context,
            "target_id": str(target_id),
            "instructions": "Go",
        }
    ]


@pytest.mark.anyio
async def test_edit_block_validates_before_interrupting(monkeypatch) -> None:
    """Cible inconnue, inéligible (document, pointeur de module) ou consignes
    absentes/vides/trop longues : échec immédiat, JAMAIS d'interrupt."""
    _no_interrupt(monkeypatch)
    executor = _executor()

    unknown = await executor(
        AIToolCall(id="c1", name=EDIT_BLOCK, arguments={"target_ref": "B9", "instructions": "x"})
    )
    assert unknown.is_error
    assert "B1" in unknown.content and "B2" in unknown.content  # candidats éligibles listés
    assert "B3" not in unknown.content

    document = await executor(
        AIToolCall(id="c2", name=EDIT_BLOCK, arguments={"target_ref": "B3", "instructions": "x"})
    )
    assert document.is_error
    assert "texte et exercice" in document.content
    assert "edit_module" not in document.content

    pointer = await executor(
        AIToolCall(id="c3", name=EDIT_BLOCK, arguments={"target_ref": "B4", "instructions": "x"})
    )
    assert pointer.is_error
    assert "edit_module" in pointer.content

    missing = await executor(AIToolCall(id="c4", name=EDIT_BLOCK, arguments={"target_ref": "B1"}))
    assert missing.is_error and "instructions" in missing.content

    blank = await executor(
        AIToolCall(id="c5", name=EDIT_BLOCK, arguments={"target_ref": "B1", "instructions": "  "})
    )
    assert blank.is_error and "vide" in blank.content

    too_long = await executor(
        AIToolCall(
            id="c6",
            name=EDIT_BLOCK,
            arguments={
                "target_ref": "B1",
                "instructions": "x" * (MAX_DELEGATION_INSTRUCTIONS_CHARS + 1),
            },
        )
    )
    assert too_long.is_error and "plafond" in too_long.content


@pytest.mark.anyio
async def test_edit_module_validates_before_interrupting(monkeypatch) -> None:
    _no_interrupt(monkeypatch)
    executor = _executor()
    unknown = await executor(
        AIToolCall(id="c1", name=EDIT_MODULE, arguments={"target_ref": "M7", "instructions": "x"})
    )
    assert unknown.is_error and "M1" in unknown.content
    missing = await executor(AIToolCall(id="c2", name=EDIT_MODULE, arguments={"target_ref": "M1"}))
    assert missing.is_error and "instructions" in missing.content


@pytest.mark.anyio
async def test_delegation_tools_absent_by_default() -> None:
    result = await build_tool_executor(FakeStorage(), _refs())(
        AIToolCall(id="c1", name=EDIT_BLOCK, arguments={"target_ref": "B1", "instructions": "x"})
    )
    assert result.is_error and "inconnu" in result.content


@pytest.mark.parametrize(
    ("value", "content", "is_error"),
    [
        ({"ok": True, "text": "Fait"}, "Fait", False),
        ({"ok": False, "text": "Cible disparue"}, "Cible disparue", True),
        ({"text": "Sans statut"}, "Sans statut", True),
        ("reprise", INTERRUPTED_TEXT, True),
        ({"ok": True, "text": ""}, INTERRUPTED_TEXT, True),
        (None, INTERRUPTED_TEXT, True),
    ],
)
def test_delegation_result_from_the_resume_value(value, content, is_error) -> None:
    result = delegation_result(value)
    assert result.content == content
    assert result.is_error is is_error


# --------------------------------------------- réécriture des args à l'émission


def test_rewrite_args_adds_target_id_context_and_title() -> None:
    refs = _refs()
    rewrite = {t.name: t.rewrite_args for t in _delegation_tools()}
    text = rewrite[EDIT_BLOCK]({"target_ref": "B1", "instructions": "x"}, refs)
    assert text == {
        "target_ref": "B1",
        "instructions": "x",
        "block_id": str(BLOCK_ID),
        "context": "block_text",
        "target_title": "Intro",
    }
    exercise = rewrite[EDIT_BLOCK]({"target_ref": "B2", "instructions": "x"}, refs)
    assert exercise["block_id"] == str(EXERCISE_ID)
    assert exercise["context"] == "block_exercise"
    # Cible irrésolue ou inéligible : args tels quels (le handler a refusé l'appel).
    assert rewrite[EDIT_BLOCK]({"target_ref": "B9", "instructions": "x"}, refs) == {
        "target_ref": "B9",
        "instructions": "x",
    }
    assert "block_id" not in rewrite[EDIT_BLOCK]({"target_ref": "B3", "instructions": "x"}, refs)
    module = rewrite[EDIT_MODULE]({"target_ref": "M1", "instructions": "x"}, refs)
    assert module == {
        "target_ref": "M1",
        "instructions": "x",
        "module_id": str(MODULE_ID),
        "context": "module",
        "target_title": "Compteur",
    }
    assert "module_id" not in rewrite[EDIT_MODULE]({"target_ref": "M9"}, refs)


def _delegation_tools():
    from app.course_assistant.delegation import DELEGATION_TOOLS

    return DELEGATION_TOOLS


# ---------------------------------------------------- message et compte rendu


def test_delegated_message_layout() -> None:
    """La note de délégation précède les consignes, titrées comme une demande
    du professeur (la règle de structure du tour reste vraie)."""
    message = delegated_message("Réécris l'introduction.")
    assert message.startswith(DELEGATED_NOTE)
    assert f"## {TEACHER_LABEL}\n\nRéécris l'introduction." in message
    assert message.index(DELEGATED_NOTE) < message.index(TEACHER_LABEL)


def test_outcome_line_with_and_without_summary() -> None:
    assert outcome_line("propose_block_edit", "Réécriture", "ACCEPTÉ") == (
        "propose_block_edit — « Réécriture » → ACCEPTÉ"
    )
    assert outcome_line("propose_block_edit", None, "REJETÉ") == "propose_block_edit → REJETÉ"


def test_recap_lists_outcomes_and_abridges_the_final_text() -> None:
    text = recap(["a → ACCEPTÉ", "b → REJETÉ"], "Voilà,   c'est\nfait. " + "x" * RECAP_TEXT_CHARS)
    lines = text.split("\n")
    assert lines[0] == "Sous-assistant terminé."
    assert lines[2] == "1. a → ACCEPTÉ"
    assert lines[3] == "2. b → REJETÉ"
    final = lines[4]
    assert final.startswith("Message final du sous-assistant : Voilà, c'est fait. ")
    assert final.endswith("…")
    assert len(final) < RECAP_TEXT_CHARS + 60
    assert lines[-1] == RECAP_FOOTER

    empty = recap([], "")
    assert RECAP_NO_PROPOSAL in empty
    assert "Message final" not in empty
    assert empty.endswith(RECAP_FOOTER)


def test_resume_value_keys_are_never_hexadecimal() -> None:
    """LangGraph lit un dict à clés hex comme une table d'interrupts."""
    value = resume_value("texte")
    assert value == {"ok": True, "text": "texte"}
    assert resume_value("x", ok=False)["ok"] is False
    assert all(not all(c in "0123456789abcdef" for c in key) for key in value)


def test_delegation_request_from_interrupt() -> None:
    request = DelegationRequest.from_interrupt(
        {
            "tool_call_id": "call_d",
            "kind": "delegation",
            "context": "module",
            "target_id": str(MODULE_ID),
            "instructions": "Ajoute un bouton.",
        }
    )
    assert request == DelegationRequest("call_d", "module", MODULE_ID, "Ajoute un bouton.")
    assert DelegationRequest.from_interrupt({"context": "module", "target_id": "x"}) is None
    assert (
        DelegationRequest.from_interrupt({"target_id": str(MODULE_ID), "instructions": "x"}) is None
    )
    assert (
        DelegationRequest.from_interrupt({"context": "x", "target_id": "nope", "instructions": "y"})
        is None
    )


def test_context_for_block() -> None:
    assert context_for_block(block_row()) == "block_text"
    assert context_for_block(_exercise_row()) == "block_exercise"
    assert context_for_block(block_row(type="document")) is None
    assert context_for_block(block_row(type="module")) is None


def test_resource_link_rewriting_is_the_descriptor_job() -> None:
    """Les tools de délégation ne réécrivent que la cible : un lien de
    contenu dans les consignes reste verbatim (le sous-assistant, lui,
    réécrit ses propositions par son descripteur)."""
    refs = _refs()
    rewrite = {t.name: t.rewrite_args for t in _delegation_tools()}
    args = rewrite[EDIT_BLOCK]({"target_ref": "B1", "instructions": "Vois oc-resource:R1"}, refs)
    assert args["instructions"] == "Vois oc-resource:R1"
    assert str(RESOURCE_ID) not in args["instructions"]
