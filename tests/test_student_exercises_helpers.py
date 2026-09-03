"""Helpers purs du tuteur d'exercice élève : redaction, contexte, historique,
verdict — aucun réseau, Postgres ni S3."""

import uuid
from types import SimpleNamespace

import pytest

from app.core.ai import AIToolCall
from app.course_assistant.context import build_refs
from app.student_exercises.context import (
    NO_EXPECTED_ANSWER_NOTICE,
    RedactedBlock,
    build_tutor_context,
    find_question,
    history_messages,
    redact_blocks,
    student_message,
)
from app.student_exercises.streaming import (
    RECORD_VERDICT,
    VerdictHolder,
    build_tutor_executor,
    guard_reveal,
)

Q1 = uuid.uuid4()
Q2 = uuid.uuid4()
OTHER_Q = uuid.uuid4()
SECRET_TARGET = "corrigé-cible-7f3a"
SECRET_SIBLING = "corrigé-voisin-91bc"
SECRET_OTHER = "corrigé-autre-exercice-c0de"


def _block(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        course_id=uuid.uuid4(),
        position=0,
        type="exercise",
        title="Pythagore",
        description=None,
        content={
            "statement": "Triangle rectangle.",
            "questions": [
                {"id": str(Q1), "statement": "Hypoténuse ?", "type": "free_text",
                 "expected_answer": SECRET_TARGET},
                {"id": str(Q2), "statement": "Aire ?", "type": "free_text",
                 "expected_answer": SECRET_SIBLING},
            ],
        },
        resource_id=None,
        module_id=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _course():
    return SimpleNamespace(title="Géométrie", description="Cours de 4e")


def test_redact_blocks_strips_every_expected_answer() -> None:
    text = _block(type="text", content={"markdown": "Le théorème…"})
    exercise = _block()
    [r_text, r_ex] = redact_blocks([text, exercise])
    assert isinstance(r_ex, RedactedBlock)
    assert r_text.content == {"markdown": "Le théorème…"}
    assert [q["id"] for q in r_ex.content["questions"]] == [str(Q1), str(Q2)]
    assert "expected_answer" not in str(r_ex.content)
    # Copie, jamais l'original.
    assert r_ex.content is not exercise.content


def test_find_question_numbering() -> None:
    block = _block()
    assert find_question(block, Q1)[0] == 1
    number, question = find_question(block, Q2)
    assert number == 2 and question["expected_answer"] == SECRET_SIBLING
    assert find_question(block, OTHER_Q) is None


def test_tutor_context_only_carries_the_target_answer() -> None:
    other = _block(
        position=1,
        title="Thalès",
        content={
            "statement": "Autre exercice",
            "questions": [{"id": str(OTHER_Q), "statement": "?", "type": "free_text",
                           "expected_answer": SECRET_OTHER}],
        },
    )
    target = _block()
    redacted = redact_blocks([target, other])
    refs = build_refs(redacted, [], [])
    context = build_tutor_context(
        _course(), refs, block=redacted[0], question_number=1,
        expected_answer=SECRET_TARGET,
    )
    assert "## Exercice en cours de résolution" in context
    assert "**Question 1**" in context
    assert "## Corrigé confidentiel de la question 1" in context
    assert SECRET_TARGET in context
    # Garde-fou : aucun autre corrigé, sous aucune forme.
    assert SECRET_SIBLING not in context
    assert SECRET_OTHER not in context
    assert "Réponse attendue" not in context
    # L'exercice est remplacé par un pointeur dans la liste du cours ; l'autre
    # bloc y est rendu (redacté).
    assert "(exercice en cours de résolution — contenu complet" in context
    assert "Autre exercice" in context
    assert "Tutoie" in context and "record_verdict" in context


def test_tutor_context_without_expected_answer() -> None:
    redacted = redact_blocks([_block()])
    refs = build_refs(redacted, [], [])
    context = build_tutor_context(
        _course(), refs, block=redacted[0], question_number=2, expected_answer="  "
    )
    assert NO_EXPECTED_ANSWER_NOTICE in context


def test_history_messages_and_student_message() -> None:
    rows = [
        SimpleNamespace(kind="answer", content="5", feedback="Relis le cours."),
        SimpleNamespace(kind="message", content="Aide", feedback=None),
    ]
    messages = history_messages(rows)
    assert [m.role for m in messages] == ["user", "assistant", "user"]
    assert messages[0].content == student_message("answer", "5")
    assert messages[0].content.startswith("Réponse de l'élève :")
    assert messages[2].content.startswith("Message de l'élève :")
    assert history_messages(rows, limit=1)[0].content.startswith("Message")


@pytest.mark.parametrize(
    ("verdict", "effort", "reveal", "expected"),
    [
        ("correct", "insufficient", True, True),
        ("incorrect", "sufficient", True, True),
        ("incorrect", "insufficient", True, False),
        ("none", "insufficient", True, False),
        ("correct", "sufficient", False, False),
    ],
)
def test_guard_reveal(verdict, effort, reveal, expected) -> None:
    assert guard_reveal(verdict, effort, reveal) is expected


@pytest.mark.anyio
async def test_record_verdict_executor() -> None:
    redacted = redact_blocks([_block()])
    refs = build_refs(redacted, [], [])
    holder = VerdictHolder()
    execute = build_tutor_executor(SimpleNamespace(), refs, holder)

    bad = await execute(AIToolCall(id="c1", name=RECORD_VERDICT, arguments={"verdict": "x"}))
    assert bad.is_error and "verdict" in bad.content
    assert holder.recorded is False

    ok = await execute(
        AIToolCall(
            id="c2",
            name=RECORD_VERDICT,
            arguments={"verdict": "incorrect", "effort": "insufficient", "reveal": True},
        )
    )
    assert not ok.is_error
    assert holder.recorded and holder.verdict == "incorrect"
    # Garde serveur : la révélation demandée est refusée.
    assert holder.reveal is False
    assert "confidentiel" in ok.content

    # Les lectures du cours restent servies par l'exécuteur de l'assistant,
    # sur l'instantané redacté.
    read = await execute(AIToolCall(id="c3", name="read_block", arguments={"block_ref": "B1"}))
    assert not read.is_error
    assert "Hypoténuse" in read.content
    assert SECRET_TARGET not in read.content
