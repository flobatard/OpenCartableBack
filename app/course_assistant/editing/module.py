"""Contexte d'édition d'un module interactif (``module``) — HITL par fichier.

Décision actée (arbitrage utilisateur) : la proposition porte sur **UN** des
trois fichiers du module, jamais sur les trois à la fois — trois tools, une
opération par appel, une proposition à la fois (le modèle enchaîne les appels
après chaque décision, en annonçant son plan avant le premier) :

- ``propose_html_edit`` / ``propose_css_edit`` / ``propose_js_edit`` :
  ``new_code`` = contenu INTÉGRAL de remplacement du fichier visé.

La cible d'une conversation de ce contexte est un **module**
(``ai_conversations.module_id``), pas un bloc : :attr:`EditContext.target` vaut
:data:`~app.course_assistant.editing.base.TARGET_MODULE` et ``block_type`` est
``None``.

``rewrite_args`` est l'**identité** : le code d'un module n'est pas du markdown
de cours — il ne porte ni ``oc-resource:`` ni ``oc-module:`` (un module est
self-contained, son réseau sortant est coupé par la CSP du srcdoc), donc rien à
réécrire avant l'émission. Côté front, le professeur revoit la proposition en
diff **pendant que l'aperçu sandbox exécute déjà le code proposé**, puis
l'applique lui-même dans son éditeur Monaco (annulable par Ctrl-Z) — la route
de décision ne mute jamais le module.

Plafond miroir de ``MAX_CODE_LENGTH`` (app/modules/schemas.py), appliqué en
validation — pas de ``maxLength`` dans les schémas (mot-clé inégalement
supporté par les providers).
"""

from app.core.ai import AIToolCall, AIToolResult, AIToolSpec
from app.course_assistant.editing.base import (
    TARGET_MODULE,
    EditContext,
    Handler,
    ProposalTool,
    hitl_gate,
    string_arg,
)
from app.course_assistant.prompts import MODULE_RUNTIME, edit_system_prompt
from app.course_assistant.refs import CourseRefs
from app.models.ai_conversation import CONTEXT_MODULE

PROPOSE_HTML_EDIT = "propose_html_edit"
PROPOSE_CSS_EDIT = "propose_css_edit"
PROPOSE_JS_EDIT = "propose_js_edit"

# Plafond d'une proposition — miroir de ``MAX_CODE_LENGTH``
# (app/modules/schemas.py) : plus long, le PATCH du module refuserait de toute
# façon d'enregistrer.
MODULE_CODE_MAX_CHARS = 200_000

_MISSION = """\
Vous êtes l'assistant pédagogique d'OpenCartable, aux côtés d'un professeur \
qui édite un module interactif de son cours : une petite application \
autonome en HTML, CSS et JavaScript (animation, simulation, quiz, \
grapheur…), destinée à ses élèves. Votre mission : l'aider à écrire, corriger \
et améliorer ce module — justesse du code, clarté du rendu, valeur \
pédagogique, accessibilité — en cohérence avec le cours, fourni pour \
contexte. Vouvoyez toujours votre interlocuteur et répondez en français, en \
markdown.\
"""

_EDIT_RULES = """\
Règles d'édition du module — impératives :

- Toute modification du module passe EXCLUSIVEMENT par les outils de \
proposition : `propose_html_edit`, `propose_css_edit` et `propose_js_edit`. \
N'écrivez jamais le code du module (ni un long extrait remanié) directement \
dans le texte de votre réponse — le professeur ne pourrait pas l'appliquer. \
Vos messages expliquent, les outils modifient.
- Chaque appel est BLOQUANT : le professeur examine votre proposition dans un \
comparatif, l'aperçu du module exécutant déjà le code proposé, et le résultat \
de l'outil vous donne sa décision — acceptée (et appliquée à son éditeur) ou \
rejetée — avec son éventuel commentaire. Une seule proposition à la fois, UN \
fichier par appel. Si une proposition est rejetée avec un commentaire, vous \
pouvez en soumettre une nouvelle version qui en tient compte.
- Un changement qui touche plusieurs fichiers s'enchaîne : annoncez d'abord \
brièvement votre plan, puis appelez un outil, attendez la décision, puis le \
suivant. Faites en sorte que chaque étape laisse le module dans un état qui \
fonctionne (par exemple, ajoutez l'élément HTML avant le JavaScript qui le \
manipule).
- `new_code` est le contenu INTÉGRAL de remplacement du fichier : recopiez à \
l'identique tout ce que vous ne modifiez pas. Le HTML est le contenu du \
`<body>` seul (ni `<html>`, ni `<head>`, ni balise `<style>`/`<script>` : le \
CSS et le JavaScript ont leur propre fichier).\
"""

