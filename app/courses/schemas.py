"""Schémas des cours et de la structure de leurs blocs.

``BlockCreate`` ne porte que le ``type`` (et les métadonnées
``titre``/``description``, communes à tous les types), le service pose un
``content`` par défaut conforme au contrat de :mod:`app.models.block`.
Le contenu s'édite ensuite via ``BlockUpdate.content`` (une forme par type
de bloc : ``TexteContent``, ``ExerciceContent``, ``DocumentContent`` ;
``module`` n'a pas de forme éditable avant le J4) ; le lien d'un bloc
``document`` vers sa ressource s'édite via ``BlockUpdate.resource_id``.
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
    # Littéraux = constantes TYPE_* de app/models/block.py. Un bloc
    # « document » naît vide (resource_id NULL) et se remplit dans
    # l'éditeur via BlockUpdate.resource_id.
    type: Literal["texte", "exercice", "document", "module"]
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


class ExerciceQuestion(BaseModel):
    """Question à réponse libre d'un bloc exercice.

    ``id`` absent/``None`` = nouvelle question (uuid4 généré par le
    service) ; un id fourni doit déjà exister dans le bloc édité (ids
    stables à vie, cf. :mod:`app.models.block`) — 422 côté service sinon.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    enonce: str = Field(max_length=20_000)
    type: Literal["texte_libre"] = "texte_libre"
    # Corrigé du prof, texte simple (pas de markdown) — jamais servi aux
    # élèves (le J2 filtrera le content des liens publics).
    reponse_attendue: str = Field(default="", max_length=20_000)


class ExerciceContent(BaseModel):
    # Contrat de app/models/block.py : sujet markdown + questions ordonnées.
    # Sémantique REMPLACEMENT : une question absente du payload est
    # supprimée. ``questions`` requis sans défaut, exprès : la forme reste
    # disjointe de TexteContent (smart union sans discriminant) et un payload
    # partiel ne peut pas effacer les questions en validant à moitié.
    model_config = ConfigDict(extra="forbid")

    enonce: str = Field(max_length=100_000)
    questions: list[ExerciceQuestion] = Field(max_length=50)

    @model_validator(mode="after")
    def _ids_sans_doublons(self) -> "ExerciceContent":
        ids = [q.id for q in self.questions if q.id is not None]
        if len(set(ids)) != len(ids):
            raise ValueError("questions contient des ids dupliqués")
        return self


class DocumentContent(BaseModel):
    # Contrat de app/models/block.py : éditorial d'affichage du bloc
    # document — la ressource pointée reste en colonne (resource_id),
    # jamais dans le content. Tous les champs ont un défaut, donc ``{}``
    # valide en DocumentContent : sans conséquence, le garde-fou
    # forme↔type du service rejette la forme sur un bloc d'un autre type,
    # et les formes restent disjointes (extra="forbid" partout).
    model_config = ConfigDict(extra="forbid")

    legende: str | None = Field(default=None, max_length=500)
    affichage: Literal["inline", "telechargement"] = "inline"


class BlockUpdate(BaseModel):
    """Édition partielle d'un bloc.

    ``titre``/``description`` s'appliquent à tous les types de bloc ;
    ``content`` est une union de formes disjointes (``extra="forbid"`` des
    deux côtés) — le service vérifie que la forme reçue correspond au type
    du bloc. ``resource_id`` ne s'applique qu'aux blocs ``document`` :
    ``null`` explicite détache la ressource, un uuid pointe une ressource
    du même cours au statut ``disponible`` (validé côté service). Seuls
    les champs effectivement fournis sont modifiés — un ``titre``/
    ``description``/``resource_id`` explicitement à ``null`` l'efface, un
    champ absent le laisse inchangé (``model_fields_set``).
    """

    model_config = ConfigDict(extra="forbid")

    titre: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=500)
    # DocumentContent en dernier : forme la plus permissive de l'union.
    content: TexteContent | ExerciceContent | DocumentContent | None = None
    resource_id: uuid.UUID | None = None

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
