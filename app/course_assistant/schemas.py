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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class ConversationUpdate(BaseModel):
    """Renommage — seul champ éditable d'une conversation."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


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
    created_at: datetime


class ConversationDetailRead(ConversationRead):
    messages: list[MessageRead]
