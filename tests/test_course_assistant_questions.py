"""Tests du tool ``ask_questions`` (questions de l'assistant au professeur) —
purs : validation des args, forme et contrôle des réponses, texte du résultat,
exécuteur (``hitl.agent_interrupt`` mocké : l'aller-retour réel du graphe est
couvert par ``test_ai_agent.py``) et exposition du tool. Les routes (interrupt,
reprise, 404/422) sont dans ``test_course_assistant_questions_api.py``.
"""

import pytest

from app.core.ai import AIToolCall
from app.course_assistant import hitl
from app.course_assistant.context import build_refs
from app.course_assistant.editing import BLOCK_TEXT, EDIT_CONTEXTS
from app.course_assistant.prompts import COURSE_SYSTEM_PROMPT, QUESTIONS_RULE
from app.course_assistant.questions import (
    ASK_DESCRIPTION_MAX_CHARS,
    ASK_LABEL_MAX_CHARS,
    ASK_MAX_OPTIONS,
    ASK_MAX_QUESTIONS,
    ASK_QUESTION_MAX_CHARS,
    ASK_QUESTIONS,
    ASK_QUESTIONS_SPEC,
    AskedOption,
    AskedQuestion,
    answer_shape,
    answers_error,
    answers_text,
    parse_questions,
    resume_value,
)
from app.course_assistant.tools import build_tool_executor, build_tool_specs
from app.student_exercises.prompts import TUTOR_SYSTEM_PROMPT


def _question(text="Quel niveau visez-vous ?", multi_select=False, labels=("Seconde", "Première")):
    return {
        "question": text,
        "multi_select": multi_select,
        "options": [{"label": label} for label in labels],
    }


LEVEL = AskedQuestion(
    text="Quel niveau visez-vous ?",
    multi_select=False,
    options=(AskedOption("Seconde", None), AskedOption("Première", "Spécialité maths")),
)
NOTIONS = AskedQuestion(
    text="Quelles notions inclure ?",
    multi_select=True,
    options=(
        AskedOption("Dérivée", None),
        AskedOption("Limites", None),
        AskedOption("Suites", None),
    ),
)


# ---------------------------------------------------------------- spec


def _schema_keys(node) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            keys |= _schema_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _schema_keys(item)
    return keys


def test_ask_questions_spec_is_static_blocking_and_keyword_free() -> None:
    """Spec statique (préfixe cacheable), bloquante (garde « un par réponse »),
    sans mot-clé de plafond — appliqués en code, inégalement supportés par les
    providers ; la description annonce le choix libre ajouté d'office."""
    assert ASK_QUESTIONS_SPEC.name == ASK_QUESTIONS
    assert ASK_QUESTIONS_SPEC.blocking is True
    keys = _schema_keys(ASK_QUESTIONS_SPEC.parameters)
    assert keys.isdisjoint({"maxLength", "minItems", "maxItems", "additionalProperties"})
    assert "Autre" in ASK_QUESTIONS_SPEC.description
    items = ASK_QUESTIONS_SPEC.parameters["properties"]["questions"]["items"]
    assert items["required"] == ["question", "multi_select", "options"]


# ---------------------------------------------------------------- parsing


def test_parse_questions_accepts_and_normalizes() -> None:
    """Espaces réduits, description vide ignorée, ``multi_select`` absent =
    choix unique ; l'ordre des options est celui des args (les réponses
    désignent les options par index)."""
    arguments = {
        "questions": [
            {
                "question": "  Quel   niveau\nvisez-vous ? ",
                "options": [
                    {"label": " Seconde ", "description": "   "},
                    {"label": "Première", "description": "Spécialité maths"},
                ],
            },
            _question("Quelles notions inclure ?", True, ("Dérivée", "Limites", "Suites")),
        ]
    }
    questions, failure = parse_questions(arguments)
    assert failure is None
    assert questions == (LEVEL, NOTIONS)


