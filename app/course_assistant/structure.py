"""Propositions structurelles de l'assistant global : ajouter, supprimer,
réordonner les blocs du cours.

Troisième famille de tools de l'**édition globale** (contexte ``course`` dont
le tour porte ``allow_edit``), à côté de la délégation
(:mod:`app.course_assistant.delegation`) : ``propose_block_add``,
``propose_block_delete`` et ``propose_blocks_reorder``. Ce ne sont **pas** des
sous-assistants — il n'y a aucun contenu à rédiger — mais des propositions
HITL ordinaires (:func:`~app.course_assistant.editing.base.hitl_gate`, genre
``proposal``), portées par le run de l'assistant global lui-même : le flux
émet ``interrupt`` et se ferme, le professeur revoit la proposition dans la
fenêtre de revue globale du front, la route de décision reprend le run.

Comme toute proposition, elles **ne mutent rien** côté back : le front
applique l'opération acceptée par les routes existantes du cours
(``POST /blocks``, ``DELETE /blocks/{id}``, ``PUT /blocks/order``) AVANT de
poster sa décision. Un ajout ne porte que la **méta** du bloc (type, titre,
description, position, ressource ou module pointé) : un bloc texte ou exercice
est créé vide, et l'assistant le fait remplir ensuite par ``edit_block`` — le
catalogue de syntaxes reste l'affaire des sous-assistants.

**Références renumérotées.** Les ``B…`` sont positionnelles
(:mod:`app.course_assistant.refs`) et la reprise rebâtit refs et tools sur
l'instantané rechargé : après une proposition acceptée, ``B3`` peut désigner un
autre bloc. Le résultat du tool rend donc le **nouveau sommaire**
(:func:`outline_lines`) et, pour un ajout, la référence du bloc créé
(``CourseRefs.new_block_refs``, depuis les ``block_refs`` capturés à
l'interrupt). Corollaire pour l'idempotence (le tool est ré-exécuté depuis le
début à la reprise, avec les args D'ORIGINE du modèle) : la validation ne
dépend que de références encore résolubles — une suppression teste d'abord
:meth:`CourseRefs.block_gone`, une permutation complète reste complète une fois
renumérotée — et le texte d'acceptation ne cite jamais l'entrée résolue.

Helpers purs, sans I/O.
"""

from app.core.ai import AIToolCall, AIToolResult, AIToolSpec
from app.course_assistant.editing.base import (
    SUMMARY_SCHEMA,
    Handler,
    ProposalTool,
    hitl_description,
    hitl_gate,
    ref_schema,
    string_arg,
    tool_error,
)
from app.course_assistant.refs import CourseRefs
from app.models.block import TYPE_DOCUMENT, TYPE_EXERCISE, TYPE_MODULE, TYPE_TEXT
from app.models.resource import STATUS_AVAILABLE

PROPOSE_BLOCK_ADD = "propose_block_add"
PROPOSE_BLOCK_DELETE = "propose_block_delete"
PROPOSE_BLOCKS_REORDER = "propose_blocks_reorder"

BLOCK_TYPES = (TYPE_TEXT, TYPE_EXERCISE, TYPE_DOCUMENT, TYPE_MODULE)
# Types dont le contenu se rédige ensuite par ``edit_block``.
_FILLABLE_TYPES = frozenset({TYPE_TEXT, TYPE_EXERCISE})

# Plafonds de ``BlockCreate`` (:mod:`app.courses.schemas`) — appliqués en
# validation, jamais en ``maxLength`` de schéma.
BLOCK_TITLE_MAX_CHARS = 300
BLOCK_DESCRIPTION_MAX_CHARS = 500
# Blocs listés dans le sommaire d'un résultat d'acceptation.
OUTLINE_MAX_BLOCKS = 100

_REJECTED = "Le professeur a REJETÉ la proposition — la structure du cours est inchangée."
_ACCEPTED_ADD = "Le professeur a ACCEPTÉ la proposition : le bloc a été ajouté au cours."
_ACCEPTED_DELETE = "Le professeur a ACCEPTÉ la proposition : le bloc a été supprimé du cours."
_ACCEPTED_REORDER = "Le professeur a ACCEPTÉ la proposition : les blocs ont été réordonnés."
_RENUMBERED = (
    "Les références B… ont été renumérotées : n'utilisez plus que celles du nouveau sommaire."
)
_FILL_HINT = "Il est vide : faites-le remplir maintenant avec `edit_block`."


