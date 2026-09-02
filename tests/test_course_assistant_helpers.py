"""Tests des helpers purs de l'assistant de cours (contexte, citations,
replay, tools, gate HITL) — aucun réseau, DB ni S3 (fakes en mémoire)."""

import io
import time
import uuid
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from app.core.ai import AIToolCall
from app.course_assistant import hitl
from app.course_assistant import tools as tools_module
from app.course_assistant.context import (
    build_course_context,
    build_refs,
    extract_sources,
    format_block,
    format_module,
    replay_messages,
)
from app.course_assistant.editing import base as editing_base
from app.course_assistant.editing.block_exercise import (
    BLOCK_EXERCISE,
    PROPOSE_QUESTION_ADD,
    PROPOSE_QUESTION_DELETE,
    PROPOSE_QUESTION_EDIT,
    PROPOSE_STATEMENT_EDIT,
    QUESTION_MAX_CHARS,
    QUESTIONS_MAX,
    STATEMENT_MAX_CHARS,
)
from app.course_assistant.editing.block_text import (
    BLOCK_TEXT,
    PROPOSAL_MAX_CHARS,
    PROPOSE_BLOCK_EDIT,
)
from app.course_assistant.tools import (
    IMAGE_MAX_BYTES,
    PDF_MAX_BYTES,
    PDF_MAX_PAGES,
    build_tool_executor,
    build_tool_specs,
    read_image_sync,
    read_pdf_sync,
)

BLOCK_ID = uuid.uuid4()
RESOURCE_ID = uuid.uuid4()
MODULE_ID = uuid.uuid4()
Q1, Q2 = uuid.uuid4(), uuid.uuid4()
EXERCISE_TOOLS = {
    PROPOSE_STATEMENT_EDIT,
    PROPOSE_QUESTION_EDIT,
    PROPOSE_QUESTION_ADD,
    PROPOSE_QUESTION_DELETE,
}