@pytest.mark.parametrize(
    ("arguments", "needle"),
    [
        ({}, "questions manquant"),
        ({"questions": []}, "questions manquant"),
        ({"questions": [_question()] * (ASK_MAX_QUESTIONS + 1)}, "Trop de questions"),
        ({"questions": ["Niveau ?"]}, "Question 1 invalide"),
        ({"questions": [_question(text="  ")]}, "Question 1 vide"),
        ({"questions": [_question(text="x" * (ASK_QUESTION_MAX_CHARS + 1))]}, "trop long"),
        ({"questions": [{**_question(), "multi_select": "oui"}]}, "multi_select invalide"),
        ({"questions": [{**_question(), "options": "Seconde"}]}, "options manquantes"),
        ({"questions": [_question(labels=("Seconde",))]}, "il en faut de 2"),
        (
            {"questions": [_question(labels=tuple(f"N{i}" for i in range(ASK_MAX_OPTIONS + 1)))]},
            "il en faut de 2",
        ),
        ({"questions": [{**_question(), "options": ["Seconde", "Première"]}]}, "option 1 invalide"),
        ({"questions": [_question(labels=("Seconde", " "))]}, "option 2 vide"),
        (
            {"questions": [_question(labels=("Seconde", "x" * (ASK_LABEL_MAX_CHARS + 1)))]},
            "trop long",
        ),
        ({"questions": [_question(labels=("Seconde", "seconde"))]}, "en double"),
        ({"questions": [_question(labels=("Seconde", "Autre"))]}, "choix libre"),
        ({"questions": [_question(labels=("Seconde", "Autre (précisez)"))]}, "choix libre"),
        ({"questions": [_question(labels=("Seconde", "Other"))]}, "choix libre"),
        (
            {
                "questions": [
                    {
                        **_question(),
                        "options": [
                            {"label": "Seconde"},
                            {
                                "label": "Première",
                                "description": "d" * (ASK_DESCRIPTION_MAX_CHARS + 1),
                            },
                        ],
                    }
                ]
            },
            "description trop long",
        ),
        ({"questions": [_question(), _question(labels=("A", "A"))]}, "Question 2, option 2"),
    ],
)
def test_parse_questions_rejections(arguments, needle) -> None:
    questions, failure = parse_questions(arguments)
    assert questions is None
    assert failure is not None and failure.is_error
    assert needle in failure.content


def test_parse_questions_keeps_symbol_only_labels_distinct() -> None:
    """Des libellés sans lettre latine (symboles, alphabet grec) ne se
    confondent pas à la normalisation."""
    questions, failure = parse_questions({"questions": [_question(labels=("α", "β", "+", "−"))]})
    assert failure is None
    assert [o.label for o in questions[0].options] == ["α", "β", "+", "−"]


# ---------------------------------------------------------------- réponses


def test_answer_shape() -> None:
    assert answer_shape((LEVEL, NOTIONS)) == [
        {"multi_select": False, "options": 2},
        {"multi_select": True, "options": 3},
    ]


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ([{"selected": [1]}, {"selected": [0, 2], "other": "Tangentes"}], None),
        ([{"selected": [], "other": "Terminale"}, {"selected": [1]}], None),
        ([{"selected": [0]}], "2 réponse(s) attendue(s), 1 reçue(s)"),
        ([{"selected": [2]}, {"selected": [0]}], "Question 1 : choix inconnu"),
        ([{"selected": [-1]}, {"selected": [0]}], "Question 1 : choix inconnu"),
        ([{"selected": [True]}, {"selected": [0]}], "Question 1 : choix inconnu"),
        ([{"selected": [0]}, {"selected": [1, 1]}], "Question 2 : choix en double"),
        ([{"selected": []}, {"selected": [0]}], "Question 1 : réponse manquante"),
        (
            [{"selected": [0], "other": None}, {"selected": [], "other": ""}],
            "Question 2 : réponse manquante",
        ),
        ([{"selected": [0, 1]}, {"selected": [0]}], "Question 1 : un seul choix attendu"),
        (
            [{"selected": [0], "other": "Terminale"}, {"selected": [0]}],
            "Question 1 : un seul choix attendu",
        ),
    ],
)
def test_answers_error_rules(answers, expected) -> None:
    assert answers_error(answer_shape((LEVEL, NOTIONS)), answers) == expected


def test_resume_value_nests_answers_under_plain_keys() -> None:
    assert resume_value(True, [{"selected": [0]}]) == {"declined": True, "answers": None}
    assert resume_value(False, [{"selected": [1], "other": None}]) == {
        "declined": False,
        "answers": [{"selected": [1], "other": None}],
    }


def test_answers_text_answered() -> None:
    """Le résultat liste chaque question (abrégée) et les choix du professeur,
    réponse libre comprise, dans l'ordre des options choisies."""
    long_question = AskedQuestion(text="Q" * 200, multi_select=False, options=LEVEL.options)
    text = answers_text(
        (LEVEL, NOTIONS, long_question),
        resume_value(
            False,
            [
                {"selected": [1], "other": None},
                {"selected": [2, 0], "other": "les tangentes"},
                {"selected": [], "other": "Terminale"},
            ],
        ),
    )
    lines = text.split("\n")
    assert lines[0] == "Le professeur a répondu à vos questions :"
    assert lines[1] == "1. Quel niveau visez-vous ? → Première"
    assert (
        lines[2]
        == "2. Quelles notions inclure ? → Suites ; Dérivée ; réponse libre : « les tangentes »"
    )
    assert lines[3].startswith("3. " + "Q" * 50) and "…" in lines[3]
    assert lines[3].endswith("→ réponse libre : « Terminale »")