# ------------------------------------------------------------------- specs


def _available_resource(resource) -> bool:
    return resource.status == STATUS_AVAILABLE


def _block_add_spec(refs: CourseRefs) -> AIToolSpec:
    return AIToolSpec(
        name=PROPOSE_BLOCK_ADD,
        description=hitl_description(
            "Propose au professeur d'AJOUTER un bloc au cours (type, titre, description, "
            "position). Un bloc texte ou exercice est créé VIDE — faites-le remplir "
            "ensuite avec edit_block ; un bloc document pointe une ressource de la "
            "bibliothèque, un bloc module un module interactif"
        ),
        parameters={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": list(BLOCK_TYPES),
                    "description": (
                        "Type du bloc : text (cours en markdown), exercise (énoncé et "
                        "questions), document (ressource de la bibliothèque), module "
                        "(module interactif)."
                    ),
                },
                "title": {"type": "string", "description": "Titre du bloc."},
                "description": {
                    "type": "string",
                    "description": "Courte description du bloc (facultative).",
                },
                "after_ref": ref_schema(
                    "Référence du bloc APRÈS lequel insérer le nouveau (ex. B3) ; "
                    "absent = en fin de cours",
                    refs.refs("block"),
                ),
                "resource_ref": ref_schema(
                    "Référence de la ressource affichée (ex. R2) — OBLIGATOIRE pour un "
                    "bloc document, interdite sinon",
                    [e.ref for e in refs.entries["resource"] if _available_resource(e.entity)],
                ),
                "module_ref": ref_schema(
                    "Référence du module interactif affiché (ex. M1) — OBLIGATOIRE pour "
                    "un bloc module, interdite sinon",
                    refs.refs("module"),
                ),
                "summary": SUMMARY_SCHEMA,
            },
            "required": ["type", "title", "summary"],
        },
    )


def _block_delete_spec(refs: CourseRefs) -> AIToolSpec:
    return AIToolSpec(
        name=PROPOSE_BLOCK_DELETE,
        description=hitl_description(
            "Propose au professeur de SUPPRIMER un bloc du cours (irréversible : son "
            "contenu et les tentatives des élèves sur ce bloc sont perdus)"
        ),
        parameters={
            "type": "object",
            "properties": {
                "target_ref": ref_schema(
                    "Référence du bloc à supprimer, telle qu'indiquée dans le sommaire (ex. B3)",
                    refs.refs("block"),
                ),
                "summary": SUMMARY_SCHEMA,
            },
            "required": ["target_ref", "summary"],
        },
    )


def _blocks_reorder_spec(refs: CourseRefs) -> AIToolSpec:
    return AIToolSpec(
        name=PROPOSE_BLOCKS_REORDER,
        description=hitl_description(
            "Propose au professeur de RÉORDONNER les blocs du cours : donnez l'ordre "
            "COMPLET souhaité (toutes les références du sommaire, chacune une fois)"
        ),
        parameters={
            "type": "object",
            "properties": {
                "order": {
                    "type": "array",
                    "items": ref_schema("Référence d'un bloc (ex. B3)", refs.refs("block")),
                    "description": (
                        "Toutes les références des blocs du cours, dans le nouvel ordre "
                        "d'affichage."
                    ),
                },
                "summary": SUMMARY_SCHEMA,
            },
            "required": ["order", "summary"],
        },
    )


# ------------------------------------------------- textes d'acceptation


def outline_lines(refs: CourseRefs) -> str:
    """Sommaire compact des blocs (référence, titre, type — une ligne par
    bloc, plafonné) dans la numérotation COURANTE de ``refs``."""
    entries = refs.entries["block"]
    if not entries:
        return "(le cours ne contient plus aucun bloc)"
    lines = [f"{e.ref} · {e.title} ({e.entity.type})" for e in entries[:OUTLINE_MAX_BLOCKS]]
    if len(entries) > OUTLINE_MAX_BLOCKS:
        lines.append(f"… ({len(entries) - OUTLINE_MAX_BLOCKS} blocs de plus)")
    return "\n".join(lines)


def _accepted(head: str, refs: CourseRefs) -> str:
    return f"{head} {_RENUMBERED}\nNouveau sommaire :\n{outline_lines(refs)}"


