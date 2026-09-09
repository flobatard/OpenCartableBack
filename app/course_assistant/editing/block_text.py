"""Contexte d'édition d'un bloc texte (``block_text``) — premier flux HITL.

Un seul tool de proposition, ``propose_block_edit`` : markdown **intégral** de
remplacement du bloc (+ résumé), plafond miroir de ``TextContent.markdown``.
Le front affiche la proposition en diff À LA PLACE de l'éditeur et l'applique
lui-même sur acceptation ; les liens de contenu ``oc-resource:``/``oc-module:``
insérés par référence courte sont réécrits en UUID à l'émission.
"""

from app.core.ai import AIToolCall, AIToolResult, AIToolSpec
from app.course_assistant.editing.base import (
    SUMMARY_SCHEMA,
    TARGET_BLOCK,
    EditContext,
    Handler,
    ProposalTool,
    hitl_description,
    hitl_gate,
)
from app.course_assistant.prompts import CONTENT_PRESERVATION_RULE, edit_system_prompt
from app.course_assistant.refs import CourseRefs
from app.models.ai_conversation import CONTEXT_BLOCK_TEXT
from app.models.block import TYPE_TEXT

PROPOSE_BLOCK_EDIT = "propose_block_edit"

# Plafond d'une proposition d'édition — miroir de ``TextContent.markdown``
# (``max_length=100_000``, app/courses/schemas.py) : une proposition plus
# longue serait de toute façon rejetée à l'enregistrement du bloc.
PROPOSAL_MAX_CHARS = 100_000

_MISSION = """\
Vous êtes l'assistant pédagogique d'OpenCartable, aux côtés d'un professeur \
qui édite un bloc de texte de son cours : vous l'aidez à réécrire, améliorer \
ou compléter ce bloc (clarté, style, exactitude, progression pédagogique), en \
cohérence avec le reste du cours.\
"""

_EDIT_RULES = f"""\
Règles d'édition du bloc : un seul outil, `propose_block_edit` \
(`new_markdown` = markdown intégral de remplacement du bloc). Le bloc édité \
est fourni en entier dans le message du tour.

{CONTENT_PRESERVATION_RULE}\
"""


def _spec(refs: CourseRefs) -> AIToolSpec:
    """Spec du tool (aucune référence en paramètre : ``refs`` inutilisé)."""
    return AIToolSpec(
        name=PROPOSE_BLOCK_EDIT,
        description=hitl_description(
            "Propose au professeur une réécriture du bloc texte en cours d'édition"
        ),
        parameters={
            "type": "object",
            "properties": {
                "new_markdown": {
                    "type": "string",
                    "description": (
                        "Markdown INTÉGRAL de remplacement du bloc (l'inchangé recopié "
                        "à l'identique)."
                    ),
                },
                "summary": SUMMARY_SCHEMA,
            },
            "required": ["new_markdown"],
        },
    )


def _build_handler(refs: CourseRefs) -> Handler:
    async def propose_block_edit(call: AIToolCall) -> AIToolResult:
        # Validation AVANT l'interrupt (échec immédiat, aucun run figé) — et
        # idempotente : à la reprise, le tool est ré-exécuté depuis le début.
        new_markdown = call.arguments.get("new_markdown")
        if not isinstance(new_markdown, str):
            return AIToolResult(
                content="Paramètre new_markdown manquant ou invalide (chaîne attendue).",
                is_error=True,
            )
        if len(new_markdown) > PROPOSAL_MAX_CHARS:
            return AIToolResult(
                content=(
                    f"Proposition trop longue ({len(new_markdown)} caractères, "
                    f"plafond {PROPOSAL_MAX_CHARS}) — proposez une version plus courte."
                ),
                is_error=True,
            )
        return hitl_gate(
            call,
            accepted_text="Le professeur a ACCEPTÉ la proposition et l'a appliquée au bloc.",
            rejected_text="Le professeur a REJETÉ la proposition — le bloc est inchangé.",
        )

    return propose_block_edit


def _rewrite_args(arguments: dict, refs: CourseRefs) -> dict:
    """Liens de contenu du markdown proposé (``oc-resource:R2``/``oc-module:M1``)
    réécrits en UUID : le markdown reçu par le front est directement applicable."""
    new_markdown = arguments.get("new_markdown")
    if not isinstance(new_markdown, str):
        return arguments
    return {**arguments, "new_markdown": refs.rewrite_content_refs(new_markdown)}


BLOCK_TEXT = EditContext(
    context=CONTEXT_BLOCK_TEXT,
    target=TARGET_BLOCK,
    block_type=TYPE_TEXT,
    type_error_detail="Ce contexte ne s'applique qu'aux blocs texte",
    system_prompt=edit_system_prompt(_MISSION, _EDIT_RULES),
    tools=(
        ProposalTool(
            name=PROPOSE_BLOCK_EDIT,
            spec=_spec,
            build_handler=_build_handler,
            rewrite_args=_rewrite_args,
        ),
    ),
)
