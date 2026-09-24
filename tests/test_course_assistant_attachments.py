"""Pièces jointes vues par le modèle : références ``A…``, section du contexte
du tour, et tool ``read_attachment``.

Helpers purs et exécuteur de tools — aucune DB, aucun réseau. Le faux storage
est local (``tests.fakes.FakeStorage`` ne porte pas ``read_object_into``), motif
de ``tests/test_course_assistant_helpers.py``.
"""

import io
import uuid

import pytest
from pypdf import PdfWriter

from app.core.ai import AIToolCall
from app.course_assistant.attachments import attachments_section, human_size
from app.course_assistant.context import build_refs, build_turn_context
from app.course_assistant.tools import (
    READ_ATTACHMENT,
    TEXT_MAX_CHARS,
    build_tool_executor,
    build_tool_specs,
    read_text_sync,
)
from tests.course_assistant_fakes import attachment_row, block_row, course_row, resource_row


class _FakeStorage:
    """Un objet par clé ; ``read_object_into`` est le seul point de contact."""

    def __init__(self, payload_by_key: dict[str, bytes]) -> None:
        self.payloads = payload_by_key

    def read_object_into(self, s3_key: str, fileobj) -> None:
        fileobj.write(self.payloads[s3_key])


def _refs(*attachments):
    return build_refs([block_row()], [resource_row()], [], attachments=list(attachments))


def _call(ref: str) -> AIToolCall:
    return AIToolCall(id="call_1", name=READ_ATTACHMENT, arguments={"attachment_ref": ref})


