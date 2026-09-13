"""Questions de l'assistant au professeur (``ask_questions``) — tool HITL de
tous les contextes de conversation.

Quand une demande est ambiguë, le modèle pose au professeur 1 à
:data:`ASK_MAX_QUESTIONS` questions d'un coup, chacune à choix unique ou
multiple, avec des suggestions ; l'interface ajoute toujours un choix libre
« Autre » (le modèle ne le propose jamais). Comme une proposition d'édition,
l'appel **fige le run** (:func:`app.course_assistant.hitl.suspend`, genre
``questions``) : les questions voyagent dans les ``args`` du ``tool_call``
(relayés et persistés tels quels), le front les affiche À LA PLACE du champ
de saisie du chat, et la route de réponse reprend le run avec la valeur de
:func:`resume_value` — le tool est ré-exécuté, son résultat EST la réponse
(:func:`answers_text`) : les choix du professeur, ou son refus de répondre.

La validation (:func:`parse_questions`) ne dépend que des args : elle rejoue
à l'identique à la reprise. Les plafonds sont appliqués ici, jamais en
mot-clé de schéma (inégalement supportés par les providers). La forme de la
réponse attendue (:func:`answer_shape`) accompagne l'interrupt : la route
contrôle une réponse (:func:`answers_error`) AVANT de consommer la reprise.

Hors contexte de l'assistant (tuteur d'exercice : aucun run checkpointé), le
tool n'est jamais exposé.
"""

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.ai import AIToolCall, AIToolResult, AIToolSpec
from app.course_assistant import hitl

ASK_QUESTIONS = "ask_questions"

# Plafonds d'un appel (miroir front : ``core/course-assistant/questions.ts``
# pour la réponse libre).
ASK_MAX_QUESTIONS = 4
ASK_MIN_OPTIONS = 2
ASK_MAX_OPTIONS = 6
ASK_QUESTION_MAX_CHARS = 300
ASK_LABEL_MAX_CHARS = 100
ASK_DESCRIPTION_MAX_CHARS = 200
MAX_QUESTION_OTHER_CHARS = 1_000

# Longueur d'une question recopiée dans le résultat (la carte du fil et le
# replay en gardent la trace sans répéter une longue formulation).
_RESULT_QUESTION_CHARS = 120

# Libellés normalisés (sans casse, accents ni ponctuation) d'un choix libre :
# l'interface l'ajoute d'office, une option qui le doublerait est refusée.
_OTHER_LABELS = frozenset(
    {
        "autre",
        "autres",
        "autre chose",
        "autre reponse",
        "autre a preciser",
        "autre preciser",
        "autre precisez",
        "other",
        "others",
        "something else",
    }
)

_ANSWERED_HEAD = "Le professeur a répondu à vos questions :"
_DECLINED_HEAD = "Le professeur a préféré ne pas répondre à vos questions :"
_DECLINED_TAIL = (
    "Poursuivez avec des hypothèses raisonnables, signalées dans votre réponse, "
    "sans reposer ces questions."
)

ASK_QUESTIONS_SPEC = AIToolSpec(
    name=ASK_QUESTIONS,
    description=(
        f"Pose au professeur 1 à {ASK_MAX_QUESTIONS} questions à choix (unique ou "
        f"multiple), chacune avec {ASK_MIN_OPTIONS} à {ASK_MAX_OPTIONS} suggestions — un "
        "champ libre « Autre » est toujours ajouté — et ATTEND sa réponse : le résultat "
        "de l'appel est sa réponse, ou son refus de répondre."
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": (
                    f"Toutes les questions du moment (1 à {ASK_MAX_QUESTIONS}), posées une "
                    "à une dans cet ordre."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "La question, courte, en texte brut.",
                        },
                        "multi_select": {
                            "type": "boolean",
                            "description": "true : plusieurs choix possibles ; false : un seul.",
                        },
                        "options": {
                            "type": "array",
                            "description": (
                                f"{ASK_MIN_OPTIONS} à {ASK_MAX_OPTIONS} suggestions distinctes "
                                "— jamais « Autre », ajouté automatiquement."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Libellé court, en texte brut.",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "Précision facultative, une phrase courte.",
                                    },
                                },
                                "required": ["label"],
                            },
                        },
                    },
                    "required": ["question", "multi_select", "options"],
                },
            }
        },
        "required": ["questions"],
    },
    blocking=True,
)


