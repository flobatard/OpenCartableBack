"""Tools d'exploration du cours pour l'assistant IA.

Quatre tools (specs neutres :class:`AIToolSpec`, exécuteur async passé à
``AIClient.stream_agent``) :

- ``read_block`` : relit un bloc en entier (mode sommaire du contexte, ou
  relecture ciblée) ;
- ``read_resource_pdf`` : extrait le texte d'une ressource PDF de la
  bibliothèque — lecture S3 **synchrone** (:func:`read_pdf_sync`, motif
  ``build_zip_sync`` de :mod:`app.course_transfer.archive`) déportée dans un
  seul ``run_in_threadpool``, plafonds stricts (taille, pages, caractères —
  contrainte Pi) ;
- ``read_resource_image`` : montre au modèle une ressource image de la
  bibliothèque (formats :data:`IMAGE_MIMES`, plafond :data:`IMAGE_MAX_BYTES`)
  — même lecture S3 sous threadpool (:func:`read_image_sync`), image jointe
  au résultat via :class:`AIToolImage` (le client la transmet dans un message
  utilisateur, cf. ``app/core/ai/client.py``) ; le ``content`` persisté reste
  une note texte — l'image n'est jamais rejouée aux tours suivants ;
- ``read_module`` : lit le code HTML/CSS/JS d'un module interactif (en base,
  aucun accès S3 ; :func:`format_module`, plafonné).

L'exécuteur **ne lève jamais** (contrat de ``stream_agent``) : tout échec
métier (uuid mal formé, cible inconnue ou hors cours, mauvais type, plafond
dépassé, fichier illisible) devient un :class:`AIToolResult` ``is_error=True``
au message explicite — le modèle le lit et se corrige, le flux SSE continue.

Aucun accès DB pendant l'exécution : blocs, ressources et modules sont ceux
chargés au départ du flux (dictionnaires indexés par id) — un tour
d'assistant travaille sur un instantané du cours, décision assumée.
"""

import base64
import io
import uuid
from collections.abc import Awaitable, Callable
from tempfile import SpooledTemporaryFile

from fastapi.concurrency import run_in_threadpool

from app.core.ai import AIToolCall, AIToolImage, AIToolResult, AIToolSpec
from app.core.storage import Storage
from app.course_assistant.context import format_block, format_module
from app.models.resource import STATUS_AVAILABLE

PDF_MIME = "application/pdf"
PDF_MAX_BYTES = 20 * 1024 * 1024
PDF_MAX_PAGES = 50
PDF_MAX_CHARS = 40_000

# Images : intersection des formats acceptés par les providers à vision ;
# plafond sur le binaire brut (≈ 4,7 Mo en base64, sous les 5 Mo d'Anthropic,
# le plus strict) — les images de cours sont bien en deçà.
IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
IMAGE_MAX_BYTES = 3_500_000

# Bascule RAM → disque du fichier temporaire (motif course_transfer).
_SPOOL_MAX_BYTES = 8 * 1024 * 1024

READ_BLOCK_SPEC = AIToolSpec(
    name="read_block",
    description=(
        "Lit le contenu complet d'un bloc du cours (texte, exercice avec "
        "corrigé, document ou module) à partir de son id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "block_id": {"type": "string", "description": "Id (uuid) du bloc à lire"}
        },
        "required": ["block_id"],
    },
)

READ_RESOURCE_PDF_SPEC = AIToolSpec(
    name="read_resource_pdf",
    description=(
        "Extrait le texte d'une ressource PDF de la bibliothèque du cours à "
        "partir de son id. Seules les ressources PDF disponibles sont lisibles."
    ),
    parameters={
        "type": "object",
        "properties": {
            "resource_id": {
                "type": "string",
                "description": "Id (uuid) de la ressource PDF à lire",
            }
        },
        "required": ["resource_id"],
    },
)

READ_RESOURCE_IMAGE_SPEC = AIToolSpec(
    name="read_resource_image",
    description=(
        "Vous montre une ressource image de la bibliothèque du cours (PNG, JPEG, "
        "GIF ou WebP, disponible) à partir de son id, pour que vous puissiez la "
        "regarder. Nécessite un modèle acceptant les images ; l'image n'est "
        "visible que pour le tour en cours."
    ),
    parameters={
        "type": "object",
        "properties": {
            "resource_id": {
                "type": "string",
                "description": "Id (uuid) de la ressource image à regarder",
            }
        },
        "required": ["resource_id"],
    },
)