def accepted_add_text(refs: CourseRefs, block_type: str) -> str:
    """Résultat d'un ajout accepté : la référence du bloc créé quand
    l'instantané rechargé en révèle exactement un (``new_block_refs``) —
    sinon le modèle le repère dans le sommaire — et, pour un bloc texte ou
    exercice, l'invitation à le faire remplir."""
    new_refs = refs.new_block_refs
    head = _ACCEPTED_ADD
    if len(new_refs) == 1:
        head += f" Sa référence est {new_refs[0]}."
    else:
        head += " Repérez-le dans le nouveau sommaire."
    if block_type in _FILLABLE_TYPES:
        head += f" {_FILL_HINT}"
    return _accepted(head, refs)


def accepted_delete_text(refs: CourseRefs) -> str:
    return _accepted(_ACCEPTED_DELETE, refs)


def accepted_reorder_text(refs: CourseRefs) -> str:
    return _accepted(_ACCEPTED_REORDER, refs)


# ---------------------------------------------------------------- handlers


def _absent(value: object) -> bool:
    return value in (None, "")


def _build_block_add_handler(refs: CourseRefs) -> Handler:
    async def propose_block_add(call: AIToolCall) -> AIToolResult:
        arguments = call.arguments
        block_type = arguments.get("type")
        if block_type not in BLOCK_TYPES:
            return tool_error(
                f"Paramètre type invalide : attendu l'un de {', '.join(BLOCK_TYPES)}."
            )
        title, failure = string_arg(
            arguments, "title", max_chars=BLOCK_TITLE_MAX_CHARS, required=True
        )
        if failure is not None:
            return failure
        assert title is not None  # ``required=True`` sans échec
        if not title.strip():
            return tool_error("Paramètre title vide : donnez un titre au bloc.")
        _, failure = string_arg(
            arguments, "description", max_chars=BLOCK_DESCRIPTION_MAX_CHARS, required=False
        )
        if failure is not None:
            return failure
        after_ref = arguments.get("after_ref")
        if not _absent(after_ref):
            resolution = refs.resolve("block", after_ref)
            if resolution.entry is None:
                return tool_error(resolution.error or "Bloc introuvable.")

        resource_ref, module_ref = arguments.get("resource_ref"), arguments.get("module_ref")
        if block_type == TYPE_DOCUMENT:
            if _absent(resource_ref):
                return tool_error(
                    "Un bloc document affiche une ressource de la bibliothèque : "
                    "précisez resource_ref. "
                    + refs.resolve("resource", None, eligible=_available_resource).error
                )
            resolution = refs.resolve("resource", resource_ref, eligible=_available_resource)
            if resolution.entry is None:
                return tool_error(resolution.error or "Ressource introuvable.")
            if not _available_resource(resolution.entry.entity):
                return tool_error(
                    f"La ressource « {resolution.entry.title} » n'est pas encore disponible "
                    "(téléversement inachevé)."
                )
        elif not _absent(resource_ref):
            return tool_error("resource_ref n'a de sens que pour un bloc de type document.")
        if block_type == TYPE_MODULE:
            if _absent(module_ref):
                return tool_error(
                    "Un bloc module affiche un module interactif de la bibliothèque : "
                    "précisez module_ref. " + refs.resolve("module", None).error
                )
            resolution = refs.resolve("module", module_ref)
            if resolution.entry is None:
                return tool_error(resolution.error or "Module introuvable.")
        elif not _absent(module_ref):
            return tool_error("module_ref n'a de sens que pour un bloc de type module.")
        # À la reprise, ``refs`` est l'instantané rechargé : le bloc accepté y
        # figure, ``new_block_refs`` donne sa référence.
        return hitl_gate(
            call, accepted_text=accepted_add_text(refs, block_type), rejected_text=_REJECTED
        )

    return propose_block_add


def _build_block_delete_handler(refs: CourseRefs) -> Handler:
    async def propose_block_delete(call: AIToolCall) -> AIToolResult:
        target_ref = call.arguments.get("target_ref")
        # Validation idempotente : à la reprise d'une suppression ACCEPTÉE, le
        # front a déjà supprimé le bloc et la référence positionnelle désigne
        # un AUTRE bloc (ou plus rien) — on teste donc d'abord la référence
        # d'origine disparue, sans jamais se fier à une résolution.
        if not refs.block_gone(target_ref):
            resolution = refs.resolve("block", target_ref)
            if resolution.entry is None:
                return tool_error(resolution.error or "Bloc introuvable.")
        return hitl_gate(call, accepted_text=accepted_delete_text(refs), rejected_text=_REJECTED)

    return propose_block_delete


