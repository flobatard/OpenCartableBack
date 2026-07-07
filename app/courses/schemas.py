"""Schémas des cours et de la structure de leurs blocs.

L'édition du contenu des blocs (éditeurs dédiés par type) est un scope
ultérieur : ``BlockCreate`` ne porte que le ``type``, le service pose un
``content`` par défaut conforme au contrat de :mod:`app.models.block`.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class CourseCreate(BaseModel):
    titre: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    subject_ids: list[uuid.UUID] = []
    education_level_ids: list[uuid.UUID] = []

    @field_validator("titre")
    @classmethod
    def _titre_non_blanc(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le titre ne peut pas être vide")
        return v


class CourseRead(BaseModel):
    # Pas d'owner_id : les routes ne servent que les cours de l'appelant.
    id: uuid.UUID
    titre: str
    description: str | None
    subject_ids: list[uuid.UUID]
    education_level_ids: list[uuid.UUID]
    block_count: int
    created_at: datetime
    updated_at: datetime


class BlockRead(BaseModel):
    id: uuid.UUID
    position: int
    type: str
    content: dict[str, Any]
    resource_id: uuid.UUID | None


class CourseDetailRead(CourseRead):
    blocks: list[BlockRead]


class BlockCreate(BaseModel):
    # Littéraux = TYPE_TEXTE / TYPE_EXERCICE / TYPE_LIEN de app/models/block.py.
    # « ressource » est volontairement absent : ce type exige un resource_id
    # (CHECK ck_blocks_ressource_coherence) et l'upload S3 n'existe pas encore.
    type: Literal["texte", "exercice", "lien"]


class BlockOrderUpdate(BaseModel):
    block_ids: list[uuid.UUID]

    @model_validator(mode="after")
    def _sans_doublons(self) -> "BlockOrderUpdate":
        if len(set(self.block_ids)) != len(self.block_ids):
            raise ValueError("block_ids contient des doublons")
        return self