READ_MODULE_SPEC = AIToolSpec(
    name="read_module",
    description=(
        "Lit le code HTML, CSS et JavaScript d'un module interactif du cours à "
        "partir de son id (un bloc « module » ne donne que l'id du module pointé)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "module_id": {"type": "string", "description": "Id (uuid) du module à lire"}
        },
        "required": ["module_id"],
    },
)

TOOL_SPECS = [READ_BLOCK_SPEC, READ_RESOURCE_PDF_SPEC, READ_RESOURCE_IMAGE_SPEC, READ_MODULE_SPEC]


def read_pdf_sync(storage: Storage, s3_key: str) -> str:
    """Télécharge et extrait le texte d'un PDF — RÉSEAU SYNCHRONE, à appeler
    uniquement sous ``run_in_threadpool`` (motif ``build_zip_sync``).

    Extraction page à page (``pypdf``), arrêt net aux plafonds
    :data:`PDF_MAX_PAGES` / :data:`PDF_MAX_CHARS` (mention de troncature).
    Lève ``pypdf`` errors ou ``Exception`` storage : l'appelant les traduit en
    :class:`AIToolResult` d'échec.
    """
    from pypdf import PdfReader

    with SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES) as tmp:
        storage.read_object_into(s3_key, tmp)
        tmp.seek(0)
        reader = PdfReader(tmp)
        parts: list[str] = []
        total = 0
        truncated = False
        for page_index, page in enumerate(reader.pages):
            if page_index >= PDF_MAX_PAGES:
                truncated = True
                break
            text = page.extract_text() or ""
            if total + len(text) > PDF_MAX_CHARS:
                parts.append(text[: PDF_MAX_CHARS - total])
                truncated = True
                break
            parts.append(text)
            total += len(text)
        content = "\n".join(parts).strip()
        if truncated:
            content += "\n\n[Document tronqué : plafond de lecture atteint]"
        return content


def read_image_sync(storage: Storage, s3_key: str) -> str:
    """Télécharge une image et la retourne en base64 — RÉSEAU SYNCHRONE, à
    appeler uniquement sous ``run_in_threadpool`` (motif :func:`read_pdf_sync`).

    Lève ``ValueError`` si l'objet dépasse :data:`IMAGE_MAX_BYTES` (taille
    réelle, la taille déclarée ayant déjà été contrôlée) ; les erreurs storage
    remontent telles quelles — l'appelant traduit en :class:`AIToolResult`.
    """
    buffer = io.BytesIO()
    storage.read_object_into(s3_key, buffer)
    data = buffer.getvalue()
    if len(data) > IMAGE_MAX_BYTES:
        raise ValueError("image trop volumineuse")
    return base64.b64encode(data).decode("ascii")