def _resolve_order(refs: CourseRefs, order: object) -> tuple[list | None, str | None]:
    """Entrées de blocs désignées par ``order`` (même ordre), ou le message
    d'échec : liste malformée, références inconnues, dupliquées ou manquantes
    — toutes listées, pour que le modèle corrige en un seul essai."""
    if not isinstance(order, list) or not order or not all(isinstance(r, str) for r in order):
        return None, "Paramètre order invalide : liste non vide de références de blocs attendue."
    entries, unknown = [], []
    for raw in order:
        entry = refs.resolve("block", raw).entry
        if entry is None:
            unknown.append(raw)
        else:
            entries.append(entry)
    seen: set = set()
    duplicated = []
    for entry in entries:
        if entry.id in seen and entry.ref not in duplicated:
            duplicated.append(entry.ref)
        seen.add(entry.id)
    missing = [e.ref for e in refs.entries["block"] if e.id not in seen]
    problems = []
    if unknown:
        problems.append(f"références inconnues : {', '.join(unknown)}")
    if duplicated:
        problems.append(f"références en double : {', '.join(duplicated)}")
    if missing:
        problems.append(f"blocs manquants : {', '.join(missing)}")
    if problems:
        return None, (
            "L'ordre doit contenir TOUS les blocs du cours, chacun une fois — "
            + " ; ".join(problems)
            + "."
        )
    return entries, None


def _build_blocks_reorder_handler(refs: CourseRefs) -> Handler:
    async def propose_blocks_reorder(call: AIToolCall) -> AIToolResult:
        entries, error = _resolve_order(refs, call.arguments.get("order"))
        if entries is None:
            return tool_error(error or "Paramètre order invalide.")
        # Permutation identité, jugée sur les références (fonction des seuls
        # args : vraie ou fausse à l'aller comme à la reprise).
        if [e.ref for e in entries] == refs.refs("block"):
            return tool_error("Cet ordre est déjà celui du cours : rien à réordonner.")
        return hitl_gate(call, accepted_text=accepted_reorder_text(refs), rejected_text=_REJECTED)

    return propose_blocks_reorder


# ------------------------------------------------ réécriture à l'émission


def _entry(refs: CourseRefs, kind, raw):
    if _absent(raw):
        return None
    return refs.resolve(kind, raw).entry


def _rewrite_block_add_args(arguments: dict, refs: CourseRefs) -> dict:
    """Ids résolus ajoutés aux args relayés et persistés (le front applique
    sans nouvelle résolution) ; nom de la ressource et titre du module pour
    l'affichage de la revue."""
    after = _entry(refs, "block", arguments.get("after_ref"))
    resource = _entry(refs, "resource", arguments.get("resource_ref"))
    module = _entry(refs, "module", arguments.get("module_ref"))
    return {
        **arguments,
        "after_id": str(after.id) if after is not None else None,
        "resource_id": str(resource.id) if resource is not None else None,
        "resource_name": resource.title if resource is not None else None,
        "module_id": str(module.id) if module is not None else None,
        "module_title": module.title if module is not None else None,
    }


def _rewrite_block_delete_args(arguments: dict, refs: CourseRefs) -> dict:
    target = _entry(refs, "block", arguments.get("target_ref"))
    if target is None:
        return arguments
    return {**arguments, "block_id": str(target.id), "target_title": target.title}


def _rewrite_blocks_reorder_args(arguments: dict, refs: CourseRefs) -> dict:
    entries, _ = _resolve_order(refs, arguments.get("order"))
    if entries is None:
        return arguments
    return {**arguments, "block_ids": [str(e.id) for e in entries]}


STRUCTURE_TOOLS: tuple[ProposalTool, ...] = (
    ProposalTool(
        name=PROPOSE_BLOCK_ADD,
        spec=_block_add_spec,
        build_handler=_build_block_add_handler,
        rewrite_args=_rewrite_block_add_args,
    ),
    ProposalTool(
        name=PROPOSE_BLOCK_DELETE,
        spec=_block_delete_spec,
        build_handler=_build_block_delete_handler,
        rewrite_args=_rewrite_block_delete_args,
    ),
    ProposalTool(
        name=PROPOSE_BLOCKS_REORDER,
        spec=_blocks_reorder_spec,
        build_handler=_build_blocks_reorder_handler,
        rewrite_args=_rewrite_blocks_reorder_args,
    ),
)

STRUCTURE_TOOL_NAMES = frozenset(tool.name for tool in STRUCTURE_TOOLS)
