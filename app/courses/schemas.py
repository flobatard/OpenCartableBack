"""Schémas des cours et de la structure de leurs blocs.

``BlockCreate`` ne porte que le ``type`` (et les métadonnées
``title``/``description``, communes à tous les types), le service pose un
``content`` par défaut conforme au contrat de :mod:`app.models.block`.
Le contenu s'édite ensuite via ``BlockUpdate.content`` (une forme par type
de bloc : ``TextContent``, ``ExerciseContent``, ``DocumentContent`` ; le
content d'un bloc ``module`` n'est pas éditable, son contenu étant le
module pointé) ; le lien d'un bloc ``document`` vers sa ressource s'édite
via ``BlockUpdate.resource_id``, celui d'un bloc ``module`` via ``module_id``.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel


class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    subject_ids: list[uuid.UUID] = []
    education_level_ids: list[uuid.UUID] = []

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le titre ne peut pas être vide")
        return v


class CourseRead(BaseModel):
    # Pas d'owner_id : les routes ne servent que les cours de l'appelant.
    id: uuid.UUID
    title: str
    description: str | None
    subject_ids: list[uuid.UUID]
    education_level_ids: list[uuid.UUID]
    block_count: int
    # Écho brut du JSONB stocké (comme BlockRead.content) : {} tant que non
    # personnalisé — le front y applique alors ses défauts.
    preview_settings: dict[str, Any]
    # Régime d'accès élève : public | private | draft.
    visibility: str
    created_at: datetime
    updated_at: datetime


class BlockRead(BaseModel):
    id: uuid.UUID
    position: int
    type: str
    title: str | None
    description: str | None
    content: dict[str, Any]
    resource_id: uuid.UUID | None
    module_id: uuid.UUID | None


class CourseDetailRead(CourseRead):
    blocks: list[BlockRead]


class CourseUpdate(BaseModel):
    """Édition partielle d'un cours : titre, description, classement.

    Seuls les champs effectivement fournis sont modifiés
    (``model_fields_set``, comme ``BlockUpdate``) : une ``description``
    explicitement à ``null`` l'efface, un champ absent la laisse inchangée. Le
    titre, lui, ne s'efface jamais (colonne NOT NULL) — ``null`` explicite ou
    titre blanc → 422. ``subject_ids``/``education_level_ids`` ont une
    sémantique de **remplacement** (comme les questions d'un exercice) : la
    liste fournie devient le classement du cours, ``[]`` le vide, ``null`` est
    refusé (c'est ``[]`` qui efface). Un id inconnu → 422, comme à la création.
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    subject_ids: list[uuid.UUID] | None = None
    education_level_ids: list[uuid.UUID] | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None  # rejeté par _non_nullable_fields (titre non effaçable)
        v = v.strip()
        if not v:
            raise ValueError("Le titre ne peut pas être vide")
        return v

    @model_validator(mode="after")
    def _non_nullable_fields(self) -> "CourseUpdate":
        fields = self.model_fields_set
        if not fields:
            raise ValueError("Fournir au moins un champ à modifier")
        if "title" in fields and self.title is None:
            raise ValueError("Le titre ne peut pas être vide")
        if "subject_ids" in fields and self.subject_ids is None:
            raise ValueError("subject_ids attend une liste (vide pour tout retirer)")
        if "education_level_ids" in fields and self.education_level_ids is None:
            raise ValueError("education_level_ids attend une liste (vide pour tout retirer)")
        return self


class CourseMetaRead(BaseModel):
    """Écho des champs éditoriaux d'un cours après un ``PATCH /courses/{id}``.

    Volontairement plus étroit que ``CourseRead`` : le nombre de blocs et les
    réglages n'ont pas bougé, les servir coûterait des requêtes de plus. Le
    front y patche ses signaux ``list``/``detail`` (``updated_at`` inclus : le
    cours vient de remonter en tête de liste). ``subject_ids`` et
    ``education_level_ids`` valent ``null`` quand le PATCH ne les portait pas
    (classement inchangé, non relu en base) — jamais ``[]``, qui signifie
    « classement vidé ».
    """

    id: uuid.UUID
    title: str
    description: str | None
    subject_ids: list[uuid.UUID] | None
    education_level_ids: list[uuid.UUID] | None
    updated_at: datetime


class PreviewSettings(BaseModel):
    """Réglages d'affichage de la preview d'un cours (typographie / mise en page).

    Contrat = interface ``CourseStyleSettings`` du front : clés camelCase (via
    ``alias_generator``), tous les champs requis (le front envoie l'objet
    complet). Remplacement complet via ``PUT /courses/{id}/preview``. Les bornes
    sont des garde-fous contre les valeurs absurdes, ajustables sans migration
    (colonne JSONB).
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    font_size_px: float = Field(ge=8, le=48)  # -> fontSizePx (facteur px/16)
    heading_scale: float = Field(ge=0.5, le=3)  # -> headingScale (1 = historique)
    line_height: float = Field(ge=1, le=3)  # -> lineHeight (facteur valeur/1.7)
    width_ch: float = Field(ge=20, le=200)  # -> widthCh (colonne de lecture)
    paragraph_gap_em: float = Field(ge=0, le=10)  # -> paragraphGapEm (valeur/1.5)
    font: Literal["sans", "serif"]


class VisibilityUpdate(BaseModel):
    """Changement du régime d'accès élève d'un cours.

    Littéraux = constantes VISIBILITY_* de app/models/course.py. Passer un
    cours en ``draft`` suspend ses liens de partage sans les supprimer
    (la vérification se fait à chaque accès public).
    """

    visibility: Literal["public", "private", "draft"]


class BlockCreate(BaseModel):
    # Littéraux = constantes TYPE_* de app/models/block.py. Un bloc
    # « document » naît vide (resource_id NULL) et se remplit dans
    # l'éditeur via BlockUpdate.resource_id.
    type: Literal["text", "exercise", "document", "module"]
    # Métadonnées facultatives, communes à tous les types (cf. app/models/block.py).
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=500)


class TextContent(BaseModel):
    # Contrat de app/models/block.py : markdown simple, jamais de HTML brut ;
    # formules LaTeX admises dans la chaîne ($…$ en ligne, $$…$$ centrée).
    # Pas de trim ni min_length : le blanc est signifiant en markdown et
    # vider un bloc est légitime.
    model_config = ConfigDict(extra="forbid")

    markdown: str = Field(max_length=100_000)


class ExerciseQuestion(BaseModel):
    """Question à réponse libre d'un bloc exercice.

    ``id`` absent/``None`` = nouvelle question (uuid4 généré par le
    service) ; un id fourni doit déjà exister dans le bloc édité (ids
    stables à vie, cf. :mod:`app.models.block`) — 422 côté service sinon.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None
    statement: str = Field(max_length=20_000)
    type: Literal["free_text"] = "free_text"
    # Corrigé du prof, texte simple (pas de markdown) — jamais servi aux
    # élèves (le régime public reconstruit le content sans lui).
    expected_answer: str = Field(default="", max_length=20_000)


class ExerciseContent(BaseModel):
    # Contrat de app/models/block.py : sujet markdown + questions ordonnées.
    # Sémantique REMPLACEMENT : une question absente du payload est
    # supprimée. ``questions`` requis sans défaut, exprès : la forme reste
    # disjointe de TextContent (smart union sans discriminant) et un payload
    # partiel ne peut pas effacer les questions en validant à moitié.
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(max_length=100_000)
    questions: list[ExerciseQuestion] = Field(max_length=50)

    @model_validator(mode="after")
    def _ids_without_duplicates(self) -> "ExerciseContent":
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

    caption: str | None = Field(default=None, max_length=500)
    display: Literal["inline", "download"] = "inline"


class BlockUpdate(BaseModel):
    """Édition partielle d'un bloc.

    ``title``/``description`` s'appliquent à tous les types de bloc ;
    ``content`` est une union de formes disjointes (``extra="forbid"`` des
    deux côtés) — le service vérifie que la forme reçue correspond au type
    du bloc. ``resource_id`` ne s'applique qu'aux blocs ``document`` :
    ``null`` explicite détache la ressource, un uuid pointe une ressource
    du même cours au statut ``available`` (validé côté service).
    ``module_id`` est son miroir pour les blocs ``module`` : ``null``
    détache, un uuid pointe un module du même cours (validé côté service —
    pas de statut à vérifier, le code vit en base). Seuls
    les champs effectivement fournis sont modifiés — un ``title``/
    ``description``/``resource_id``/``module_id`` explicitement à ``null``
    l'efface, un champ absent le laisse inchangé (``model_fields_set``).
    """

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=500)
    # DocumentContent en dernier : forme la plus permissive de l'union.
    content: TextContent | ExerciseContent | DocumentContent | None = None
    resource_id: uuid.UUID | None = None
    module_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "BlockUpdate":
        if not self.model_fields_set:
            raise ValueError("Fournir au moins un champ à modifier")
        return self


class BlockOrderUpdate(BaseModel):
    block_ids: list[uuid.UUID]

    @model_validator(mode="after")
    def _without_duplicates(self) -> "BlockOrderUpdate":
        if len(set(self.block_ids)) != len(self.block_ids):
            raise ValueError("block_ids contient des doublons")
        return self
