"""Schémas de l'assistant de cours (conversations + messages).

Règle d'or (motif ``app/public/schemas.py``) : jamais de ``owner_id`` ni de
donnée interne dans les réponses. ``ConversationCreate.context`` est un
``Literal`` restreint aux contextes **livrés** — étendre le Literal au fil des
lots (bloc texte/exercice, module), le CHECK en base accepte déjà les quatre.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.ai_conversation import CONTEXT_COURSE

# Garde-fou de taille d'un message utilisateur (422 Pydantic au-delà).
MAX_MESSAGE_CHARS = 8_000


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: Literal["course"] = CONTEXT_COURSE


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