def test_answers_text_declined_and_defensive() -> None:
    """Refus : les questions restent listées (la carte du fil se suffit) et le
    modèle est invité à poursuivre sans les reposer ; une valeur de reprise
    malformée vaut refus."""
    declined = answers_text((LEVEL, NOTIONS), resume_value(True, None))
    assert declined.split("\n")[:3] == [
        "Le professeur a préféré ne pas répondre à vos questions :",
        "1. Quel niveau visez-vous ?",
        "2. Quelles notions inclure ?",
    ]
    assert "sans reposer ces questions" in declined
    for malformed in (None, "oui", {"declined": False, "answers": [{"selected": [0]}]}, {}):
        assert answers_text((LEVEL, NOTIONS), malformed) == declined
    # Index invalides ignorés : la ligne reste lisible.
    odd = answers_text((LEVEL,), {"declined": False, "answers": [{"selected": [9, "x"]}]})
    assert odd.endswith("→ (sans réponse)")


# ---------------------------------------------------------------- exécuteur


def _questions_executor():
    return build_tool_executor(None, build_refs([], [], []), questions=True)


@pytest.mark.anyio
async def test_executor_ask_questions_validates_before_interrupting(monkeypatch) -> None:
    """Args invalides : échec immédiat, JAMAIS d'interrupt (aucun run figé)."""
    monkeypatch.setattr(hitl, "agent_interrupt", lambda payload: pytest.fail("interrupt inattendu"))
    result = await _questions_executor()(
        AIToolCall(id="call_q", name=ASK_QUESTIONS, arguments={"questions": []})
    )
    assert result.is_error


@pytest.mark.anyio
async def test_executor_ask_questions_returns_the_answers(monkeypatch) -> None:
    """Le payload de l'interrupt porte la clé de reprise, le genre et la forme
    de réponse ; le résultat du tool EST la réponse du professeur."""
    seen: list[dict] = []
    reply = resume_value(False, [{"selected": [0], "other": None}])
    monkeypatch.setattr(hitl, "agent_interrupt", lambda payload: seen.append(payload) or reply)
    result = await _questions_executor()(
        AIToolCall(id="call_q", name=ASK_QUESTIONS, arguments={"questions": [_question()]})
    )
    assert not result.is_error
    assert (
        result.content
        == "Le professeur a répondu à vos questions :\n1. Quel niveau visez-vous ? → Seconde"
    )
    assert seen == [
        {
            "tool_call_id": "call_q",
            "kind": "questions",
            "answer_shape": [{"multi_select": False, "options": 2}],
        }
    ]


@pytest.mark.anyio
async def test_ask_questions_absent_by_default() -> None:
    """Sans ``questions=True`` (tuteur d'exercice), ni spec ni handler."""
    refs = build_refs([], [], [])
    assert ASK_QUESTIONS not in {spec.name for spec in build_tool_specs(refs)}
    result = await build_tool_executor(None, refs)(
        AIToolCall(id="call_q", name=ASK_QUESTIONS, arguments={"questions": [_question()]})
    )
    assert result.is_error and "inconnu" in result.content


def test_tool_specs_mark_hitl_tools_blocking() -> None:
    """Tools de proposition et questions sont bloquants ; les lectures non."""
    refs = build_refs([], [], [])
    specs = {spec.name: spec for spec in build_tool_specs(refs, edit=BLOCK_TEXT, questions=True)}
    assert specs["propose_block_edit"].blocking is True
    assert specs[ASK_QUESTIONS].blocking is True
    assert specs["read_block"].blocking is False


# ---------------------------------------------------------------- prompts


def test_questions_rule_reaches_every_assistant_prompt_never_the_tutor() -> None:
    assert QUESTIONS_RULE in COURSE_SYSTEM_PROMPT
    assert EDIT_CONTEXTS
    for edit in EDIT_CONTEXTS.values():
        assert QUESTIONS_RULE in edit.system_prompt, edit.context
    assert QUESTIONS_RULE not in TUTOR_SYSTEM_PROMPT
    assert "ask_questions" not in TUTOR_SYSTEM_PROMPT
