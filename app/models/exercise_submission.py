"""Tentatives d'un élève sur une question d'exercice, et le retour du tuteur IA
(dernière brique du J5 côté élève — :mod:`app.student_exercises`).

**Une ligne par tour** de l'élève sur une question : soit une **réponse**
soumise à la correction (``kind = 'answer'``), soit un **message** libre
adressé au tuteur (``kind = 'message'`` — demande d'aide, question sur le
cours). Le fil d'une question = ses lignes triées ``created_at, id`` ; c'est
ce fil que le tuteur relit pour juger l'effort de l'élève (décision actée :
persistance par tentative, révise le « sans persistance » du cadrage initial).

La ligne est insérée **avant** l'appel provider (durable même si l'appel
échoue — ``feedback`` reste NULL) et complétée à la clôture du flux :

- ``feedback`` : retour du tuteur en markdown de cours (texte partiel persisté
  sur erreur mid-stream) ;
- ``verdict`` : jugement structuré du tuteur sur la réponse — ``correct`` /
  ``partial`` / ``incorrect``, ou ``none`` (message d'aide, ou aucune réponse
  évaluable) ; ``effort`` : ``sufficient`` / ``insufficient`` ;
- ``revealed`` : le corrigé du professeur a été révélé à ce tour — c'est le
  BACK qui le joint (verbatim, jamais copié ici : relu dans le bloc à la
  lecture du fil), sur la décision structurée du modèle bornée par une garde
  serveur (:mod:`app.student_exercises.streaming`).

``question_id`` désigne ``blocks.content.questions[].id`` (uuid **stable à
vie**, contrat de :mod:`app.models.block`) — pas de FK possible vers un
élément de JSONB. FK toutes en ``CASCADE`` (utilisateur, cours, bloc) ;
aucune relation ORM (lazy-load async interdit).

Purge : la tâche existe (``app/maintenance/``, ``PURGE_EXERCISE_SUBMISSIONS_DAYS``)
mais est **désactivée par défaut** — ce sont des données personnelles d'élèves,
et l'effacement manuel existe des deux côtés (l'élève ses tours, le prof ceux de
tous ses élèves). L'opérateur l'active en posant une rétention.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

KIND_ANSWER = "answer"
KIND_MESSAGE = "message"
KINDS = (KIND_ANSWER, KIND_MESSAGE)

VERDICT_CORRECT = "correct"
VERDICT_PARTIAL = "partial"
VERDICT_INCORRECT = "incorrect"
VERDICT_NONE = "none"
VERDICTS = (VERDICT_CORRECT, VERDICT_PARTIAL, VERDICT_INCORRECT, VERDICT_NONE)

EFFORT_SUFFICIENT = "sufficient"
EFFORT_INSUFFICIENT = "insufficient"
EFFORTS = (EFFORT_SUFFICIENT, EFFORT_INSUFFICIENT)


class ExerciseSubmission(Base):
    __tablename__ = "exercise_submissions"
    __table_args__ = (
        CheckConstraint(
            f"kind IN ('{KIND_ANSWER}', '{KIND_MESSAGE}')",
            name="ck_exercise_submissions_kind",
        ),
        CheckConstraint(
            f"verdict IS NULL OR verdict IN ('{VERDICT_CORRECT}', '{VERDICT_PARTIAL}', "
            f"'{VERDICT_INCORRECT}', '{VERDICT_NONE}')",
            name="ck_exercise_submissions_verdict",
        ),
        CheckConstraint(
            f"effort IS NULL OR effort IN ('{EFFORT_SUFFICIENT}', '{EFFORT_INSUFFICIENT}')",
            name="ck_exercise_submissions_effort",
        ),
        # Le fil d'une question pour un élève (tri created_at, id).
        Index(
            "ix_exercise_submissions_thread",
            "user_id",
            "block_id",
            "question_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Pas d'index simple : l'index du fil (user_id en tête) le couvre.
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    block_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("blocks.id", ondelete="CASCADE"), index=True
    )
    question_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    feedback: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(String(20))
    effort: Mapped[str | None] = mapped_column(String(20))
    revealed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
