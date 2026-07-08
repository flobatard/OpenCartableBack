"""Schémas des cours et de la structure de leurs blocs.

L'édition du contenu des blocs (éditeurs dédiés par type) est un scope
ultérieur : ``BlockCreate`` ne porte que le ``type`` (et les métadonnées
``titre``/``description``, communes à tous les types), le service pose un
``content`` par défaut conforme au contrat de :mod:`app.models.block`.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    titre: str | None
    description: str | None
    content: dict[str, Any]
    resource_id: uuid.UUID | None


class CourseDetailRead(CourseRead):
    blocks: list[BlockRead]


class BlockCreate(BaseModel):
    # Littéraux = TYPE_TEXTE / TYPE_EXERCICE / TYPE_LIEN de app/models/block.py.
    # « ressource » est volontairement absent : ce type exige un resource_id
    # (CHECK ck_blocks_ressource_coherence) et l'upload S3 n'existe pas encore.
    type: Literal["texte", "exercice", "lien"]
    # Métadonnées facultatives, communes à tous les types (cf. app/models/block.py).
    titre: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=500)


class TexteContent(BaseModel):
    # Contrat de app/models/block.py : markdown simple, jamais de HTML brut ;
    # formules LaTeX admises dans la chaîne ($…$ en ligne, $$…$$ centrée).
    # Pas de trim ni min_length : le blanc est signifiant en markdown et
    # vider un bloc est légitime.
    model_config = ConfigDict(extra="forbid")

    markdown: str = Field(max_length=100_000)


class BlockUpdate(BaseModel):
    """Édition partielle d'un bloc.

    ``titre``/``description`` s'appliquent à tous les types de bloc ;
    ``content`` reste réservé aux blocs texte (cf. ``update_block`` du
    service). Seuls les champs effectivement fournis sont modifiés — un
    ``titre``/``description`` explicitement à ``null`` l'efface, un champ
    absent le laisse inchangé (``model_fields_set``). Enveloppe extensible :
    quand les éditeurs exercice/lien arriveront, `content` deviendra une
    union de formes disjointes (extra="forbid").
    """

    model_config = ConfigDict(extra="forbid")

    titre: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=500)
    content: TexteContent | None = None

    @model_validator(mode="after")
    def _au_moins_un_champ(self) -> "BlockUpdate":
        if not self.model_fields_set:
            raise ValueError("Fournir au moins un champ à modifier")
        return self


class BlockOrderUpdate(BaseModel):
    block_ids: list[uuid.UUID]

    @model_validator(mode="after")
    def _sans_doublons(self) -> "BlockOrderUpdate":
        if len(set(self.block_ids)) != len(self.block_ids):
            raise ValueError("block_ids contient des doublons")
        return self
