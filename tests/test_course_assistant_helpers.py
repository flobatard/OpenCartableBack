"""Tests des helpers purs de l'assistant de cours (contexte, citations,
replay, tools) — aucun réseau, DB ni S3 (fakes en mémoire)."""

import io
import uuid
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

from app.core.ai import AIToolCall
from app.course_assistant import tools as tools_module
from app.course_assistant.context import (
    build_course_context,
    build_refs,
    extract_sources,
    format_block,
    format_module,
    replay_messages,
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


def _refs(blocks=None, resources=None, modules=None):
    return build_refs(
        blocks if blocks is not None else [_block()],
        resources if resources is not None else [_resource()],
        modules if modules is not None else [_module()],
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