@dataclass(frozen=True)
class AskedOption:
    label: str
    description: str | None


@dataclass(frozen=True)
class AskedQuestion:
    text: str
    multi_select: bool
    options: tuple[AskedOption, ...]


def _error(message: str) -> AIToolResult:
    return AIToolResult(content=message, is_error=True)


def _normalized_label(label: str) -> str:
    """Libellé sans casse, accents ni ponctuation, espaces réduits — repli
    sur le libellé en minuscules s'il n'est fait que de ponctuation."""
    decomposed = unicodedata.normalize("NFKD", label.casefold())
    kept = "".join(c if c.isalnum() else " " for c in decomposed if not unicodedata.combining(c))
    return " ".join(kept.split()) or label.casefold()


def _text(
    value: Any, name: str, *, max_chars: int, required: bool
) -> tuple[str | None, AIToolResult | None]:
    """Texte borné, espaces réduits : ``(valeur, None)`` — ``(None, None)``
    si facultatif et absent ou vide — ou ``(None, échec)``."""
    if value is None:
        return None, (_error(f"{name} manquant (texte attendu).") if required else None)
    if not isinstance(value, str):
        return None, _error(f"{name} invalide (texte attendu).")
    text = " ".join(value.split())
    if not text:
        return None, (_error(f"{name} vide.") if required else None)
    if len(text) > max_chars:
        return None, _error(
            f"{name} trop long ({len(text)} caractères, plafond {max_chars}) — raccourcissez."
        )
    return text, None


def _parse_question(item: Any, number: int) -> tuple[AskedQuestion | None, AIToolResult | None]:
    if not isinstance(item, dict):
        return None, _error(f"Question {number} invalide (objet attendu).")
    text, failure = _text(
        item.get("question"),
        f"Question {number}",
        max_chars=ASK_QUESTION_MAX_CHARS,
        required=True,
    )
    if failure is not None:
        return None, failure
    multi_select = item.get("multi_select")
    if multi_select is None:
        multi_select = False
    if not isinstance(multi_select, bool):
        return None, _error(f"Question {number} : multi_select invalide (booléen attendu).")
    raw_options = item.get("options")
    if not isinstance(raw_options, list):
        return None, _error(f"Question {number} : options manquantes (liste attendue).")
    if not ASK_MIN_OPTIONS <= len(raw_options) <= ASK_MAX_OPTIONS:
        return None, _error(
            f"Question {number} : {len(raw_options)} option(s), il en faut de "
            f"{ASK_MIN_OPTIONS} à {ASK_MAX_OPTIONS}."
        )
    options: list[AskedOption] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_options, start=1):
        name = f"Question {number}, option {index}"
        if not isinstance(raw, dict):
            return None, _error(f"{name} invalide (objet attendu).")
        label, failure = _text(raw.get("label"), name, max_chars=ASK_LABEL_MAX_CHARS, required=True)
        if failure is not None:
            return None, failure
        normalized = _normalized_label(label)
        if normalized in _OTHER_LABELS:
            return None, _error(
                f"{name} : « {label} » est inutile, un choix libre « Autre » est "
                "toujours ajouté automatiquement — retirez cette option."
            )
        if normalized in seen:
            return None, _error(f"{name} : « {label} » en double.")
        seen.add(normalized)
        description, failure = _text(
            raw.get("description"),
            f"{name}, description",
            max_chars=ASK_DESCRIPTION_MAX_CHARS,
            required=False,
        )
        if failure is not None:
            return None, failure
        options.append(AskedOption(label=label, description=description))
    return AskedQuestion(text=text, multi_select=multi_select, options=tuple(options)), None


def parse_questions(
    arguments: Mapping[str, Any],
) -> tuple[tuple[AskedQuestion, ...] | None, AIToolResult | None]:
    """Questions d'un appel, validées : ``(questions, None)`` ou ``(None,
    échec)`` au message actionnable. Pure — elle rejoue à l'identique à la
    reprise — et l'ordre des options est celui des args (les réponses
    désignent les options par leur index)."""
    raw = arguments.get("questions")
    if not isinstance(raw, list) or not raw:
        return None, _error(
            f"Paramètre questions manquant ou invalide (liste de 1 à {ASK_MAX_QUESTIONS} "
            "questions attendue)."
        )
    if len(raw) > ASK_MAX_QUESTIONS:
        return None, _error(
            f"Trop de questions ({len(raw)}, plafond {ASK_MAX_QUESTIONS}) — gardez les "
            "plus structurantes."
        )
    questions: list[AskedQuestion] = []
    for number, item in enumerate(raw, start=1):
        question, failure = _parse_question(item, number)
        if failure is not None:
            return None, failure
        questions.append(question)
    return tuple(questions), None