_REJECTED = "Le professeur a REJETÉ la proposition — le module est inchangé."

_SUMMARY_SCHEMA = {
    "type": "string",
    "description": (
        "Une phrase, en français, décrivant le changement proposé (affichée au professeur)."
    ),
}
_HITL_NOTICE = (
    " ATTEND la décision du professeur : le comparatif lui est présenté dans son "
    "éditeur, avec un aperçu du module tel que votre code le rendrait, et le "
    "résultat de l'appel est sa décision — proposition acceptée (et appliquée) ou "
    "rejetée, avec son éventuel commentaire. Ne modifie rien par lui-même. Une "
    "seule proposition à la fois."
)


def _spec(name: str, label: str, language: str, hint: str) -> AIToolSpec:
    """Spec d'un des trois tools (``refs`` inutilisé : aucune référence en
    paramètre — la cible est le module édité, il n'y en a qu'un)."""
    return AIToolSpec(
        name=name,
        description=(
            f"Propose au professeur une nouvelle version du {label} du module "
            "interactif en cours d'édition et" + _HITL_NOTICE
        ),
        parameters={
            "type": "object",
            "properties": {
                "new_code": {
                    "type": "string",
                    "description": (
                        f"Code {language} INTÉGRAL de remplacement du fichier — "
                        f"recopier à l'identique tout ce qui ne change pas. {hint}"
                    ),
                },
                "summary": _SUMMARY_SCHEMA,
            },
            "required": ["new_code"],
        },
    )


def _html_spec(refs: CourseRefs) -> AIToolSpec:
    return _spec(
        PROPOSE_HTML_EDIT,
        "fichier HTML",
        "HTML",
        "Contenu du `<body>` uniquement, sans balise `<style>` ni `<script>`.",
    )


def _css_spec(refs: CourseRefs) -> AIToolSpec:
    return _spec(
        PROPOSE_CSS_EDIT,
        "fichier CSS",
        "CSS",
        "Feuille de style seule, sans balise `<style>`.",
    )


def _js_spec(refs: CourseRefs) -> AIToolSpec:
    return _spec(
        PROPOSE_JS_EDIT,
        "fichier JavaScript",
        "JavaScript",
        "Script seul, sans balise `<script>` ; il s'exécute une fois le HTML en place.",
    )


def _build_code_handler(name: str, accepted_text: str) -> Handler:
    """Handler d'un tool de proposition de code : validation AVANT l'interrupt
    (échec immédiat, aucun run figé) et idempotente — à la reprise, le tool est
    ré-exécuté depuis le début."""

    async def propose_code_edit(call: AIToolCall) -> AIToolResult:
        _, failure = string_arg(
            call.arguments, "new_code", max_chars=MODULE_CODE_MAX_CHARS, required=True
        )
        if failure is not None:
            return failure
        return hitl_gate(call, accepted_text=accepted_text, rejected_text=_REJECTED)

    propose_code_edit.__name__ = name
    return propose_code_edit


def _accepted(label: str) -> str:
    return (
        f"Le professeur a ACCEPTÉ la proposition et l'a appliquée au fichier {label} "
        "du module."
    )


def _no_rewrite(arguments: dict, refs: CourseRefs) -> dict:
    """Rien à réécrire : le code d'un module ne porte pas de référence courte
    (voir docstring du module)."""
    return arguments


def _proposal_tool(name: str, spec, label: str) -> ProposalTool:
    return ProposalTool(
        name=name,
        spec=spec,
        build_handler=lambda refs: _build_code_handler(name, _accepted(label)),
        rewrite_args=_no_rewrite,
    )


MODULE = EditContext(
    context=CONTEXT_MODULE,
    target=TARGET_MODULE,
    block_type=None,
    type_error_detail="Ce contexte ne s'applique qu'aux modules interactifs",
    system_prompt=edit_system_prompt(_MISSION, _EDIT_RULES, catalog=MODULE_RUNTIME),
    tools=(
        _proposal_tool(PROPOSE_HTML_EDIT, _html_spec, "HTML"),
        _proposal_tool(PROPOSE_CSS_EDIT, _css_spec, "CSS"),
        _proposal_tool(PROPOSE_JS_EDIT, _js_spec, "JavaScript"),
    ),
)
