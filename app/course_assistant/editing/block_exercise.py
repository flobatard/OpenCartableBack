"""Contexte d'édition d'un bloc exercice (``block_exercise``) — HITL par question.

La proposition est **unitaire**, pas
un remplacement de l'exercice entier — quatre tools de proposition, une
opération par appel, une proposition à la fois (le modèle enchaîne les appels
après chaque décision) :

- ``propose_statement_edit`` : sujet (énoncé général) markdown intégral ;
- ``propose_question_edit`` : énoncé et/ou corrigé d'une question existante ;
- ``propose_question_add`` : nouvelle question (position ``after_ref``, fin
  par défaut) ;
- ``propose_question_delete`` : suppression d'une question.

Les questions existantes sont désignées par leur **référence courte** ``Q1…``
(:mod:`app.course_assistant.refs`, genre ``question`` — construit du seul bloc
édité, ``enum`` des specs, résolveur tolérant), **stable le temps du tour**
(reprises comprises) : le modèle ne manipule jamais l'id d'une question.
À l'émission, ``rewrite_args`` ajoute l'id résolu (``question_id`` /
``after_id`` — la référence est conservée pour le replay) et réécrit les liens
de contenu ``oc-resource:``/``oc-module:`` des champs markdown : le front
reçoit un payload directement applicable au formulaire de l'éditeur, qui
applique lui-même sur acceptation (ids de questions stables — le PATCH du
bloc conserve les ids existants et le back génère ceux des nouvelles).
À la reprise d'un ajout accepté, la référence de la question ajoutée
(:attr:`~app.course_assistant.refs.CourseRefs.new_question_refs`, instantané
rechargé) est donnée au modèle dans le résultat du tool — aucune relecture du
bloc nécessaire.

Plafonds miroir de ``ExerciseContent``/``ExerciseQuestion``
(app/courses/schemas.py), appliqués en validation — pas de ``maxLength``/
``maxItems`` dans les schémas (mots-clés inégalement supportés par les
providers).
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
    string_arg,
    tool_error,
)
from app.course_assistant.prompts import CONTENT_PRESERVATION_RULE, edit_system_prompt
from app.course_assistant.refs import CourseRefs, RefEntry
from app.models.ai_conversation import CONTEXT_BLOCK_EXERCISE
from app.models.block import TYPE_EXERCISE

PROPOSE_STATEMENT_EDIT = "propose_statement_edit"
PROPOSE_QUESTION_EDIT = "propose_question_edit"
PROPOSE_QUESTION_ADD = "propose_question_add"
PROPOSE_QUESTION_DELETE = "propose_question_delete"

STATEMENT_MAX_CHARS = 100_000
QUESTION_MAX_CHARS = 20_000
QUESTIONS_MAX = 50

_MISSION = """\
Vous êtes l'assistant pédagogique d'OpenCartable, aux côtés d'un professeur \
qui édite un exercice de son cours : un sujet, suivi de questions à réponse \
libre portant chacune le corrigé du professeur. Vous l'aidez à améliorer, \
compléter ou restructurer cet exercice (clarté des énoncés, progression des \
questions, exactitude des corrigés, cohérence avec le reste du cours).\
"""

_EDIT_RULES = f"""\
Règles d'édition de l'exercice : quatre outils — `propose_statement_edit` (le \
sujet), `propose_question_edit` (énoncé et/ou corrigé d'une question \
existante), `propose_question_add` (nouvelle question) et \
`propose_question_delete` (suppression). UNE opération par appel : pour \
plusieurs questions, enchaîner les appels, chacun après la décision du \
précédent. Les questions existantes sont désignées par leur référence (ref: \
Q1, Q2…), stable pendant tout l'échange : une question supprimée libère sa \
référence (jamais réattribuée), une question ajoutée reçoit la suivante \
(indiquée dans le résultat de l'outil). Le sujet et les énoncés sont en \
markdown ; `expected_answer` est le corrigé du professeur, en texte simple, \
jamais montré aux élèves. L'exercice édité est fourni en entier dans le \
message du tour.

{CONTENT_PRESERVATION_RULE}\
"""

_REJECTED = "Le professeur a REJETÉ la proposition — l'exercice est inchangé."
_ACCEPTED_STATEMENT = (
    "Le professeur a ACCEPTÉ la proposition et l'a appliquée au sujet de l'exercice."
)
_ACCEPTED_QUESTION_EDIT = "Le professeur a ACCEPTÉ la proposition et l'a appliquée à la question."
_ACCEPTED_QUESTION_ADD = (
    "Le professeur a ACCEPTÉ la proposition : la question a été ajoutée à l'exercice."
)
_ACCEPTED_QUESTION_ADD_REREAD = (
    f"{_ACCEPTED_QUESTION_ADD} Relisez le bloc (`read_block`) pour connaître sa "
    "référence avant toute autre modification la concernant."
)
_ACCEPTED_QUESTION_DELETE = (
    "Le professeur a ACCEPTÉ la proposition : la question a été supprimée de l'exercice ; "
    "sa référence n'est plus valide, les autres questions conservent la leur."
)


# ------------------------------------------------------------------- specs


def _question_ref_schema(refs: CourseRefs, description: str) -> dict:
    """Paramètre « référence de question », ``enum`` des références valides
    (omis si vide — motif ``_ref_spec``)."""
    schema: dict = {"type": "string", "description": description}
    question_refs = refs.refs("question")
    if question_refs:
        schema["enum"] = question_refs
    return schema


def _statement_spec(refs: CourseRefs) -> AIToolSpec:
    return AIToolSpec(
        name=PROPOSE_STATEMENT_EDIT,
        description=hitl_description(
            "Propose au professeur une nouvelle version du SUJET (énoncé général) de "
            "l'exercice en cours d'édition"
        ),
        parameters={
            "type": "object",
            "properties": {
                "new_statement": {
                    "type": "string",
                    "description": (
                        "Sujet de l'exercice, markdown INTÉGRAL de remplacement "
                        "(l'inchangé recopié à l'identique)."
                    ),
                },
                "summary": SUMMARY_SCHEMA,
            },
            "required": ["new_statement"],
        },
    )


def _question_edit_spec(refs: CourseRefs) -> AIToolSpec:
    return AIToolSpec(
        name=PROPOSE_QUESTION_EDIT,
        description=hitl_description(
            "Propose au professeur la modification d'UNE question existante de "
            "l'exercice (son énoncé et/ou son corrigé)"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question_ref": _question_ref_schema(
                    refs, "Référence de la question à modifier (ex. Q2)."
                ),
                "statement": {
                    "type": "string",
                    "description": (
                        "Nouvel énoncé, markdown INTÉGRAL de remplacement — omettre "
                        "pour conserver l'énoncé actuel."
                    ),
                },
                "expected_answer": {
                    "type": "string",
                    "description": (
                        "Nouveau corrigé, texte simple sans markdown — omettre pour "
                        "conserver le corrigé actuel."
                    ),
                },
                "summary": SUMMARY_SCHEMA,
            },
            "required": ["question_ref"],
        },
    )


def _question_add_spec(refs: CourseRefs) -> AIToolSpec:
    return AIToolSpec(
        name=PROPOSE_QUESTION_ADD,
        description=hitl_description(
            "Propose au professeur l'AJOUT d'une nouvelle question à l'exercice"
        ),
        parameters={
            "type": "object",
            "properties": {
                "statement": {
                    "type": "string",
                    "description": "Énoncé de la nouvelle question, en markdown.",
                },
                "expected_answer": {
                    "type": "string",
                    "description": (
                        "Corrigé de la nouvelle question, texte simple sans markdown "
                        "— chaîne vide si aucun."
                    ),
                },
                "after_ref": _question_ref_schema(
                    refs,
                    "Référence de la question APRÈS laquelle insérer (ex. Q2) — "
                    "omettre pour ajouter en fin d'exercice.",
                ),
                "summary": SUMMARY_SCHEMA,
            },
            "required": ["statement"],
        },
    )


def _question_delete_spec(refs: CourseRefs) -> AIToolSpec:
    return AIToolSpec(
        name=PROPOSE_QUESTION_DELETE,
        description=hitl_description(
            "Propose au professeur la SUPPRESSION d'une question de l'exercice"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question_ref": _question_ref_schema(
                    refs, "Référence de la question à supprimer (ex. Q2)."
                ),
                "summary": SUMMARY_SCHEMA,
            },
            "required": ["question_ref"],
        },
    )


# ---------------------------------------------------------------- handlers


def _resolve_question(refs: CourseRefs, raw) -> tuple[RefEntry | None, AIToolResult | None]:
    """Question du bloc édité par référence (chaîne tolérante de ``resolve``),
    ou le résultat d'échec listant les candidats."""
    resolution = refs.resolve("question", raw)
    if resolution.entry is None:
        return None, tool_error(resolution.error)
    return resolution.entry, None


def _build_statement_handler(refs: CourseRefs) -> Handler:
    async def propose_statement_edit(call: AIToolCall) -> AIToolResult:
        _, failure = string_arg(
            call.arguments, "new_statement", max_chars=STATEMENT_MAX_CHARS, required=True
        )
        if failure is not None:
            return failure
        return hitl_gate(call, accepted_text=_ACCEPTED_STATEMENT, rejected_text=_REJECTED)

    return propose_statement_edit


def _build_question_edit_handler(refs: CourseRefs) -> Handler:
    async def propose_question_edit(call: AIToolCall) -> AIToolResult:
        _, failure = _resolve_question(refs, call.arguments.get("question_ref"))
        if failure is not None:
            return failure
        statement, failure = string_arg(
            call.arguments, "statement", max_chars=QUESTION_MAX_CHARS, required=False
        )
        if failure is not None:
            return failure
        expected, failure = string_arg(
            call.arguments, "expected_answer", max_chars=QUESTION_MAX_CHARS, required=False
        )
        if failure is not None:
            return failure
        if statement is None and expected is None:
            return tool_error(
                "Rien à modifier : fournissez statement (nouvel énoncé) et/ou "
                "expected_answer (nouveau corrigé)."
            )
        return hitl_gate(call, accepted_text=_ACCEPTED_QUESTION_EDIT, rejected_text=_REJECTED)

    return propose_question_edit


def accepted_question_add_text(refs: CourseRefs) -> str:
    """Résultat d'un ajout accepté : la référence de la question ajoutée
    quand l'instantané rechargé à la reprise en révèle exactement une
    (``new_question_refs``) — sinon, invitation à relire le bloc."""
    new_refs = refs.new_question_refs
    if len(new_refs) == 1:
        return f"{_ACCEPTED_QUESTION_ADD} Sa référence est {new_refs[0]}."
    return _ACCEPTED_QUESTION_ADD_REREAD


def _build_question_add_handler(refs: CourseRefs) -> Handler:
    async def propose_question_add(call: AIToolCall) -> AIToolResult:
        _, failure = string_arg(
            call.arguments, "statement", max_chars=QUESTION_MAX_CHARS, required=True
        )
        if failure is not None:
            return failure
        _, failure = string_arg(
            call.arguments, "expected_answer", max_chars=QUESTION_MAX_CHARS, required=False
        )
        if failure is not None:
            return failure
        if len(refs.entries["question"]) >= QUESTIONS_MAX:
            return tool_error(
                f"L'exercice compte déjà {QUESTIONS_MAX} questions (plafond) — "
                "proposez d'en supprimer ou d'en fusionner avant d'ajouter."
            )
        after_ref = call.arguments.get("after_ref")
        if after_ref not in (None, ""):
            _, failure = _resolve_question(refs, after_ref)
            if failure is not None:
                return failure
        # À la reprise, ``refs`` est l'instantané rechargé : la question
        # acceptée y a reçu sa référence (aucune relecture du bloc).
        return hitl_gate(
            call, accepted_text=accepted_question_add_text(refs), rejected_text=_REJECTED
        )

    return propose_question_add


def _build_question_delete_handler(refs: CourseRefs) -> Handler:
    async def propose_question_delete(call: AIToolCall) -> AIToolResult:
        _, failure = _resolve_question(refs, call.arguments.get("question_ref"))
        if failure is not None:
            return failure
        return hitl_gate(
            call, accepted_text=_ACCEPTED_QUESTION_DELETE, rejected_text=_REJECTED
        )

    return propose_question_delete


# ------------------------------------------------ réécriture à l'émission


def _rewrite_markdown(arguments: dict, refs: CourseRefs, key: str) -> dict:
    """Liens de contenu d'un champ markdown réécrits en UUID (motif ``block_text``)."""
    value = arguments.get(key)
    if not isinstance(value, str):
        return arguments
    return {**arguments, key: refs.rewrite_content_refs(value)}


def _resolved_id(refs: CourseRefs, raw) -> str | None:
    """Id (chaîne) de la question visée par une référence, ``None`` si absente
    ou irrésolue (le handler aura répondu par une erreur — jamais d'interrupt)."""
    if raw in (None, ""):
        return None
    resolution = refs.resolve("question", raw)
    return str(resolution.entry.id) if resolution.entry is not None else None


def _rewrite_statement_args(arguments: dict, refs: CourseRefs) -> dict:
    return _rewrite_markdown(arguments, refs, "new_statement")


def _rewrite_question_edit_args(arguments: dict, refs: CourseRefs) -> dict:
    return {
        **_rewrite_markdown(arguments, refs, "statement"),
        "question_id": _resolved_id(refs, arguments.get("question_ref")),
    }


def _rewrite_question_add_args(arguments: dict, refs: CourseRefs) -> dict:
    return {
        **_rewrite_markdown(arguments, refs, "statement"),
        "after_id": _resolved_id(refs, arguments.get("after_ref")),
    }


def _rewrite_question_delete_args(arguments: dict, refs: CourseRefs) -> dict:
    return {**arguments, "question_id": _resolved_id(refs, arguments.get("question_ref"))}


BLOCK_EXERCISE = EditContext(
    context=CONTEXT_BLOCK_EXERCISE,
    target=TARGET_BLOCK,
    block_type=TYPE_EXERCISE,
    type_error_detail="Ce contexte ne s'applique qu'aux blocs exercice",
    system_prompt=edit_system_prompt(_MISSION, _EDIT_RULES),
    tools=(
        ProposalTool(
            name=PROPOSE_STATEMENT_EDIT,
            spec=_statement_spec,
            build_handler=_build_statement_handler,
            rewrite_args=_rewrite_statement_args,
        ),
        ProposalTool(
            name=PROPOSE_QUESTION_EDIT,
            spec=_question_edit_spec,
            build_handler=_build_question_edit_handler,
            rewrite_args=_rewrite_question_edit_args,
        ),
        ProposalTool(
            name=PROPOSE_QUESTION_ADD,
            spec=_question_add_spec,
            build_handler=_build_question_add_handler,
            rewrite_args=_rewrite_question_add_args,
        ),
        ProposalTool(
            name=PROPOSE_QUESTION_DELETE,
            spec=_question_delete_spec,
            build_handler=_build_question_delete_handler,
            rewrite_args=_rewrite_question_delete_args,
        ),
    ),
)
