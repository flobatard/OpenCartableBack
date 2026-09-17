"""Schémas de l'assistant de cours (conversations + messages).

Règle d'or (motif ``app/public/schemas.py``) : jamais de ``owner_id`` ni de
donnée interne dans les réponses. ``ConversationCreate.context`` est un
``Literal`` des quatre contextes (``course``, ``block_text``,
``block_exercise``, ``module``) ; la résolution d'exercice élève n'est pas
une conversation (:mod:`app.models.exercise_submission`). La cohérence contexte ↔
cible est validée deux fois : ici (422 Pydantic) et par le CHECK
``ck_ai_conversations_target``.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.course_assistant.questions import (
    ASK_MAX_OPTIONS,
    ASK_MAX_QUESTIONS,
    MAX_QUESTION_OTHER_CHARS,
)
from app.models.ai_conversation import (
    CONTEXT_BLOCK_EXERCISE,
    CONTEXT_BLOCK_TEXT,
    CONTEXT_COURSE,
    CONTEXT_MODULE,
)

# Garde-fou de taille d'un message utilisateur (422 Pydantic au-delà).
MAX_MESSAGE_CHARS = 8_000
# Commentaire d'une décision HITL (relayé au modèle dans le résultat du tool).
MAX_PROPOSAL_COMMENT_CHARS = 2_000

# Contextes d'édition d'un bloc : ``block_id`` requis (miroir du CHECK) ; le
# contexte ``module`` exige ``module_id``, le contexte ``course`` ni l'un ni
# l'autre.
_BLOCK_CONTEXTS = frozenset({CONTEXT_BLOCK_TEXT, CONTEXT_BLOCK_EXERCISE})


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: Literal["course", "block_text", "block_exercise", "module"] = CONTEXT_COURSE
    block_id: uuid.UUID | None = None
    module_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check_target(self) -> "ConversationCreate":
        if self.context in _BLOCK_CONTEXTS:
            if self.block_id is None:
                raise ValueError(f"block_id est requis pour le contexte « {self.context} »")
            if self.module_id is not None:
                raise ValueError(
                    f"module_id ne s'applique pas au contexte « {self.context} »"
                )
        elif self.context == CONTEXT_MODULE:
            if self.module_id is None:
                raise ValueError("module_id est requis pour le contexte « module »")
            if self.block_id is not None:
                raise ValueError("block_id ne s'applique pas au contexte « module »")
        elif self.block_id is not None or self.module_id is not None:
            raise ValueError(
                "block_id et module_id ne s'appliquent pas au contexte « course »"
            )
        return self


class ProposalDecisionCreate(BaseModel):
    """Décision du professeur sur une proposition d'édition en attente
    (flux HITL bloquant — cf. ``hitl.py``) : acceptée/rejetée + commentaire
    optionnel relayé au modèle dans le résultat du tool."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    comment: str | None = Field(default=None, max_length=MAX_PROPOSAL_COMMENT_CHARS)


class QuestionAnswerItem(BaseModel):
    """Réponse à UNE question de l'assistant : index des suggestions choisies
    (ordre des args de l'appel) et/ou réponse libre « Autre » — espaces
    réduits, vide ramené à ``None``. La cohérence avec les questions posées
    (nombre, bornes, choix unique) est contrôlée par la route, contre la forme
    retenue au registre (``questions.answers_error``)."""

    model_config = ConfigDict(extra="forbid")

    selected: list[Annotated[int, Field(ge=0, strict=True)]] = Field(
        default_factory=list, max_length=ASK_MAX_OPTIONS
    )
    other: str | None = Field(default=None, max_length=MAX_QUESTION_OTHER_CHARS)

    @field_validator("other", mode="before")
    @classmethod
    def _normalize_other(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = " ".join(value.split()) or None
        return value


class QuestionAnswerCreate(BaseModel):
    """Réponse du professeur aux questions de l'assistant en attente (flux
    HITL bloquant — cf. ``hitl.py``) : une réponse par question, ou le refus
    de répondre (``declined``, sans réponses)."""

    model_config = ConfigDict(extra="forbid")

    declined: bool = False
    answers: list[QuestionAnswerItem] | None = Field(default=None, max_length=ASK_MAX_QUESTIONS)

    @model_validator(mode="after")
    def _check_consistency(self) -> "QuestionAnswerCreate":
        if self.declined and self.answers:
            raise ValueError("Un refus de répondre ne porte aucune réponse")
        if not self.declined and not self.answers:
            raise ValueError("Réponses manquantes")
        return self


class ConversationUpdate(BaseModel):
    """Renommage — seul champ éditable d'une conversation."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)


class MessageCreate(BaseModel):
    """Message du professeur. ``allow_edit`` — édition globale, opt-in du
    tour, préférence du navigateur — n'est honoré qu'en contexte ``course`` :
    l'assistant global reçoit alors les tools de délégation
    (:mod:`app.course_assistant.delegation`) et le prompt qui les décrit."""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    allow_edit: bool = False


class ConversationRead(BaseModel):
    id: uuid.UUID
    context: str
    block_id: uuid.UUID | None
    module_id: uuid.UUID | None
    title: str | None
    created_at: datetime
    updated_at: datetime


class MessageRead(BaseModel):
    """Un message persisté — les tours ``tool`` sont inclus (lignes d'activité
    repliées côté front)."""

    id: uuid.UUID
    role: str
    position: int
    content: str
    tool_calls: list[dict[str, Any]]
    tool_call_id: str | None
    is_error: bool
    sources: dict[str, Any]
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    created_at: datetime


class ConversationDetailRead(ConversationRead):
    messages: list[MessageRead]
