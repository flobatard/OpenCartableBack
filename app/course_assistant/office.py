"""Extraction du texte des pièces jointes bureautiques (docx, pptx, odt, odp).

Appelé par le tool ``read_attachment`` (:mod:`app.course_assistant.tools`) sur
un fichier DÉJÀ téléchargé depuis S3 : ce module ne fait aucune I/O réseau, il
lit un objet fichier ouvert.

**Imports paresseux** (motif de ``read_pdf_sync``) : ``python-docx``,
``python-pptx`` et ``odfpy`` tirent ``lxml`` et ``Pillow``, qui n'ont rien à
faire en mémoire tant qu'aucune pièce bureautique n'est lue — la doctrine du
projet est de ne rien charger de lourd au boot (contrainte Pi).

**Ce qu'on en tire, et ce qu'on en perd.** Le texte des paragraphes, des
cellules de tableau et des diapositives, dans l'ordre du document. Perdus : la
mise en forme, la structure fine des tableaux, les images, les en-têtes et
pieds de page, les notes. C'est un INDICE pour le modèle, pas une source de
vérité — et le résultat du tool le dit au modèle, pour qu'il ne présente pas
une extraction dégradée comme le document.

**Gardes.** Un fichier OOXML/ODF est un zip : on borne la taille décompressée
DÉCLARÉE avant de laisser une lib l'ouvrir (une archive de 15 Mo peut
décompresser en gigaoctets), et on borne le texte produit. Les parseurs XML
des trois libs sont durcis contre l'expansion d'entités (``resolve_entities``
à faux chez python-docx/python-pptx, ``defusedxml`` chez odfpy) ; le refus
explicite d'un ``DOCTYPE`` reste posé ici — trois lignes qui ne dépendent pas
de la politique d'une lib tierce.
"""

import zipfile

from app.models.ai_attachment import KIND_OFFICE  # noqa: F401 — ré-export de commodité

# Plafond du texte rendu au modèle — aligné sur PDF_MAX_CHARS : ce qui compte
# n'est pas l'octet mais ce que ça pèse en tokens.
OFFICE_MAX_CHARS = 40_000
# Plafond de la taille DÉCOMPRESSÉE déclarée par l'archive (garde zip bomb).
OFFICE_TOTAL_MAX_BYTES = 24 * 1024 * 1024
# Octets de tête scannés à la recherche d'une déclaration d'entité.
_DOCTYPE_SCAN_BYTES = 4096

TRUNCATION_MARKER = "\n\n[Document tronqué : plafond de lecture atteint]"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
ODT_MIME = "application/vnd.oasis.opendocument.text"
ODP_MIME = "application/vnd.oasis.opendocument.presentation"

OFFICE_MIMES = frozenset({DOCX_MIME, PPTX_MIME, ODT_MIME, ODP_MIME})


def _check_archive(fileobj) -> None:
    """Refuse une archive dont le décompressé déclaré dépasse le plafond, ou
    dont une partie XML porte une déclaration d'entité.

    Lève ``ValueError`` (l'appelant traduit en résultat d'outil en erreur).
    ``zipfile.BadZipFile`` remonte telle quelle : le fichier n'est pas une
    archive, donc pas le format annoncé.
    """
    fileobj.seek(0)
    with zipfile.ZipFile(fileobj) as archive:
        total = sum(info.file_size for info in archive.infolist())
        if total > OFFICE_TOTAL_MAX_BYTES:
            raise ValueError("archive trop volumineuse une fois décompressée")
        for info in archive.infolist():
            if not info.filename.endswith(".xml"):
                continue
            with archive.open(info) as part:
                head = part.read(_DOCTYPE_SCAN_BYTES)
            # OOXML et ODF ne portent JAMAIS de DOCTYPE : le refuser ferme
            # d'un coup billion-laughs et les entités externes.
            if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
                raise ValueError("XML à déclaration d'entité refusé")
    fileobj.seek(0)


def _docx_text(fileobj) -> str:
    """Paragraphes puis tableaux, dans l'ordre du document."""
    from docx import Document

    document = Document(fileobj)
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _pptx_text(fileobj) -> str:
    """Une section par diapositive, titrée : sans repère, le texte de plusieurs
    slides se lit comme un seul bloc incohérent."""
    from pptx import Presentation

    presentation = Presentation(fileobj)
    parts: list[str] = []
    for number, slide in enumerate(presentation.slides, start=1):
        lines = [
            shape.text_frame.text
            for shape in slide.shapes
            if shape.has_text_frame and shape.text_frame.text.strip()
        ]
        if lines:
            parts.append(f"## Diapositive {number}\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _odf_text(fileobj) -> str:
    """Titres et paragraphes d'un document OpenDocument (texte ou présentation)."""
    from odf import teletype, text
    from odf.opendocument import load

    document = load(fileobj)
    parts = [
        teletype.extractText(node)
        for kind in (text.H, text.P)
        for node in document.getElementsByType(kind)
    ]
    return "\n".join(part for part in parts if part.strip())


_EXTRACTORS = {
    DOCX_MIME: _docx_text,
    PPTX_MIME: _pptx_text,
    ODT_MIME: _odf_text,
    ODP_MIME: _odf_text,
}


def extract_office_text(fileobj, mime: str) -> str:
    """Texte d'une pièce jointe bureautique ouverte, plafonné et annoté.

    L'en-tête dit au modèle que ce qu'il lit est une extraction dégradée :
    sans lui, il présenterait un tableau aplati comme le tableau du document.

    Lève ``ValueError`` (gabarit refusé, mime inconnu) ou les erreurs des libs
    de lecture — l'appelant les traduit en résultat d'outil en erreur.
    """
    extractor = _EXTRACTORS.get(mime)
    if extractor is None:
        raise ValueError(f"type bureautique non pris en charge : {mime}")
    _check_archive(fileobj)
    content = extractor(fileobj).strip()
    if not content:
        return ""
    truncated = len(content) > OFFICE_MAX_CHARS
    if truncated:
        content = content[:OFFICE_MAX_CHARS]
    header = (
        "[Texte brut extrait du document — mise en forme, structure des "
        "tableaux et images perdues.]"
    )
    return f"{header}\n\n{content}" + (TRUNCATION_MARKER if truncated else "")


__all__ = [
    "KIND_OFFICE",
    "OFFICE_MAX_CHARS",
    "OFFICE_MIMES",
    "OFFICE_TOTAL_MAX_BYTES",
    "extract_office_text",
]
