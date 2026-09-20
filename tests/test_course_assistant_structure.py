"""Tests des helpers purs des propositions structurelles
(``app/course_assistant/structure.py``) : specs et ``enum`` des tools
``propose_block_add``/``propose_block_delete``/``propose_blocks_reorder``,
handlers (validation avant l'interrupt, idempotence à la reprise sur
l'instantané rechargé — références renumérotées), textes d'acceptation et
réécriture des args à l'émission. Le flux est couvert par
``test_course_assistant_structure_api.py``."""

import uuid
from types import SimpleNamespace

import pytest

from app.core.ai import AIToolCall
from app.course_assistant import hitl
from app.course_assistant.context import build_refs
from app.course_assistant.structure import (
    BLOCK_DESCRIPTION_MAX_CHARS,
    BLOCK_TITLE_MAX_CHARS,
    PROPOSE_BLOCK_ADD,
    PROPOSE_BLOCK_DELETE,
    PROPOSE_BLOCKS_REORDER,
    STRUCTURE_TOOL_NAMES,
    STRUCTURE_TOOLS,
    outline_lines,
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
THIRD_ID = uuid.uuid4()
NEW_ID = uuid.uuid4()
PENDING_RESOURCE_ID = uuid.uuid4()


def _blocks():
    """Intro (B1, texte), Exercice (B2), Bilan (B3, texte)."""
    return [
        block_row(),
        block_row(id=EXERCISE_ID, type="exercise", title="Exercice", content={}),
        block_row(id=THIRD_ID, title="Bilan"),
    ]


def _pending_resource():
    return SimpleNamespace(
        **{**vars(resource_row()), "id": PENDING_RESOURCE_ID, "status": "pending"},
    )


def _refs(blocks=None, *, resources=None, modules=None, block_refs=None):
    return build_refs(
        blocks if blocks is not None else _blocks(),
        resources if resources is not None else [resource_row()],
        modules if modules is not None else [module_row()],
        block_refs=block_refs,
    )


def _origin(blocks=None) -> dict[str, str]:
    """``block_refs`` tels que capturés à l'interrupt sur ``blocks``."""
    return {e.ref: str(e.id) for e in _refs(blocks).entries["block"]}


def _executor(refs=None):
    return build_tool_executor(FakeStorage(), refs or _refs(), delegation=True)


def _no_interrupt(monkeypatch) -> None:
    monkeypatch.setattr(
        hitl,
        "agent_interrupt",
        lambda payload: pytest.fail("interrupt inattendu sur des args invalides"),
    )


def _decide(monkeypatch, accepted=True, comment=None) -> list[dict]:
    seen: list[dict] = []
    monkeypatch.setattr(
        hitl,
        "agent_interrupt",
        lambda payload: seen.append(payload) or {"accepted": accepted, "comment": comment},
    )
    return seen


def _call(name, **arguments):
    return AIToolCall(id="call_s", name=name, arguments={"summary": "Résumé", **arguments})


# ------------------------------------------------------------------- specs


def test_specs_are_blocking_and_gated_by_global_edit() -> None:
    specs = {s.name: s for s in build_tool_specs(_refs(), delegation=True)}
    assert STRUCTURE_TOOL_NAMES <= set(specs)
    assert all(specs[name].blocking for name in STRUCTURE_TOOL_NAMES)
    assert not STRUCTURE_TOOL_NAMES & {s.name for s in build_tool_specs(_refs())}

    add = specs[PROPOSE_BLOCK_ADD].parameters
    assert add["properties"]["type"]["enum"] == ["text", "exercise", "document", "module"]
    assert add["properties"]["after_ref"]["enum"] == ["B1", "B2", "B3"]
    assert add["properties"]["module_ref"]["enum"] == ["M1"]
    assert add["required"] == ["type", "title", "summary"]
    delete = specs[PROPOSE_BLOCK_DELETE].parameters
    assert delete["properties"]["target_ref"]["enum"] == ["B1", "B2", "B3"]
    reorder = specs[PROPOSE_BLOCKS_REORDER].parameters
    assert reorder["properties"]["order"]["items"]["enum"] == ["B1", "B2", "B3"]


def test_add_spec_lists_available_resources_only() -> None:
    refs = _refs(resources=[resource_row(), _pending_resource()])
    specs = {s.name: s for s in build_tool_specs(refs, delegation=True)}
    assert specs[PROPOSE_BLOCK_ADD].parameters["properties"]["resource_ref"]["enum"] == ["R1"]


def test_specs_omit_the_enum_on_an_empty_course() -> None:
    specs = {
        s.name: s
        for s in build_tool_specs(_refs(blocks=[], resources=[], modules=[]), delegation=True)
    }
    add = specs[PROPOSE_BLOCK_ADD].parameters["properties"]
    assert all("enum" not in add[key] for key in ("after_ref", "resource_ref", "module_ref"))
    assert "enum" not in specs[PROPOSE_BLOCK_DELETE].parameters["properties"]["target_ref"]
    assert "enum" not in specs[PROPOSE_BLOCKS_REORDER].parameters["properties"]["order"]["items"]


# ------------------------------------------------------------------- ajout


@pytest.mark.anyio
async def test_block_add_validates_before_interrupting(monkeypatch) -> None:
    _no_interrupt(monkeypatch)
    executor = _executor(_refs(resources=[resource_row(), _pending_resource()]))

    async def run(**arguments):
        result = await executor(_call(PROPOSE_BLOCK_ADD, **arguments))
        assert result.is_error
        return result.content

    assert "type" in await run(type="quiz", title="T")
    assert "title" in await run(type="text")
    assert "vide" in await run(type="text", title="   ")
    assert "plafond" in await run(type="text", title="x" * (BLOCK_TITLE_MAX_CHARS + 1))
    assert "plafond" in await run(
        type="text", title="T", description="x" * (BLOCK_DESCRIPTION_MAX_CHARS + 1)
    )
    assert "B1" in await run(type="text", title="T", after_ref="B9")
    # Document : ressource obligatoire, connue, disponible.
    missing = await run(type="document", title="Fiche")
    assert "resource_ref" in missing and "R1" in missing and "R2" not in missing
    assert "R1" in await run(type="document", title="Fiche", resource_ref="R9")
    assert "disponible" in await run(type="document", title="Fiche", resource_ref="R2")
    # Module : module obligatoire et connu.
    assert "module_ref" in await run(type="module", title="Appli")
    assert "M1" in await run(type="module", title="Appli", module_ref="M9")
    # Pointeurs refusés hors de leur type.
    assert "document" in await run(type="text", title="T", resource_ref="R1")
    assert "module" in await run(type="exercise", title="T", module_ref="M1")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {"type": "text", "title": "Rappels", "after_ref": "B1"},
        {"type": "exercise", "title": "Application"},
        {"type": "document", "title": "Fiche", "resource_ref": "R1"},
        {"type": "module", "title": "Appli", "module_ref": "M1", "after_ref": ""},
    ],
)
async def test_block_add_suspends_as_a_proposal(monkeypatch, arguments) -> None:
    seen = _decide(monkeypatch, accepted=False, comment="Pas ici")
    result = await _executor()(_call(PROPOSE_BLOCK_ADD, **arguments))
    assert seen == [{"tool_call_id": "call_s", "kind": hitl.KIND_PROPOSAL}]
    assert not result.is_error
    assert "REJETÉ" in result.content and result.content.endswith("Son commentaire : Pas ici")