def answer_shape(questions: Sequence[AskedQuestion]) -> list[dict[str, Any]]:
    """Forme de la réponse attendue, une entrée par question — relayée avec
    l'interrupt et retenue au registre pour contrôler la réponse."""
    return [{"multi_select": q.multi_select, "options": len(q.options)} for q in questions]


def answers_error(
    shape: Sequence[Mapping[str, Any]], answers: Sequence[Mapping[str, Any]]
) -> str | None:
    """Pourquoi ``answers`` ne répond pas aux questions de ``shape`` (``None``
    si elle y répond) : une réponse par question, choix existants et sans
    doublon, au moins un choix ou une réponse libre, un seul en choix unique
    (la réponse libre compte pour un choix)."""
    if len(answers) != len(shape):
        return f"{len(shape)} réponse(s) attendue(s), {len(answers)} reçue(s)"
    for number, (expected, answer) in enumerate(zip(shape, answers, strict=True), start=1):
        selected = list(answer.get("selected") or [])
        if any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < expected["options"]
            for index in selected
        ):
            return f"Question {number} : choix inconnu"
        if len(set(selected)) != len(selected):
            return f"Question {number} : choix en double"
        count = len(selected) + (1 if answer.get("other") else 0)
        if count == 0:
            return f"Question {number} : réponse manquante"
        if not expected["multi_select"] and count > 1:
            return f"Question {number} : un seul choix attendu"
    return None


def resume_value(declined: bool, answers: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Valeur de reprise de l'interrupt. Clés non hexadécimales : LangGraph
    lirait un dict de clés hexadécimales comme une table ``{id: valeur}``."""
    if declined:
        return {"declined": True, "answers": None}
    return {
        "declined": False,
        "answers": [
            {"selected": list(answer.get("selected") or []), "other": answer.get("other")}
            for answer in answers or []
        ],
    }


def _abridged_question(text: str) -> str:
    if len(text) <= _RESULT_QUESTION_CHARS:
        return text
    return text[: _RESULT_QUESTION_CHARS - 1].rstrip() + "…"


def _answer_line(question: AskedQuestion, answer: Any) -> str:
    parts: list[str] = []
    if isinstance(answer, dict):
        for index in answer.get("selected") or []:
            if (
                isinstance(index, int)
                and not isinstance(index, bool)
                and 0 <= index < len(question.options)
            ):
                parts.append(question.options[index].label)
        other = answer.get("other")
        if isinstance(other, str) and other.strip():
            parts.append(f"réponse libre : « {other.strip()} »")
    return " ; ".join(parts) if parts else "(sans réponse)"


def answers_text(questions: Sequence[AskedQuestion], resume: Any) -> str:
    """Résultat du tool : les questions (abrégées) et les choix du
    professeur, ou son refus. Une valeur de reprise malformée vaut refus
    (défensif : la route a déjà contrôlé la réponse)."""
    answers = None
    if isinstance(resume, dict) and resume.get("declined") is False:
        answers = resume.get("answers")
    if not isinstance(answers, list) or len(answers) != len(questions):
        listed = [
            f"{number}. {_abridged_question(q.text)}" for number, q in enumerate(questions, start=1)
        ]
        return "\n".join([_DECLINED_HEAD, *listed, _DECLINED_TAIL])
    lines = [_ANSWERED_HEAD]
    for number, (question, answer) in enumerate(zip(questions, answers, strict=True), start=1):
        lines.append(
            f"{number}. {_abridged_question(question.text)} → {_answer_line(question, answer)}"
        )
    return "\n".join(lines)


async def handle_ask_questions(call: AIToolCall) -> AIToolResult:
    """Exécuteur du tool : validation (échec immédiat, aucun run figé), puis
    attente de la réponse du professeur, dont le texte est le résultat."""
    questions, failure = parse_questions(call.arguments)
    if failure is not None:
        return failure
    resume = hitl.suspend(call, kind=hitl.KIND_QUESTIONS, answer_shape=answer_shape(questions))
    return AIToolResult(content=answers_text(questions, resume))
