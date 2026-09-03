"""Schémas Pydantic du tuteur d'exercice élève.

Règle d'or (miroir de :mod:`app.public.schemas`) : le corrigé du professeur
n'apparaît QUE dans ``revealed_answer`` / ``expected_answer``, et uniquement
après une révélation décidée côté serveur — jamais dans un tour non révélé.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Même plafond qu'un message de l'assistant de cours.
MAX_CONTENT_CHARS = 8_000


class SubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["answer", "message"]
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)


class SubmissionRead(BaseModel):
    id: uuid.UUID
    kind: str
    content: str
    feedback: str | None
    verdict: str | None
    effort: str | None
    revealed: bool
    created_at: datetime


class QuestionThreadRead(BaseModel):
    turns: list[SubmissionRead]
    # Corrigé courant du professeur, servi ssi un tour du fil l'a révélé.
    revealed_answer: str | None


class SubmissionsRead(BaseModel):
    # Clé : id de question (chaîne) — les questions sans tour sont absentes.
    questions: dict[str, QuestionThreadRead]


class DeletedRead(BaseModel):
    """Nombre de tours effacés (élève : les siens ; professeur : ceux de tous)."""

    deleted: int


class SubmissionSummaryRead(BaseModel):
    """Vue professeur : tentatives des élèves sur un exercice, par question
    (clé = id de question, chaîne — les questions supprimées du bloc peuvent
    encore y figurer tant que leurs tentatives ne sont pas effacées)."""

    total: int
    by_question: dict[str, int]