def _parse_uuid(raw: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def build_tool_executor(
    storage: Storage,
    blocks_by_id: dict[uuid.UUID, object],
    resources_by_id: dict[uuid.UUID, object],
    positions_by_id: dict[uuid.UUID, int],
    modules_by_id: dict[uuid.UUID, object] | None = None,
) -> Callable[[AIToolCall], Awaitable[AIToolResult]]:
    """Fabrique l'exécuteur async passé à ``stream_agent``.

    Les tools d'un même round peuvent s'exécuter en concurrence (ToolNode) :
    la lecture S3 est déportée par appel dans le threadpool, ``storage`` est
    thread-safe en lecture. ``positions_by_id`` donne le numéro d'affichage du
    bloc (même en-tête que le contexte) ; ``modules_by_id`` sert au titre du
    module pointé par un bloc ``module`` et à ``read_module``.
    """
    modules_by_id = modules_by_id or {}

    async def _read_block(arguments: dict) -> AIToolResult:
        block_id = _parse_uuid(arguments.get("block_id"))
        block = blocks_by_id.get(block_id) if block_id else None
        if block is None:
            return AIToolResult(
                content="Bloc introuvable dans ce cours (vérifiez l'id).", is_error=True
            )
        return AIToolResult(
            content=format_block(block, positions_by_id[block_id], modules_by_id)
        )

    def _available_resource(arguments: dict) -> tuple[object | None, AIToolResult | None]:
        """Ressource du cours prête à être lue, ou le résultat d'échec à renvoyer."""
        resource_id = _parse_uuid(arguments.get("resource_id"))
        resource = resources_by_id.get(resource_id) if resource_id else None
        if resource is None:
            return None, AIToolResult(
                content="Ressource introuvable dans ce cours (vérifiez l'id).",
                is_error=True,
            )
        if resource.status != STATUS_AVAILABLE:
            return None, AIToolResult(
                content="Ressource pas encore disponible (upload non confirmé).",
                is_error=True,
            )
        return resource, None

    async def _read_resource_pdf(arguments: dict) -> AIToolResult:
        resource, failure = _available_resource(arguments)
        if failure is not None:
            return failure
        if resource.mime != PDF_MIME:
            return AIToolResult(
                content=f"Cette ressource n'est pas un PDF (type : {resource.mime}).",
                is_error=True,
            )
        if resource.size > PDF_MAX_BYTES:
            return AIToolResult(
                content=(
                    f"PDF trop volumineux pour être lu ({resource.size} octets, "
                    f"plafond {PDF_MAX_BYTES})."
                ),
                is_error=True,
            )
        try:
            content = await run_in_threadpool(read_pdf_sync, storage, resource.s3_key)
        except Exception:
            return AIToolResult(
                content="Lecture du PDF impossible (fichier corrompu ou illisible).",
                is_error=True,
            )
        if not content.strip():
            return AIToolResult(
                content="Ce PDF ne contient pas de texte extractible (scan d'images ?).",
                is_error=True,
            )
        return AIToolResult(content=content)

    async def _read_resource_image(arguments: dict) -> AIToolResult:
        resource, failure = _available_resource(arguments)
        if failure is not None:
            return failure
        if resource.mime not in IMAGE_MIMES:
            return AIToolResult(
                content=(
                    f"Cette ressource n'est pas une image lisible (type : {resource.mime} ; "
                    "formats acceptés : PNG, JPEG, GIF, WebP)."
                ),
                is_error=True,
            )
        if resource.size > IMAGE_MAX_BYTES:
            return AIToolResult(
                content=(
                    f"Image trop volumineuse pour être transmise ({resource.size} octets, "
                    f"plafond {IMAGE_MAX_BYTES})."
                ),
                is_error=True,
            )
        try:
            data = await run_in_threadpool(read_image_sync, storage, resource.s3_key)
        except Exception:
            return AIToolResult(
                content=(
                    "Lecture de l'image impossible (fichier absent, trop volumineux "
                    "ou illisible)."
                ),
                is_error=True,
            )
        return AIToolResult(
            content=(
                f"Image « {resource.original_name} » ({resource.mime}, {resource.size} octets) "
                "transmise au modèle pour ce tour — non conservée dans l'historique, "
                "relisez la ressource si vous en avez encore besoin."
            ),
            image=AIToolImage(
                mime_type=resource.mime,
                data=data,
                caption=(
                    f"Ressource image « {resource.original_name} » du cours "
                    f"(id: {resource.id}), demandée via read_resource_image."
                ),
            ),
        )

    async def _read_module(arguments: dict) -> AIToolResult:
        module_id = _parse_uuid(arguments.get("module_id"))
        module = modules_by_id.get(module_id) if module_id else None
        if module is None:
            return AIToolResult(
                content="Module introuvable dans ce cours (vérifiez l'id).", is_error=True
            )
        return AIToolResult(content=format_module(module))

    handlers = {
        READ_BLOCK_SPEC.name: _read_block,
        READ_RESOURCE_PDF_SPEC.name: _read_resource_pdf,
        READ_RESOURCE_IMAGE_SPEC.name: _read_resource_image,
        READ_MODULE_SPEC.name: _read_module,
    }

    async def executor(call: AIToolCall) -> AIToolResult:
        handler = handlers.get(call.name)
        if handler is None:
            return AIToolResult(content=f"Outil inconnu : {call.name}", is_error=True)
        return await handler(call.arguments)

    return executor