@pytest.mark.anyio
@pytest.mark.parametrize(("block_type", "fill"), [("text", True), ("document", False)])
async def test_accepted_add_names_the_new_block_in_the_new_outline(
    monkeypatch, block_type, fill
) -> None:
    """À la reprise, l'instantané rechargé porte le bloc créé : sa référence
    (renumérotée) est nommée, le nouveau sommaire rendu ; un bloc à rédiger
    invite à ``edit_block``."""
    _decide(monkeypatch)
    new = block_row(id=NEW_ID, type=block_type, title="Rappels", content={})
    blocks = _blocks()
    refs = _refs([blocks[0], new, *blocks[1:]], block_refs=_origin())
    assert refs.new_block_refs == ("B2",)
    arguments = {"type": block_type, "title": "Rappels", "after_ref": "B1"}
    if block_type == "document":
        arguments["resource_ref"] = "R1"
    result = await _executor(refs)(_call(PROPOSE_BLOCK_ADD, **arguments))
    assert not result.is_error
    assert "ACCEPTÉ" in result.content and "Sa référence est B2." in result.content
    assert "renumérotées" in result.content
    assert f"B2 · Rappels ({block_type})" in result.content
    assert "B4 · Bilan (text)" in result.content
    assert ("edit_block" in result.content) is fill


@pytest.mark.anyio
async def test_accepted_add_without_a_single_new_block_points_to_the_outline(monkeypatch) -> None:
    _decide(monkeypatch)
    result = await _executor()(_call(PROPOSE_BLOCK_ADD, type="text", title="Rappels"))
    assert "Sa référence" not in result.content
    assert "Repérez-le" in result.content


# -------------------------------------------------------------- suppression


@pytest.mark.anyio
async def test_block_delete_validates_before_interrupting(monkeypatch) -> None:
    _no_interrupt(monkeypatch)
    unknown = await _executor()(_call(PROPOSE_BLOCK_DELETE, target_ref="B9"))
    assert unknown.is_error and "B1" in unknown.content
    missing = await _executor()(_call(PROPOSE_BLOCK_DELETE))
    assert missing.is_error


