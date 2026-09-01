"""Schémas de l'assistant de cours (conversations + messages).

Règle d'or (motif ``app/public/schemas.py``) : jamais de ``owner_id`` ni de
donnée interne dans les réponses. ``ConversationCreate.context`` est un
``Literal`` restreint aux contextes **livrés** (``course``, ``block_text``) —
étendre le Literal au fil des lots (bloc exercice, module), le CHECK en base
accepte déjà les quatre. La cohérence contexte ↔ cible est validée deux fois :
ici (422 Pydantic) et par le CHECK ``ck_ai_conversations_target``.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.ai_conversation import CONTEXT_BLOCK_TEXT, CONTEXT_COURSE

# Garde-fou de taille d'un message utilisateur (422 Pydantic au-delà).
MAX_MESSAGE_CHARS = 8_000
# Commentaire d'une décision HITL (relayé au modèle dans le résultat du tool).
MAX_PROPOSAL_COMMENT_CHARS = 2_000


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: Literal["course", "block_text"] = CONTEXT_COURSE
    block_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check_target(self) -> "ConversationCreate":
        if self.context == CONTEXT_BLOCK_TEXT and self.block_id is None:
            raise ValueError("block_id est requis pour le contexte « block_text »")
        if self.context == CONTEXT_COURSE and self.block_id is not None:
            raise ValueError("block_id ne s'applique pas au contexte « course »")
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
