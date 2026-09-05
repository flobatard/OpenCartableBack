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
  utilisateur, cf. ``app/core/ai/agent.py``) ; le ``content`` persisté reste
  une note texte — l'image n'est jamais rejouée aux tours suivants ;
- ``read_module`` : lit le code HTML/CSS/JS d'un module interactif (en base,
  aucun accès S3 ; :func:`format_module`, plafonné).

S'y ajoutent, dans un **contexte d'édition** (``edit`` = descripteur
:class:`~app.course_assistant.editing.EditContext`), ses tools de proposition
**HITL** (:mod:`app.course_assistant.editing`) : ils **ne mutent rien** — la
proposition voyage dans les ``args`` du ``tool_call`` — et **figent le run**
(``agent_interrupt``) jusqu'à la décision du professeur, dont le texte est le
résultat du tool. Leurs specs s'ajoutent aux lectures (:func:`build_tool_specs`)
et leur handler prime au dispatch de l'exécuteur (:func:`build_tool_executor`).

Les tools ciblent une entité par sa **référence courte** (``B3``, ``R2``,
``M1`` — :mod:`app.course_assistant.refs`, jamais un UUID côté modèle) :
:func:`build_tool_specs` construit les specs **par tour** avec un ``enum`` des
références valides (restreint aux ressources éligibles pour les lectures
PDF/image ; omis quand la liste est vide — un ``enum`` vide est un schéma
invalide chez certains providers), et l'exécuteur résout l'argument par la
chaîne tolérante de :meth:`CourseRefs.resolve` (référence, UUID exact ou
déformé, titre).

L'exécuteur **ne lève jamais** (contrat de ``stream_agent``) : tout échec
métier (cible inconnue ou ambiguë, mauvais type, plafond dépassé, fichier
illisible) devient un :class:`AIToolResult` ``is_error=True`` au message
explicite — listant les candidats le cas échéant — que le modèle lit pour se
corriger, le flux SSE continue.