def _block(**overrides):
    defaults = dict(
        id=BLOCK_ID,
        type="text",
        title="Introduction",
        description=None,
        content={"markdown": "Le théorème de Pythagore."},
        resource_id=None,
        module_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _resource(**overrides):
    defaults = dict(
        id=RESOURCE_ID,
        original_name="cours.pdf",
        type="document",
        mime="application/pdf",
        size=1234,
        status="available",
        s3_key="courses/x/resources/y/cours.pdf",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _module(**overrides):
    defaults = dict(
        id=MODULE_ID,
        title="Balance interactive",
        html="<div id=\"scale\"></div>",
        css="#scale { color: red; }",
        js="console.log('go');",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


_COURSE = SimpleNamespace(title="Géométrie", description="Cours de 4e")


def _exercise(**overrides):
    """Bloc exercice à deux questions (ids uuid, stables)."""
    defaults = dict(
        type="exercise",
        title="Exercice 1",
        content={
            "statement": "Soit $x$ un réel.",
            "questions": [
                {"id": str(Q1), "statement": "Calculer $x^2$.", "type": "free_text",
                 "expected_answer": "x au carré"},
                {"id": str(Q2), "statement": "Conclure.", "type": "free_text",
                 "expected_answer": ""},
            ],
        },
    )
    defaults.update(overrides)
    return _block(**defaults)


def _refs(blocks=None, resources=None, modules=None, focus_block=None, question_refs=None):
    return build_refs(
        blocks if blocks is not None else [_block()],
        resources if resources is not None else [_resource()],
        modules if modules is not None else [_module()],
        focus_block=focus_block,
        question_refs=question_refs,
    )


# ---------------------------------------------------------------- contexte


def test_format_block_exercise_includes_expected_answer() -> None:
    block = _block(
        type="exercise",
        title=None,
        content={
            "statement": "Résoudre.",
            "questions": [
                {"id": "q1", "statement": "2+2 ?", "type": "free_text", "expected_answer": "4"}
            ],
        },
    )
    others = [_block(id=uuid.uuid4()), _block(id=uuid.uuid4())]
    text = format_block(block, _refs(blocks=[*others, block]))
    assert "### Bloc 3 — Exercice (ref: B3)" in text
    assert str(block.id) not in text  # jamais d'UUID côté modèle
    assert "2+2 ?" in text
    assert "Réponse attendue (corrigé du professeur) : 4" in text


def test_format_block_exercise_shows_question_refs_never_ids() -> None:
    exercise = _exercise()
    # Bloc édité (focus) : les questions portent leur référence courte.
    focused = format_block(exercise, _refs(blocks=[exercise], focus_block=exercise))
    assert "**Question 1** (ref: Q1) : Calculer $x^2$." in focused
    assert "**Question 2** (ref: Q2) : Conclure." in focused
    assert "Réponse attendue (corrigé du professeur) : x au carré" in focused
    assert str(Q1) not in focused and str(Q2) not in focused
    # Hors édition : ni référence ni id (aucun tool ne consomme l'id).
    plain = format_block(exercise, _refs(blocks=[exercise]))
    assert "**Question 1** : Calculer $x^2$." in plain
    assert "(ref: Q" not in plain and "(id:" not in plain
    assert str(Q1) not in plain
    # Aucune question : mention explicite.
    empty = _exercise(content={"statement": "Sujet seul.", "questions": []})
    assert "(aucune question)" in format_block(empty, _refs(blocks=[empty]))


def test_build_refs_numbers_questions_of_the_edited_exercise_only() -> None:
    exercise = _exercise()
    assert _refs(blocks=[exercise], focus_block=exercise).refs("question") == ["Q1", "Q2"]
    assert _refs(blocks=[exercise]).refs("question") == []  # pas de focus
    text = _block()
    assert _refs(blocks=[text], focus_block=text).refs("question") == []  # focus texte
    # Numérotation du tour rejouée (reprise) : la Q1 du mapping a disparu, sa
    # référence est libérée sans être réattribuée ; la question hors mapping
    # (Q1 réelle) devient Q3.
    mapping = {"Q1": str(uuid.uuid4()), "Q2": str(Q2)}
    replayed = _refs(blocks=[exercise], focus_block=exercise, question_refs=mapping)
    assert replayed.refs("question") == ["Q2", "Q3"]


def test_format_block_module_names_pointed_module() -> None:
    block = _block(type="module", title="Manip", content={}, module_id=MODULE_ID)
    text = format_block(block, _refs(blocks=[block]))
    assert "Module interactif pointé : « Balance interactive » (ref: M1)" in text
    assert "read_module" in text
    assert str(MODULE_ID) not in text
    # Module absent de l'instantané (supprimé) : mention explicite, sans plantage.
    orphan = format_block(block, _refs(blocks=[block], modules=[]))
    assert "introuvable dans la bibliothèque" in orphan


def test_format_block_document_names_pointed_resource() -> None:
    block = _block(type="document", content={"caption": "Le sujet"}, resource_id=RESOURCE_ID)
    text = format_block(block, _refs(blocks=[block]))
    assert "Le sujet" in text
    assert "Ressource pointée : « cours.pdf » (ref: R1)" in text


def test_format_module_code_blocks_and_cap() -> None:
    text = format_module(_module(css=""), _refs())
    assert "Balance interactive (ref: M1)" in text
    assert "```html\n<div id=\"scale\"></div>\n```" in text
    assert "**CSS** : (vide)" in text
    assert "```javascript\nconsole.log('go');\n```" in text
    assert "tronqué" not in text

    capped = format_module(_module(js="x" * 5000), _refs(), max_chars=500)
    assert len(capped) < 600
    assert capped.endswith("[Module tronqué : plafond de lecture atteint]")


def test_build_course_context_full_mode() -> None:
    context = build_course_context(_COURSE, _refs(modules=[]))
    assert "# Cours : Géométrie" in context
    assert "### Bloc 1 — Introduction (ref: B1)" in context
    assert "Le théorème de Pythagore." in context
    assert "cours.pdf (ref: R1" in context
    assert "(aucun module)" in context
    assert "oc-block:<ref>" in context  # consigne de citation
    assert str(BLOCK_ID) not in context and str(RESOURCE_ID) not in context
    for tool in ("read_block", "read_resource_pdf", "read_resource_image", "read_module"):
        assert f"`{tool}`" in context


def test_build_course_context_names_modules_of_module_blocks() -> None:
    block = _block(type="module", title=None, content={}, module_id=MODULE_ID)
    context = build_course_context(_COURSE, _refs(blocks=[block]))
    assert "Module interactif pointé : « Balance interactive » (ref: M1)" in context
    assert "- Balance interactive (ref: M1)" in context


def test_build_course_context_summary_mode() -> None:
    blocks = [
        _block(id=uuid.uuid4(), content={"markdown": "mot " * 500}) for _ in range(5)
    ]
    context = build_course_context(_COURSE, _refs(blocks=blocks), max_chars=2000)
    assert "extraits" in context
    assert "read_block" in context
    # Chaque en-tête de bloc reste présent, le corps est tronqué.
    for i in range(1, 6):
        assert f"(ref: B{i})" in context
    assert "mot " * 100 not in context


def test_build_course_context_block_text_focus() -> None:
    other = _block(id=uuid.uuid4(), title="Suite", content={"markdown": "La suite du cours."})
    focus = _block()
    context = build_course_context(
        _COURSE, _refs(blocks=[focus, other]), focus_block=focus, edit=BLOCK_TEXT
    )
    # Mission et règles d'édition substituées à la mission « course ».
    assert "un bloc de texte de son cours" in context
    assert "propose_block_edit" in context
    assert "L'appel est BLOQUANT" in context
    # Syntaxes d'édition déclarées (le modèle connaît tous ses outils).
    assert "```mermaid" in context
    assert "```tikz" in context
    assert "```geogebra" in context
    assert "```jsxgraph" in context
    assert "oc-module:<cible>" in context
    # Bloc édité en entier dans la section dédiée, pointeur dans la liste.
    assert "## Bloc en cours d'édition" in context
    assert context.count("Le théorème de Pythagore.") == 1
    assert "(bloc en cours d'édition — contenu complet dans la section dédiée ci-dessus)" in context
    # Le reste du cours reste rendu.
    assert "La suite du cours." in context
    # Les syntaxes/règles d'édition ne polluent pas le contexte « course ».
    course_context = build_course_context(_COURSE, _refs(blocks=[focus, other]))
    assert "```tikz" not in course_context
    assert "propose_block_edit" not in course_context


def test_build_course_context_block_exercise_focus() -> None:
    exercise = _exercise()
    other = _block(id=uuid.uuid4(), title="Suite", content={"markdown": "La suite du cours."})
    context = build_course_context(
        _COURSE,
        _refs(blocks=[exercise, other], focus_block=exercise),
        focus_block=exercise,
        edit=BLOCK_EXERCISE,
    )
    assert "qui édite un exercice de son cours" in context
    for tool in EXERCISE_TOOLS:
        assert f"`{tool}`" in context
    assert "Chaque appel est BLOQUANT" in context
    assert "UNE opération par appel" in context
    assert "```mermaid" in context  # syntaxes d'édition déclarées
    assert "## Bloc en cours d'édition" in context
    assert "(ref: Q1)" in context and "(ref: Q2)" in context
    assert str(Q1) not in context and str(Q2) not in context
    assert "propose_block_edit" not in context
    assert "La suite du cours." in context
    # Rien de tout cela dans le contexte « course » ni dans « block_text ».
    course_context = build_course_context(_COURSE, _refs(blocks=[exercise, other]))
    assert "propose_question_edit" not in course_context
    assert "(ref: Q1)" not in course_context
    text_context = build_course_context(
        _COURSE, _refs(blocks=[other]), focus_block=other, edit=BLOCK_TEXT
    )
    assert "propose_question_edit" not in text_context


def test_build_course_context_requires_edit_with_focus() -> None:
    """``focus_block`` et ``edit`` vont ensemble (contexte d'édition)."""
    focus = _block()
    with pytest.raises(ValueError):
        build_course_context(_COURSE, _refs(blocks=[focus]), focus_block=focus)
    with pytest.raises(ValueError):
        build_course_context(_COURSE, _refs(blocks=[focus]), edit=BLOCK_TEXT)


def test_build_course_context_block_text_focus_full_even_in_summary_mode() -> None:
    others = [
        _block(id=uuid.uuid4(), content={"markdown": "mot " * 500}) for _ in range(5)
    ]
    focus = _block(content={"markdown": "CONTENU ÉDITÉ INTÉGRAL. " * 40})
    context = build_course_context(
        _COURSE,
        _refs(blocks=[focus, *others]),
        max_chars=2000,
        focus_block=focus,
        edit=BLOCK_TEXT,
    )
    assert "extraits" in context
    # Le bloc édité reste rendu EN ENTIER (jamais excerpté), une seule fois.
    assert context.count("CONTENU ÉDITÉ INTÉGRAL. " * 40) == 1


# ------------------------------------------------------------------- specs


def test_tool_specs_enum_follows_snapshot() -> None:
    resources = [
        _resource(id=uuid.uuid4()),  # R1 : PDF disponible
        _resource(id=uuid.uuid4(), type="image", mime="image/png"),  # R2 : image
        _resource(id=uuid.uuid4(), status="pending"),  # R3 : PDF non confirmé
    ]
    specs = {s.name: s for s in build_tool_specs(_refs(resources=resources))}
    assert set(specs) == {"read_block", "read_resource_pdf", "read_resource_image", "read_module"}
    assert specs["read_block"].parameters["properties"]["block_ref"]["enum"] == ["B1"]
    assert specs["read_block"].parameters["required"] == ["block_ref"]
    assert specs["read_resource_pdf"].parameters["properties"]["resource_ref"]["enum"] == ["R1"]
    assert specs["read_resource_image"].parameters["properties"]["resource_ref"]["enum"] == ["R2"]
    assert specs["read_module"].parameters["properties"]["module_ref"]["enum"] == ["M1"]

    # Liste vide : pas d'enum (schéma invalide chez certains providers).
    empty = {s.name: s for s in build_tool_specs(_refs(blocks=[], resources=[], modules=[]))}
    for name, param in (
        ("read_block", "block_ref"),
        ("read_resource_pdf", "resource_ref"),
        ("read_module", "module_ref"),
    ):
        assert "enum" not in empty[name].parameters["properties"][param]


def test_tool_specs_propose_only_on_demand() -> None:
    assert PROPOSE_BLOCK_EDIT not in {s.name for s in build_tool_specs(_refs())}
    specs = {s.name: s for s in build_tool_specs(_refs(), edit=BLOCK_TEXT)}
    spec = specs[PROPOSE_BLOCK_EDIT]
    assert spec.parameters["required"] == ["new_markdown"]
    assert set(spec.parameters["properties"]) == {"new_markdown", "summary"}
    assert "ATTEND sa décision" in spec.description
    assert not EXERCISE_TOOLS & set(specs)  # les tools exercice n'y sont pas


def test_tool_specs_block_exercise() -> None:
    exercise = _exercise()
    refs = _refs(blocks=[exercise], focus_block=exercise)
    specs = {s.name: s for s in build_tool_specs(refs, edit=BLOCK_EXERCISE)}
    assert EXERCISE_TOOLS <= set(specs)
    assert PROPOSE_BLOCK_EDIT not in specs
    for spec in (specs[name] for name in EXERCISE_TOOLS):
        assert "ATTEND la décision" in spec.description
        assert "summary" in spec.parameters["properties"]
    assert specs[PROPOSE_STATEMENT_EDIT].parameters["required"] == ["new_statement"]
    edit = specs[PROPOSE_QUESTION_EDIT].parameters
    assert edit["required"] == ["question_ref"]
    assert set(edit["properties"]) == {"question_ref", "statement", "expected_answer", "summary"}
    assert edit["properties"]["question_ref"]["enum"] == ["Q1", "Q2"]
    add = specs[PROPOSE_QUESTION_ADD].parameters
    assert add["required"] == ["statement"]
    assert add["properties"]["after_ref"]["enum"] == ["Q1", "Q2"]
    delete = specs[PROPOSE_QUESTION_DELETE].parameters
    assert delete["required"] == ["question_ref"]
    assert delete["properties"]["question_ref"]["enum"] == ["Q1", "Q2"]
    # Aucune question numérotée : pas d'enum (schéma invalide sinon).
    no_questions = {s.name: s for s in build_tool_specs(_refs(), edit=BLOCK_EXERCISE)}
    edit_props = no_questions[PROPOSE_QUESTION_EDIT].parameters["properties"]
    assert "enum" not in edit_props["question_ref"]
    assert "enum" not in no_questions[PROPOSE_QUESTION_ADD].parameters["properties"]["after_ref"]


# ---------------------------------------------------------------- citations


def test_extract_sources_filters_hallucinations() -> None:
    other = uuid.uuid4()
    content = (
        f"Voir [Intro](oc-block:{BLOCK_ID}) et [Faux](oc-block:{other}) "
        f"et [PDF](oc-resource:{RESOURCE_ID}) — encore oc-block:{BLOCK_ID}."
    )
    sources = extract_sources(content, {BLOCK_ID}, {RESOURCE_ID})
    assert sources == {"blocks": [str(BLOCK_ID)], "resources": [str(RESOURCE_ID)]}


def test_extract_sources_empty() -> None:
    assert extract_sources("Rien à citer", {BLOCK_ID}, set()) == {
        "blocks": [],
        "resources": [],
    }


# ---------------------------------------------------------------- replay


def _row(role, content="", tool_calls=None, tool_call_id=None, is_error=False, provider=None):
    return SimpleNamespace(
        role=role,
        content=content,
        tool_calls=tool_calls or [],
        tool_call_id=tool_call_id,
        is_error=is_error,
        provider=provider,
    )


_CALL = {"id": "call_1", "name": "read_block", "arguments": {"block_id": "b1"}}


def test_replay_native_tool_round_same_provider() -> None:
    rows = [
        _row("user", "Question"),
        _row("assistant", "", tool_calls=[_CALL], provider="ollama"),
        _row("tool", "CONTENU", tool_call_id="call_1"),
        _row("assistant", "Réponse", provider="ollama"),
    ]
    messages, truncated = replay_messages(rows, "ollama")
    assert not truncated
    assert [m.role for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[1].tool_calls[0].id == "call_1"
    assert messages[2].tool_call_id == "call_1"


def test_replay_folds_other_provider_round() -> None:
    rows = [
        _row("user", "Question"),
        _row("assistant", "", tool_calls=[_CALL], provider="mistral"),
        _row("tool", "CONTENU " * 200, tool_call_id="call_1"),
        _row("assistant", "Réponse", provider="mistral"),
    ]
    messages, _ = replay_messages(rows, "ollama")
    assert [m.role for m in messages] == ["user", "assistant", "assistant"]
    folded = messages[1]
    assert folded.tool_calls is None
    assert "read_block" in folded.content
    assert "…" in folded.content  # résultat replié tronqué


def test_replay_folds_incomplete_round() -> None:
    """Round sans résultats persistés (erreur mid-round) : jamais rejoué natif."""
    rows = [
        _row("user", "Question"),
        _row("assistant", "Partiel", tool_calls=[_CALL], provider="ollama"),
    ]
    messages, _ = replay_messages(rows, "ollama")
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].tool_calls is None
    assert "Partiel" in messages[1].content


def test_replay_truncates_at_round_boundary() -> None:
    """La fenêtre écarte les tours tool orphelins de tête."""
    rows = [
        _row("user", f"Q{i}") for i in range(10)
    ] + [
        _row("assistant", "", tool_calls=[_CALL], provider="ollama"),
        _row("tool", "CONTENU", tool_call_id="call_1"),
        _row("assistant", "Fin", provider="ollama"),
    ]
    messages, truncated = replay_messages(rows, "ollama", limit=2)
    assert truncated
    # Fenêtre de 2 : [tool, assistant] → le tool orphelin est écarté.
    assert [m.role for m in messages] == ["assistant"]
    assert messages[0].content == "Fin"


# ---------------------------------------------------------------- read_pdf_sync


class _FakeStorage:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read_object_into(self, s3_key: str, fileobj) -> None:
        fileobj.write(self.payload)


def _blank_pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_read_pdf_sync_blank_pages() -> None:
    content = read_pdf_sync(_FakeStorage(_blank_pdf(2)), "clé")
    assert content == ""


def test_read_pdf_sync_page_cap() -> None:
    content = read_pdf_sync(_FakeStorage(_blank_pdf(PDF_MAX_PAGES + 5)), "clé")
    assert "[Document tronqué" in content


# ---------------------------------------------------------------- read_image_sync


def test_read_image_sync_base64() -> None:
    assert read_image_sync(_FakeStorage(b"\x89PNG"), "clé") == "iVBORw=="


def test_read_image_sync_rejects_oversized_object() -> None:
    with pytest.raises(ValueError):
        read_image_sync(_FakeStorage(b"x" * (IMAGE_MAX_BYTES + 1)), "clé")


# ---------------------------------------------------------------- exécuteur


def _executor(storage=None, blocks=None, resources=None, modules=None):
    return build_tool_executor(storage or _FakeStorage(b""), _refs(blocks, resources, modules))


@pytest.mark.anyio
@pytest.mark.parametrize("raw", ["B1", "1", str(BLOCK_ID), "introduction"])
async def test_executor_read_block_ok(raw) -> None:
    result = await _executor()(AIToolCall(name="read_block", arguments={"block_ref": raw}))
    assert not result.is_error
    assert "### Bloc 1 — Introduction (ref: B1)" in result.content
    assert "Le théorème de Pythagore." in result.content


@pytest.mark.anyio
@pytest.mark.parametrize("raw", ["pas-une-ref", str(uuid.uuid4()), "B7"])
async def test_executor_read_block_not_found_lists_candidates(raw) -> None:
    result = await _executor()(AIToolCall(name="read_block", arguments={"block_ref": raw}))
    assert result.is_error
    assert "introuvable" in result.content
    assert "B1 — Introduction" in result.content  # le modèle peut se corriger


@pytest.mark.anyio
async def test_executor_read_block_missing_argument() -> None:
    result = await _executor()(AIToolCall(name="read_block", arguments={}))
    assert result.is_error
    assert "non précisé" in result.content


@pytest.mark.anyio
async def test_executor_pdf_rejections() -> None:
    resources = [
        _resource(id=uuid.uuid4(), mime="image/png"),
        _resource(id=uuid.uuid4(), size=PDF_MAX_BYTES + 1),
        _resource(id=uuid.uuid4(), status="pending"),
    ]
    executor = _executor(resources=resources)

    result = await executor(
        AIToolCall(name="read_resource_pdf", arguments={"resource_ref": str(uuid.uuid4())})
    )
    assert result.is_error
    assert "introuvable" in result.content
    # Seuls les PDF disponibles sont proposés (R2, même trop gros) — ni l'image ni le pending.
    assert "R2 — cours.pdf" in result.content
    assert "R1 —" not in result.content and "R3 —" not in result.content

    for ref, needle in zip(
        ("R1", "R2", "R3"), ["pas un PDF", "volumineux", "pas encore disponible"], strict=True
    ):
        result = await executor(
            AIToolCall(name="read_resource_pdf", arguments={"resource_ref": ref})
        )
        assert result.is_error
        assert needle in result.content


@pytest.mark.anyio
async def test_executor_pdf_corrupt_file() -> None:
    executor = _executor(storage=_FakeStorage(b"pas un pdf du tout"))
    result = await executor(
        AIToolCall(name="read_resource_pdf", arguments={"resource_ref": "R1"})
    )
    assert result.is_error
    assert "illisible" in result.content


@pytest.mark.anyio
@pytest.mark.parametrize("raw", ["R1", "cours.pdf", str(RESOURCE_ID)])
async def test_executor_pdf_success(monkeypatch: pytest.MonkeyPatch, raw) -> None:
    monkeypatch.setattr(tools_module, "read_pdf_sync", lambda storage, key: "TEXTE EXTRAIT")
    result = await _executor()(
        AIToolCall(name="read_resource_pdf", arguments={"resource_ref": raw})
    )
    assert not result.is_error
    assert result.content == "TEXTE EXTRAIT"


@pytest.mark.anyio
async def test_executor_read_block_module_shows_title() -> None:
    block = _block(type="module", title="Manip", content={}, module_id=MODULE_ID)
    result = await _executor(blocks=[block])(
        AIToolCall(name="read_block", arguments={"block_ref": "B1"})
    )
    assert not result.is_error
    assert "« Balance interactive » (ref: M1)" in result.content


@pytest.mark.anyio
async def test_executor_read_module() -> None:
    executor = _executor()
    result = await executor(AIToolCall(name="read_module", arguments={"module_ref": "M1"}))
    assert not result.is_error
    assert "```html" in result.content
    assert "console.log('go');" in result.content
    assert result.image is None

    missing = await executor(
        AIToolCall(name="read_module", arguments={"module_ref": str(uuid.uuid4())})
    )
    assert missing.is_error
    assert "introuvable" in missing.content
    assert "M1 — Balance interactive" in missing.content


@pytest.mark.anyio
async def test_executor_image_rejections() -> None:
    resources = [
        _resource(id=uuid.uuid4(), type="image", mime="image/svg+xml"),
        _resource(id=uuid.uuid4(), type="image", mime="image/png", size=IMAGE_MAX_BYTES + 1),
        _resource(id=uuid.uuid4(), type="image", mime="image/png", status="pending"),
    ]
    executor = _executor(resources=resources)
    for ref, needle in zip(
        ("R1", "R2", "R3"), ["pas une image", "volumineuse", "pas encore disponible"], strict=True
    ):
        result = await executor(
            AIToolCall(name="read_resource_image", arguments={"resource_ref": ref})
        )
        assert result.is_error
        assert needle in result.content
        assert result.image is None


@pytest.mark.anyio
async def test_executor_image_success_attaches_base64() -> None:
    resource = _resource(
        type="image", mime="image/png", original_name="figure.png", size=4, s3_key="k/figure.png"
    )
    executor = _executor(storage=_FakeStorage(b"\x89PNG"), resources=[resource])
    result = await executor(
        AIToolCall(name="read_resource_image", arguments={"resource_ref": "figure"})  # titre proche
    )
    assert not result.is_error
    assert "figure.png" in result.content
    assert "non conservée" in result.content  # note rejouée aux tours suivants
    assert result.image is not None
    assert result.image.mime_type == "image/png"
    assert result.image.data == "iVBORw=="
    assert "(ref: R1)" in result.image.caption


@pytest.mark.anyio
async def test_executor_image_storage_failure() -> None:
    class _BrokenStorage:
        def read_object_into(self, s3_key, fileobj):
            raise RuntimeError("S3 down")

    resource = _resource(type="image", mime="image/jpeg")
    result = await _executor(storage=_BrokenStorage(), resources=[resource])(
        AIToolCall(name="read_resource_image", arguments={"resource_ref": "R1"})
    )
    assert result.is_error
    assert "impossible" in result.content


@pytest.mark.anyio
async def test_executor_unknown_tool() -> None:
    result = await _executor()(AIToolCall(name="hack", arguments={}))
    assert result.is_error


# ------------------------------------------------- propose_block_edit (HITL)


def _propose_executor():
    return build_tool_executor(_FakeStorage(b""), _refs(), edit=BLOCK_TEXT)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ({"accepted": True}, "ACCEPTÉ la proposition et l'a appliquée"),
        ({"accepted": False}, "REJETÉ la proposition"),
        (
            {"accepted": False, "comment": "Trop long, condensez."},
            "Son commentaire : Trop long, condensez.",
        ),
    ],
)
async def test_executor_propose_returns_the_decision(monkeypatch, decision, expected) -> None:
    """Le résultat du tool EST la décision du professeur — ``agent_interrupt``
    est mocké ici (le vrai fige le run LangGraph : l'aller-retour complet est
    couvert par ``test_ai_agent.py``) ; le payload de l'interrupt porte l'id
    d'appel (clé de la reprise)."""
    seen: list[dict] = []
    monkeypatch.setattr(
        editing_base, "agent_interrupt", lambda payload: seen.append(payload) or decision
    )
    result = await _propose_executor()(
        AIToolCall(id="call_p", name=PROPOSE_BLOCK_EDIT, arguments={"new_markdown": "# Nouveau"})
    )
    assert not result.is_error
    assert expected in result.content
    assert seen == [{"tool_call_id": "call_p"}]


@pytest.mark.anyio
async def test_executor_propose_validates_before_interrupting(monkeypatch) -> None:
    """Args invalides : échec immédiat, JAMAIS d'interrupt (aucun run figé)."""
    monkeypatch.setattr(
        editing_base,
        "agent_interrupt",
        lambda payload: pytest.fail("interrupt inattendu sur des args invalides"),
    )
    executor = _propose_executor()

    missing = await executor(AIToolCall(id="c1", name=PROPOSE_BLOCK_EDIT, arguments={}))
    assert missing.is_error
    assert "new_markdown" in missing.content

    not_a_string = await executor(
        AIToolCall(id="c2", name=PROPOSE_BLOCK_EDIT, arguments={"new_markdown": 42})
    )
    assert not_a_string.is_error

    too_long = await executor(
        AIToolCall(
            id="c3",
            name=PROPOSE_BLOCK_EDIT,
            arguments={"new_markdown": "x" * (PROPOSAL_MAX_CHARS + 1)},
        )
    )
    assert too_long.is_error
    assert "plafond" in too_long.content


@pytest.mark.anyio
async def test_executor_propose_block_edit_absent_by_default() -> None:
    """Hors contexte d'édition, le tool n'existe pas (outil inconnu)."""
    result = await _executor()(
        AIToolCall(name=PROPOSE_BLOCK_EDIT, arguments={"new_markdown": "x"})
    )
    assert result.is_error
    assert "inconnu" in result.content


# ------------------------------------ tools de proposition d'un exercice (HITL)


def _exercise_executor(exercise=None, question_refs=None):
    block = exercise or _exercise()
    refs = _refs(blocks=[block], focus_block=block, question_refs=question_refs)
    return build_tool_executor(_FakeStorage(b""), refs, edit=BLOCK_EXERCISE), refs


def _no_interrupt(monkeypatch) -> None:
    monkeypatch.setattr(
        editing_base,
        "agent_interrupt",
        lambda payload: pytest.fail("interrupt inattendu sur des args invalides"),
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments", "accepted_needle"),
    [
        (PROPOSE_STATEMENT_EDIT, {"new_statement": "# Sujet"}, "appliquée au sujet"),
        (PROPOSE_QUESTION_EDIT, {"question_ref": "Q2", "expected_answer": "42"},
         "appliquée à la question"),
        (PROPOSE_QUESTION_ADD, {"statement": "Nouvelle ?"}, "a été ajoutée"),
        (PROPOSE_QUESTION_DELETE, {"question_ref": "q1"}, "a été supprimée"),
    ],
)
async def test_executor_exercise_tools_return_the_decision(
    monkeypatch, name, arguments, accepted_needle
) -> None:
    """Le résultat de chaque tool EST la décision ; le payload de l'interrupt
    porte l'id d'appel (clé de la reprise)."""
    seen: list[dict] = []
    decisions = iter(
        [{"accepted": True}, {"accepted": False, "comment": "Pas comme ça."}]
    )
    monkeypatch.setattr(
        editing_base,
        "agent_interrupt",
        lambda payload: seen.append(payload) or next(decisions),
    )
    executor, _ = _exercise_executor()
    accepted = await executor(AIToolCall(id="call_p", name=name, arguments=arguments))
    assert not accepted.is_error
    assert "ACCEPTÉ" in accepted.content and accepted_needle in accepted.content
    rejected = await executor(AIToolCall(id="call_p", name=name, arguments=arguments))
    assert not rejected.is_error
    assert "REJETÉ" in rejected.content and "l'exercice est inchangé" in rejected.content
    assert "Son commentaire : Pas comme ça." in rejected.content
    assert seen == [{"tool_call_id": "call_p"}] * 2


@pytest.mark.anyio
async def test_executor_exercise_add_tells_the_model_to_reread_the_block(monkeypatch) -> None:
    monkeypatch.setattr(editing_base, "agent_interrupt", lambda payload: {"accepted": True})
    executor, _ = _exercise_executor()
    arguments = {"statement": "?", "after_ref": "Q1"}
    result = await executor(AIToolCall(id="c", name=PROPOSE_QUESTION_ADD, arguments=arguments))
    assert "read_block" in result.content


@pytest.mark.anyio
async def test_executor_exercise_validates_before_interrupting(monkeypatch) -> None:
    """Args invalides : échec immédiat et actionnable, JAMAIS d'interrupt."""
    _no_interrupt(monkeypatch)
    executor, _ = _exercise_executor()

    async def failing(name, arguments):
        result = await executor(AIToolCall(id="c", name=name, arguments=arguments))
        assert result.is_error
        return result.content

    assert "new_statement" in await failing(PROPOSE_STATEMENT_EDIT, {})
    assert "chaîne attendue" in await failing(PROPOSE_STATEMENT_EDIT, {"new_statement": 42})
    assert "plafond" in await failing(
        PROPOSE_STATEMENT_EDIT, {"new_statement": "x" * (STATEMENT_MAX_CHARS + 1)}
    )
    # Question inconnue / ambiguë : les candidats sont listés.
    unknown = await failing(PROPOSE_QUESTION_EDIT, {"question_ref": "Q9", "statement": "x"})
    assert "introuvable" in unknown and "Q1 — Calculer $x^2$." in unknown
    assert "non précisé" in await failing(PROPOSE_QUESTION_EDIT, {"statement": "x"})
    assert "Rien à modifier" in await failing(PROPOSE_QUESTION_EDIT, {"question_ref": "Q1"})
    assert "plafond" in await failing(
        PROPOSE_QUESTION_EDIT,
        {"question_ref": "Q1", "expected_answer": "x" * (QUESTION_MAX_CHARS + 1)},
    )
    assert "statement" in await failing(PROPOSE_QUESTION_ADD, {})
    misplaced = await failing(PROPOSE_QUESTION_ADD, {"statement": "?", "after_ref": "Q7"})
    assert "introuvable" in misplaced
    assert "introuvable" in await failing(PROPOSE_QUESTION_DELETE, {"question_ref": "Q7"})
    assert "non précisé" in await failing(PROPOSE_QUESTION_DELETE, {})


@pytest.mark.anyio
async def test_executor_exercise_add_respects_question_cap(monkeypatch) -> None:
    _no_interrupt(monkeypatch)
    full = _exercise(
        content={
            "statement": "",
            "questions": [
                {"id": str(uuid.uuid4()), "statement": f"Q{i}", "type": "free_text",
                 "expected_answer": ""}
                for i in range(QUESTIONS_MAX)
            ],
        }
    )
    executor, _ = _exercise_executor(exercise=full)
    result = await executor(
        AIToolCall(id="c", name=PROPOSE_QUESTION_ADD, arguments={"statement": "?"})
    )
    assert result.is_error
    assert "plafond" in result.content


@pytest.mark.anyio
async def test_executor_exercise_tools_absent_in_block_text_context() -> None:
    result = await _propose_executor()(
        AIToolCall(name=PROPOSE_QUESTION_DELETE, arguments={"question_ref": "Q1"})
    )
    assert result.is_error
    assert "inconnu" in result.content


def test_rewrite_exercise_args_adds_ids_and_rewrites_content_links() -> None:
    _, refs = _exercise_executor()
    rewrite = {tool.name: tool.rewrite_args for tool in BLOCK_EXERCISE.tools}

    statement = rewrite[PROPOSE_STATEMENT_EDIT](
        {"new_statement": "Voir [PDF](oc-resource:R1)", "summary": "s"}, refs
    )
    assert statement == {"new_statement": f"Voir [PDF](oc-resource:{RESOURCE_ID})", "summary": "s"}

    edit = rewrite[PROPOSE_QUESTION_EDIT](
        {"question_ref": "q2", "statement": "![f](oc-resource:R1)", "expected_answer": "42"}, refs
    )
    assert edit["question_id"] == str(Q2)
    assert edit["question_ref"] == "q2"  # la référence reste (replay fidèle)
    assert edit["statement"] == f"![f](oc-resource:{RESOURCE_ID})"
    assert edit["expected_answer"] == "42"

    add = rewrite[PROPOSE_QUESTION_ADD]({"statement": "?", "after_ref": "Q1"}, refs)
    assert add["after_id"] == str(Q1)
    assert rewrite[PROPOSE_QUESTION_ADD]({"statement": "?"}, refs)["after_id"] is None

    delete = rewrite[PROPOSE_QUESTION_DELETE]({"question_ref": str(Q1)}, refs)
    assert delete["question_id"] == str(Q1)
    # Référence irrésolue : id None (le handler a déjà refusé l'appel).
    assert rewrite[PROPOSE_QUESTION_DELETE]({"question_ref": "Q9"}, refs)["question_id"] is None
    # Args malformés : laissés tels quels, jamais d'exception.
    assert rewrite[PROPOSE_STATEMENT_EDIT]({"new_statement": 5}, refs) == {"new_statement": 5}


# ----------------------------------------------- registre de reprises (hitl)


def _pending(**overrides) -> hitl.PendingProposal:
    defaults = dict(thread_id="t-1", tool_call_id="call_p", provider="mistral", config=None)
    defaults.update(overrides)
    return hitl.PendingProposal(**defaults)


def test_hitl_pending_carries_optional_question_refs() -> None:
    assert _pending().question_refs is None
    mapping = {"Q1": str(Q1)}
    assert _pending(question_refs=mapping).question_refs == mapping


def test_hitl_register_take_and_mismatch() -> None:
    conversation_id = uuid.uuid4()
    pending = _pending()
    assert hitl.register(conversation_id, pending) is None
    # Mauvais id d'appel : l'entrée n'est PAS consommée.
    assert hitl.take(conversation_id, "autre") is None
    assert hitl.take(conversation_id, "call_p") is pending
    assert hitl.take(conversation_id, "call_p") is None  # consommée


def test_hitl_register_replaces_and_returns_previous() -> None:
    """Une seule reprise par conversation : la nouvelle remplace l'ancienne,
    rendue à l'appelant (qui purge son thread)."""
    conversation_id = uuid.uuid4()
    first = _pending(thread_id="t-1", tool_call_id="c1")
    second = _pending(thread_id="t-2", tool_call_id="c2")
    hitl.register(conversation_id, first)
    assert hitl.register(conversation_id, second) is first
    assert hitl.take(conversation_id, "c2") is second


def test_hitl_expired_entry_is_dropped() -> None:
    conversation_id = uuid.uuid4()
    expired = _pending(created_at=time.time() - hitl.PENDING_TTL_SECONDS - 1)
    hitl.register(conversation_id, expired)
    assert hitl.take(conversation_id, "call_p") is None


def test_hitl_drop() -> None:
    conversation_id = uuid.uuid4()
    pending = _pending()
    hitl.register(conversation_id, pending)
    assert hitl.drop(conversation_id) is pending
    assert hitl.drop(conversation_id) is None