def _pdf_bytes(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# ─────────────────────────────────────────────
# Références courtes
# ─────────────────────────────────────────────


def test_attachments_are_numbered_with_an_a_prefix():
    first = attachment_row(id=uuid.uuid4(), original_name="a.png")
    second = attachment_row(id=uuid.uuid4(), original_name="b.pdf", kind="pdf")
    refs = _refs(first, second)
    assert refs.refs("attachment") == ["A1", "A2"]
    assert refs.ref_of("attachment", first.id) == "A1"


def test_attachment_refs_do_not_disturb_the_other_kinds():
    """Le genre est nouveau : blocs, ressources et modules gardent B/R/M."""
    refs = _refs(attachment_row())
    assert refs.refs("block") == ["B1"]
    assert refs.refs("resource") == ["R1"]


def test_attachment_resolves_by_ref_and_by_name():
    row = attachment_row(original_name="photo-tableau.png")
    refs = _refs(row)
    assert refs.resolve("attachment", "A1").entry.entity is row
    assert refs.resolve("attachment", "photo-tableau.png").entry.entity is row


def test_unknown_attachment_ref_lists_the_candidates():
    refs = _refs(attachment_row(original_name="photo.png"))
    resolution = refs.resolve("attachment", "A9")
    assert resolution.entry is None
    assert "photo.png" in resolution.error


# ─────────────────────────────────────────────
# Section du contexte du tour
# ─────────────────────────────────────────────


def test_section_is_empty_without_attachments():
    assert attachments_section(_refs(), []) == ""


def test_section_marks_the_attachments_of_this_message():
    fresh, older = uuid.uuid4(), uuid.uuid4()
    refs = _refs(
        attachment_row(id=older, original_name="ancien.pdf", kind="pdf"),
        attachment_row(id=fresh, original_name="neuf.png"),
    )
    section = attachments_section(refs, [fresh])

    assert "neuf.png (ref: A2, image," in section
    assert section.count("jointe à CE message") == 1
    assert "ancien.pdf (ref: A1, PDF," in section
    assert "jointe à un message précédent" in section
    # Le CONTENU n'y est jamais : seulement de quoi décider quoi lire.
    assert "read_attachment" in section


def test_section_without_current_ids_omits_the_origin():
    """Cas du sous-assistant : il reçoit des consignes, pas le message du prof."""
    section = attachments_section(_refs(attachment_row()), None)
    assert "jointe à" not in section


def test_turn_context_puts_attachments_last():
    """Au plus près de la demande du professeur, que ``turn_message`` colle
    après le séparateur — c'est ce qui rend l'appel difficile à manquer."""
    refs = _refs(attachment_row())
    context = build_turn_context(course_row(), refs, new_attachment_ids=[])
    assert context.index("## Pièces jointes") > context.index("## Sommaire du cours")


def test_turn_context_without_attachments_has_no_section():
    context = build_turn_context(course_row(), _refs(), new_attachment_ids=[])
    assert "Pièces jointes" not in context


@pytest.mark.parametrize(
    ("size", "expected"), [(512, "512 o"), (2048, "2.0 ko"), (3_500_000, "3.3 Mo")]
)
def test_human_size(size, expected):
    assert human_size(size) == expected


# ─────────────────────────────────────────────
# Spec du tool
# ─────────────────────────────────────────────


def test_tool_is_absent_without_attachments():
    """Sans pièce jointe, la spec aurait un ``enum`` vide — schéma invalide
    chez certains providers — et décrirait un outil sans objet."""
    names = [s.name for s in build_tool_specs(_refs())]
    assert READ_ATTACHMENT not in names


def test_tool_enum_lists_every_attachment():
    refs = _refs(attachment_row(id=uuid.uuid4()), attachment_row(id=uuid.uuid4()))
    spec = next(s for s in build_tool_specs(refs) if s.name == READ_ATTACHMENT)
    assert spec.parameters["properties"]["attachment_ref"]["enum"] == ["A1", "A2"]
    assert not spec.blocking


# ─────────────────────────────────────────────
# Exécution du tool
# ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_read_attachment_shows_an_image_to_the_model():
    row = attachment_row(kind="image", mime="image/png", s3_key="k-img")
    executor = build_tool_executor(_FakeStorage({"k-img": b"\x89PNG"}), _refs(row))

    result = await executor(_call("A1"))
    assert not result.is_error
    assert result.image is not None
    assert result.image.mime_type == "image/png"
    assert result.image.data == "iVBORw=="
    # Le contenu PERSISTÉ reste une note : l'image n'est jamais rejouée.
    assert "photo.png" in result.content


@pytest.mark.anyio
async def test_read_attachment_extracts_text_from_a_pdf():
    row = attachment_row(kind="pdf", mime="application/pdf", s3_key="k-pdf")
    executor = build_tool_executor(_FakeStorage({"k-pdf": _pdf_bytes()}), _refs(row))

    result = await executor(_call("A1"))
    # Un PDF de pages blanches n'a pas de texte : erreur actionnable, pas un crash.
    assert result.is_error
    assert "texte extractible" in result.content


@pytest.mark.anyio
async def test_read_attachment_reads_a_text_file():
    row = attachment_row(kind="text", mime="text/csv", s3_key="k-csv")
    executor = build_tool_executor(_FakeStorage({"k-csv": b"nom;note\nZoe;18"}), _refs(row))

    result = await executor(_call("A1"))
    assert not result.is_error
    assert result.content == "nom;note\nZoe;18"
    assert result.image is None


@pytest.mark.anyio
async def test_read_attachment_extracts_text_from_an_office_document():
    from docx import Document

    from app.course_assistant.office import DOCX_MIME

    document = Document()
    document.add_paragraph("Barème de l'exercice 3")
    buffer = io.BytesIO()
    document.save(buffer)

    row = attachment_row(kind="office", mime=DOCX_MIME, s3_key="k-docx")
    executor = build_tool_executor(_FakeStorage({"k-docx": buffer.getvalue()}), _refs(row))

    result = await executor(_call("A1"))
    assert not result.is_error
    assert "Barème de l'exercice 3" in result.content
    # Le modèle est prévenu que ce qu'il lit est une extraction dégradée.
    assert "Texte brut extrait du document" in result.content


@pytest.mark.anyio
async def test_read_attachment_never_raises_on_a_storage_failure():
    """Contrat de l'exécuteur : tout échec devient un résultat lisible par le
    modèle, le flux SSE continue."""
    row = attachment_row(kind="text", mime="text/plain", s3_key="absente")
    executor = build_tool_executor(_FakeStorage({}), _refs(row))

    result = await executor(_call("A1"))
    assert result.is_error
    assert "impossible" in result.content


@pytest.mark.anyio
async def test_read_attachment_rejects_an_unconfirmed_upload():
    row = attachment_row(status="pending", kind="text", mime="text/plain", s3_key="k")
    executor = build_tool_executor(_FakeStorage({"k": b"x"}), _refs(row))

    result = await executor(_call("A1"))
    assert result.is_error
    assert "pas encore disponible" in result.content


# ─────────────────────────────────────────────
# Lecture de texte
# ─────────────────────────────────────────────


def test_read_text_sync_decodes_loosely():
    """Un encodage bancal donne un texte imparfait, jamais une exception."""
    assert read_text_sync(_FakeStorage({"k": b"caf\xe9"}), "k") == "caf�"


def test_read_text_sync_truncates_with_a_marker():
    payload = b"a" * (TEXT_MAX_CHARS + 100)
    content = read_text_sync(_FakeStorage({"k": payload}), "k")
    assert "[Document tronqué" in content
    assert len(content) < len(payload)