@pytest.mark.anyio
async def test_block_delete_is_idempotent_once_applied(monkeypatch) -> None:
    """Reprise d'une suppression acceptée : le bloc a disparu de l'instantané
    rechargé — la référence d'origine (la dernière, désormais irrésoluble, ou
    une du milieu, qui désigne maintenant un AUTRE bloc) passe la validation
    et le résultat rend le nouveau sommaire."""
    seen = _decide(monkeypatch)
    blocks = _blocks()
    for target_ref, remaining in (("B3", blocks[:2]), ("B1", blocks[1:])):
        refs = _refs(remaining, block_refs=_origin())
        assert list(refs.stale_blocks) == [target_ref]
        result = await _executor(refs)(_call(PROPOSE_BLOCK_DELETE, target_ref=target_ref))
        assert not result.is_error
        assert "supprimé" in result.content and "renumérotées" in result.content
        assert "B2 · " in result.content and "B3 · " not in result.content
    assert len(seen) == 2


# --------------------------------------------------------- réordonnancement


@pytest.mark.anyio
async def test_blocks_reorder_validates_before_interrupting(monkeypatch) -> None:
    _no_interrupt(monkeypatch)
    executor = _executor()

    async def run(order):
        result = await executor(_call(PROPOSE_BLOCKS_REORDER, order=order))
        assert result.is_error
        return result.content

    assert "order" in await run("B1,B2")
    assert "order" in await run([])
    assert "order" in await run(["B1", 2])
    problems = await run(["B2", "B2", "B7"])
    assert "inconnues : B7" in problems
    assert "en double : B2" in problems
    assert "manquants : B1, B3" in problems
    assert "déjà" in await run(["B1", "B2", "B3"])


@pytest.mark.anyio
async def test_blocks_reorder_is_idempotent_once_applied(monkeypatch) -> None:
    """Une permutation complète reste complète une fois les références
    renumérotées : ré-exécuté à la reprise, le tool rend la décision et le
    nouveau sommaire."""
    seen = _decide(monkeypatch)
    blocks = _blocks()
    order = ["B3", "B1", "B2"]
    first = await _executor()(_call(PROPOSE_BLOCKS_REORDER, order=order))
    assert not first.is_error
    applied = [blocks[2], blocks[0], blocks[1]]
    result = await _executor(_refs(applied, block_refs=_origin()))(
        _call(PROPOSE_BLOCKS_REORDER, order=order)
    )
    assert not result.is_error
    assert "réordonnés" in result.content
    assert result.content.index("B1 · Bilan") < result.content.index("B2 · Intro")
    assert seen == [{"tool_call_id": "call_s", "kind": hitl.KIND_PROPOSAL}] * 2


def test_outline_lines_handles_an_empty_course() -> None:
    assert "aucun bloc" in outline_lines(_refs(blocks=[]))


# ------------------------------------------------ réécriture à l'émission


def _rewrite(name, arguments, refs=None):
    [tool] = [t for t in STRUCTURE_TOOLS if t.name == name]
    return tool.rewrite_args(arguments, refs or _refs())


def test_rewrite_args_add_resolved_ids() -> None:
    added = _rewrite(
        PROPOSE_BLOCK_ADD,
        {"type": "document", "title": "Fiche", "after_ref": "B2", "resource_ref": "R1"},
    )
    assert added["after_id"] == str(EXERCISE_ID)
    assert added["resource_id"] == str(RESOURCE_ID) and added["resource_name"] == "cours.pdf"
    assert added["module_id"] is None and added["module_title"] is None
    assert added["after_ref"] == "B2"  # args d'origine conservés

    module = _rewrite(PROPOSE_BLOCK_ADD, {"type": "module", "title": "A", "module_ref": "M1"})
    assert module["module_id"] == str(MODULE_ID) and module["after_id"] is None

    deleted = _rewrite(PROPOSE_BLOCK_DELETE, {"target_ref": "B1"})
    assert deleted["block_id"] == str(BLOCK_ID) and deleted["target_title"] == "Intro"

    reordered = _rewrite(PROPOSE_BLOCKS_REORDER, {"order": ["B3", "B1", "B2"]})
    assert reordered["block_ids"] == [str(THIRD_ID), str(BLOCK_ID), str(EXERCISE_ID)]


def test_rewrite_args_leave_unresolved_args_untouched() -> None:
    assert _rewrite(PROPOSE_BLOCK_DELETE, {"target_ref": "B9"}) == {"target_ref": "B9"}
    assert _rewrite(PROPOSE_BLOCKS_REORDER, {"order": ["B1"]}) == {"order": ["B1"]}