Aucun accès DB pendant l'exécution : blocs, ressources et modules sont ceux
chargés au départ du flux (instantané porté par :class:`CourseRefs`) — un
tour d'assistant travaille sur un instantané du cours, décision assumée.
"""

import base64
import io
from collections.abc import Awaitable, Callable
from tempfile import SpooledTemporaryFile

from fastapi.concurrency import run_in_threadpool

from app.core.ai import AIToolCall, AIToolImage, AIToolResult, AIToolSpec
from app.core.storage import Storage
from app.course_assistant.editing.base import EditContext
from app.course_assistant.refs import CourseRefs
from app.course_assistant.render import format_block, format_module
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

READ_BLOCK = "read_block"
READ_RESOURCE_PDF = "read_resource_pdf"
READ_RESOURCE_IMAGE = "read_resource_image"
READ_MODULE = "read_module"


def _is_readable_pdf(resource) -> bool:
    return resource.status == STATUS_AVAILABLE and resource.mime == PDF_MIME


def _is_readable_image(resource) -> bool:
    return resource.status == STATUS_AVAILABLE and resource.mime in IMAGE_MIMES


def _ref_spec(
    name: str, description: str, param: str, param_description: str, refs: list[str]
) -> AIToolSpec:
    """Spec à un seul paramètre « référence », ``enum`` des valeurs valides
    (omis si vide)."""
    schema: dict = {"type": "string", "description": param_description}
    if refs:
        schema["enum"] = refs
    return AIToolSpec(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {param: schema}, "required": [param]},
    )


def build_tool_specs(refs: CourseRefs, *, edit: EditContext | None = None) -> list[AIToolSpec]:
    """Les specs du tour, ``enum`` calé sur l'instantané du cours ; ``edit``
    (contexte d'édition) ajoute les specs de ses tools de proposition."""
    specs = [
        _ref_spec(
            READ_BLOCK,
            "Lit le contenu complet d'un bloc du cours (texte, exercice avec "
            "corrigé, document ou module) à partir de sa référence (B1, B2…).",
            "block_ref",
            "Référence du bloc à lire, telle qu'indiquée dans le cours (ex. B3)",
            refs.refs("block"),
        ),
        _ref_spec(
            READ_RESOURCE_PDF,
            "Extrait le texte d'une ressource PDF de la bibliothèque du cours à "
            "partir de sa référence (R1, R2…). Seules les ressources PDF "
            "disponibles sont lisibles.",
            "resource_ref",
            "Référence de la ressource PDF à lire, telle qu'indiquée dans la "
            "bibliothèque (ex. R2)",
            [e.ref for e in refs.entries["resource"] if _is_readable_pdf(e.entity)],
        ),
        _ref_spec(
            READ_RESOURCE_IMAGE,
            "Vous montre une ressource image de la bibliothèque du cours (PNG, JPEG, "
            "GIF ou WebP, disponible) à partir de sa référence (R1, R2…), pour que "
            "vous puissiez la regarder. Nécessite un modèle acceptant les images ; "
            "l'image n'est visible que pour le tour en cours.",
            "resource_ref",
            "Référence de la ressource image à regarder, telle qu'indiquée dans la "
            "bibliothèque (ex. R2)",
            [e.ref for e in refs.entries["resource"] if _is_readable_image(e.entity)],
        ),
        _ref_spec(
            READ_MODULE,
            "Lit le code HTML, CSS et JavaScript d'un module interactif du cours à "
            "partir de sa référence (M1, M2…) — un bloc « module » ne donne que la "
            "référence du module pointé.",
            "module_ref",
            "Référence du module à lire, telle qu'indiquée dans le cours (ex. M1)",
            refs.refs("module"),
        ),
    ]
    if edit is not None:
        specs.extend(tool.spec(refs) for tool in edit.tools)
    return specs


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


def build_tool_executor(
    storage: Storage, refs: CourseRefs, *, edit: EditContext | None = None
) -> Callable[[AIToolCall], Awaitable[AIToolResult]]:
    """Fabrique l'exécuteur async passé à ``stream_agent``.

    Les tools d'un même round peuvent s'exécuter en concurrence (ToolNode) :
    la lecture S3 est déportée par appel dans le threadpool, ``storage`` est
    thread-safe en lecture. ``refs`` porte l'instantané du cours (blocs,
    ressources, modules et leurs références courtes). ``edit`` (contexte
    d'édition — run à ``thread_id`` obligatoire) active ses tools de
    proposition HITL (validation puis **interrupt** jusqu'à la décision du
    professeur — docstring du module), qui reçoivent l'appel complet (ils
    lisent ``call.id``, clé de reprise) et priment sur les lectures.
    """

    async def _read_block(arguments: dict) -> AIToolResult:
        resolution = refs.resolve("block", arguments.get("block_ref"))
        if resolution.entry is None:
            return AIToolResult(content=resolution.error, is_error=True)
        return AIToolResult(content=format_block(resolution.entry.entity, refs))

    def _available_resource(
        arguments: dict, eligible
    ) -> tuple[object | None, AIToolResult | None]:
        """Ressource du cours prête à être lue, ou le résultat d'échec à renvoyer."""
        resolution = refs.resolve("resource", arguments.get("resource_ref"), eligible=eligible)
        if resolution.entry is None:
            return None, AIToolResult(content=resolution.error, is_error=True)
        resource = resolution.entry.entity
        if resource.status != STATUS_AVAILABLE:
            return None, AIToolResult(
                content="Ressource pas encore disponible (upload non confirmé).",
                is_error=True,
            )
        return resource, None

    async def _read_resource_pdf(arguments: dict) -> AIToolResult:
        resource, failure = _available_resource(arguments, _is_readable_pdf)
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
        resource, failure = _available_resource(arguments, _is_readable_image)
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
                    f"(ref: {refs.ref_of('resource', resource.id)}), demandée via "
                    "read_resource_image."
                ),
            ),
        )

    async def _read_module(arguments: dict) -> AIToolResult:
        resolution = refs.resolve("module", arguments.get("module_ref"))
        if resolution.entry is None:
            return AIToolResult(content=resolution.error, is_error=True)
        return AIToolResult(content=format_module(resolution.entry.entity, refs))

    handlers = {
        READ_BLOCK: _read_block,
        READ_RESOURCE_PDF: _read_resource_pdf,
        READ_RESOURCE_IMAGE: _read_resource_image,
        READ_MODULE: _read_module,
    }
    # Tools de proposition du contexte d'édition, construits une fois par tour.
    proposal_handlers = (
        {tool.name: tool.build_handler(refs) for tool in edit.tools} if edit is not None else {}
    )

    async def executor(call: AIToolCall) -> AIToolResult:
        proposal = proposal_handlers.get(call.name)
        if proposal is not None:
            return await proposal(call)
        handler = handlers.get(call.name)
        if handler is None:
            return AIToolResult(content=f"Outil inconnu : {call.name}", is_error=True)
        return await handler(call.arguments)

    return executor
