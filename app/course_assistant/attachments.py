"""Pièces jointes des chats de l'assistant : whitelist, plafonds, helpers purs.

Module **pur** (aucune I/O, aucun accès DB, feuille du graphe d'imports —
doctrine de :mod:`app.course_assistant.prompts`) : le service, les schémas et
les tools l'importent, jamais l'inverse.

La whitelist :data:`ATTACHMENT_TYPES` est **fermée** (motif
``AVATAR_EXTENSIONS`` de :mod:`app.users.schemas`) : elle donne à la fois les
mimes acceptés, l'extension de la clé S3 et la **famille de traitement**
(``kind``), qui décide du plafond de taille et de la façon dont le tool
``read_attachment`` sert la pièce au modèle — image montrée telle quelle aux
providers à vision, ou texte extrait.

Le mime déclaré au presign est figé dans la signature de l'URL PUT, puis
**re-vérifié** au HEAD de confirmation : une URL présignée PUT ne borne ni la
taille ni le type, seul le HEAD fait foi.
"""

import re

from app.models.ai_attachment import KIND_IMAGE, KIND_OFFICE, KIND_PDF, KIND_TEXT

# Mime → (famille de traitement, extension de la clé S3). Fermée : tout mime
# absent est refusé en 422 par le schéma de presign.
ATTACHMENT_TYPES: dict[str, tuple[str, str]] = {
    "image/png": (KIND_IMAGE, "png"),
    "image/jpeg": (KIND_IMAGE, "jpg"),
    "image/webp": (KIND_IMAGE, "webp"),
    "application/pdf": (KIND_PDF, "pdf"),
    "text/plain": (KIND_TEXT, "txt"),
    "text/markdown": (KIND_TEXT, "md"),
    "text/csv": (KIND_TEXT, "csv"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        KIND_OFFICE,
        "docx",
    ),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        KIND_OFFICE,
        "pptx",
    ),
    "application/vnd.oasis.opendocument.text": (KIND_OFFICE, "odt"),
    "application/vnd.oasis.opendocument.presentation": (KIND_OFFICE, "odp"),
}

# Plafond de taille PAR FAMILLE. Image et PDF sont calés sur les plafonds de
# lecture de app/course_assistant/tools.py (IMAGE_MAX_BYTES, PDF_MAX_BYTES) —
# mêmes chemins de lecture, donc mêmes bornes ; un test garde l'égalité.
ATTACHMENT_MAX_BYTES: dict[str, int] = {
    KIND_IMAGE: 3_500_000,
    KIND_PDF: 20 * 1024 * 1024,
    KIND_TEXT: 1_000_000,
    KIND_OFFICE: 15 * 1024 * 1024,
}

# Pièces jointes d'UN message (422 au-delà, garde-fou du contexte du tour).
MAX_ATTACHMENTS_PER_MESSAGE = 5
# Pièces jointes cumulées d'une conversation : garde-fou de l'``enum`` du tool
# ``read_attachment``, qui les porte toutes (422 au presign au-delà).
MAX_ATTACHMENTS_PER_CONVERSATION = 30

# Libellé de famille, pour les messages d'erreur et le contexte du tour.
KIND_LABELS: dict[str, str] = {
    KIND_IMAGE: "image",
    KIND_PDF: "PDF",
    KIND_TEXT: "texte",
    KIND_OFFICE: "document bureautique",
}

# Caractères conservés dans un nom de fichier sanitizé (le reste → « _ ») —
# même règle que app/resources/service.py, dupliquée pour garder ce module
# pur (cf. TODO.md).
_ALLOWED_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def attachment_kind(mime: str) -> str:
    """Famille de traitement d'un mime de la whitelist.

    Lève ``KeyError`` hors whitelist : les appelants valident le mime en
    amont (``Literal`` du schéma de presign), c'est une garde de programmation.
    """
    return ATTACHMENT_TYPES[mime][0]


def max_bytes_for(mime: str) -> int:
    """Plafond de taille applicable à ce mime (via sa famille)."""
    return ATTACHMENT_MAX_BYTES[attachment_kind(mime)]


def sanitize_attachment_name(name: str) -> str:
    """Nom de fichier sûr pour une clé S3 : basename, chars restreints, borné.

    Neutralise toute tentative de traversée de chemin (``/``, ``\\``) et borne
    la longueur ; la partie ``<attachment_id>/`` de la clé garantit déjà
    l'unicité. Le nom d'origine, lui, est conservé intact en base (c'est ce
    que voient le professeur et le modèle).
    """
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _ALLOWED_NAME_CHARS.sub("_", base).strip("._")
    return (base or "fichier")[:200]


def human_size(size: int) -> str:
    """Taille lisible pour le modèle (jamais un nombre d'octets brut)."""
    value = float(size)
    for unit in ("o", "ko", "Mo"):
        if value < 1024 or unit == "Mo":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} Mo"  # pragma: no cover — la boucle sort toujours avant


def attachments_section(refs, current_ids) -> str:
    """Section « Pièces jointes » du contexte du tour, ou ``""`` s'il n'y en a
    aucune.

    Le **contenu** n'y figure jamais : seulement de quoi décider quoi lire
    (nom, famille, taille) et la référence à passer à ``read_attachment``.
    Même doctrine que le sommaire du cours (décision 23), pour la même raison :
    le tour resterait cacheable et le coût borné.

    Les pièces de CE message sont distinguées de celles des messages
    précédents — c'est le signal qui pousse le modèle à lire ce que le
    professeur vient de joindre. ``current_ids=None`` retire la distinction :
    c'est le cas d'un sous-assistant d'édition, qui reçoit des consignes et
    non le message du professeur — pour lui, aucune pièce n'est « de ce
    message ».
    """
    entries = refs.entries["attachment"]
    if not entries:
        return ""
    current = None if current_ids is None else {str(i) for i in current_ids}
    lines = []
    for entry in entries:
        attachment = entry.entity
        label = KIND_LABELS.get(attachment.kind, attachment.kind)
        line = (
            f"- {attachment.original_name} (ref: {entry.ref}, {label}, "
            f"{human_size(attachment.size)})"
        )
        if current is not None:
            line += (
                " — jointe à CE message"
                if str(attachment.id) in current
                else " — jointe à un message précédent"
            )
        lines.append(line)
    return "\n\n".join(
        [
            "## Pièces jointes de la conversation",
            "Fichiers joints par le professeur. Leur contenu N'EST PAS dans ce "
            "message : le lire avec `read_attachment` (sa référence en paramètre).",
            "\n".join(lines),
        ]
    )


def attachment_s3_key(course_id, attachment_id, original_name: str) -> str:
    """Clé S3 d'une pièce jointe.

    Sous le préfixe ``courses/`` **délibérément** : c'est l'un des deux
    préfixes balayés par la réconciliation des orphelins
    (:data:`app.maintenance.service.S3_PREFIXES`), et la volumétrie des
    pièces jointes est ainsi comptée par ``storage_inventory`` sans
    modification.
    """
    return (
        f"courses/{course_id}/assistant/{attachment_id}/"
        f"{sanitize_attachment_name(original_name)}"
    )
