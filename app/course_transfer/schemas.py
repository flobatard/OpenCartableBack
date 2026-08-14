"""Schémas du format d'échange ``.zip`` d'un cours (export/import).

Le zip contient ``manifest.json`` (ce schéma, ``CourseManifest``) et une entrée
binaire ``resources/<uuid>`` par ressource déclarée. Le manifest est la seule
source de vérité de l'import : aucune entrée du zip n'est lue si elle n'y est
pas déclarée. Tout est ``extra="forbid"`` : un manifest inconnu ou d'une
version future échoue en 422 propre plutôt que d'importer à moitié.

Portabilité inter-instances : le classement est porté par les ``code`` des
matières/niveaux (uuid5 déterministes dérivés du code — cf.
``app/subjects/seed_data.py``), remappés par lookup à l'import ; les codes
inconnus de l'instance cible sont ignorés silencieusement.

Les ``id`` exportés (ressources, modules) ne servent qu'à relier les entrées
du manifest entre elles et aux entrées binaires du zip : l'import régénère
tous les uuid (sauf les ``questions[].id`` des exercices, stables à vie —
contrat de :mod:`app.models.block`).
"""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings
from app.courses.schemas import DocumentContent, ExerciceContent, TexteContent
from app.models.block import TYPE_DOCUMENT, TYPE_EXERCICE, TYPE_MODULE, TYPE_TEXTE
from app.modules.schemas import MAX_CODE_LENGTH

FORMAT = "opencartable-course"
FORMAT_VERSION = 1

# Limites de comptage d'une archive — garde-fous d'import (Pi), très au-delà
# d'un cours réel.
MAX_BLOCKS = 500
MAX_RESOURCES = 200
MAX_MODULES = 100
# Taille maximale lue de manifest.json (zip-bomb) — un manifest réel aux
# limites ci-dessus reste très en dessous.
MAX_MANIFEST_BYTES = 5 * 1024 * 1024


class ManifestCourse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    titre: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    # Écho brut du JSONB (motif CourseRead) : {} tant que non personnalisé.
    preview_settings: dict[str, Any] = {}
    subject_codes: list[str] = Field(default=[], max_length=100)
    education_level_codes: list[str] = Field(default=[], max_length=100)

    @field_validator("titre")
    @classmethod
    def _titre_non_blanc(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le titre ne peut pas être vide")
        return v


class ManifestResource(BaseModel):
    """Métadonnées d'une ressource ; son binaire vit dans ``resources/<id>``.

    Seules les ressources ``disponible`` sont exportées ; le ``statut`` et la
    ``s3_key`` ne voyagent pas (recalculés à l'import).
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    type: Literal["document", "image", "audio", "video"]
    nom_original: str = Field(min_length=1, max_length=255)
    taille: int = Field(ge=1)
    mime: str = Field(min_length=1, max_length=255)

    @field_validator("taille")
    @classmethod
    def _taille_sous_plafond(cls, v: int) -> int:
        if v > settings.S3_MAX_UPLOAD_BYTES:
            raise ValueError(
                f"taille au-dessus du plafond ({settings.S3_MAX_UPLOAD_BYTES} octets)"
            )
        return v


class ManifestModule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    titre: str = Field(min_length=1, max_length=255)
    html: str = Field(max_length=MAX_CODE_LENGTH)
    css: str = Field(max_length=MAX_CODE_LENGTH)
    js: str = Field(max_length=MAX_CODE_LENGTH)

    @field_validator("titre")
    @classmethod
    def _titre_non_blanc(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le titre ne peut pas être vide")
        return v


# Forme de content par type de bloc (contrat de app/models/block.py) — les
# blocs « module » ont un content {} strict, validé à part.
_CONTENT_PAR_TYPE: dict[str, type[BaseModel]] = {
    TYPE_TEXTE: TexteContent,
    TYPE_EXERCICE: ExerciceContent,
    TYPE_DOCUMENT: DocumentContent,
}


class ManifestBlock(BaseModel):
    """Bloc exporté ; ``resource_ref``/``module_ref`` = colonnes remappées.

    Le ``content`` est validé selon le ``type`` (formes de
    :mod:`app.courses.schemas`) puis **normalisé** : redump JSON du modèle
    validé — défauts posés, ids de questions en ``str`` — pour que l'import
    insère un JSONB conforme au contrat de :mod:`app.models.block` quel que
    soit le manifest d'origine. Une question sans ``id`` (manifest écrit à la
    main) en recevra un frais côté service.
    """

    model_config = ConfigDict(extra="forbid")

    position: int = Field(ge=0)
    type: Literal["texte", "exercice", "document", "module"]
    titre: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=500)
    content: dict[str, Any] = {}
    resource_ref: uuid.UUID | None = None
    module_ref: uuid.UUID | None = None

    @model_validator(mode="after")
    def _coherence(self) -> "ManifestBlock":
        # Miroir des CHECKs ck_blocks_document_coherence / ck_blocks_module_coherence.
        if self.resource_ref is not None and self.type != TYPE_DOCUMENT:
            raise ValueError("resource_ref ne s'applique qu'aux blocs « document »")
        if self.module_ref is not None and self.type != TYPE_MODULE:
            raise ValueError("module_ref ne s'applique qu'aux blocs « module »")
        if self.type == TYPE_MODULE:
            if self.content != {}:
                raise ValueError("le content d'un bloc « module » doit être {}")
            return self
        forme = _CONTENT_PAR_TYPE[self.type]
        self.content = forme.model_validate(self.content).model_dump(mode="json")
        return self


class CourseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Littéraux stricts : un format/une version inconnus = 422 propre.
    format: Literal["opencartable-course"]
    format_version: Literal[1]
    exported_at: datetime
    course: ManifestCourse
    blocks: list[ManifestBlock] = Field(default=[], max_length=MAX_BLOCKS)
    resources: list[ManifestResource] = Field(default=[], max_length=MAX_RESOURCES)
    modules: list[ManifestModule] = Field(default=[], max_length=MAX_MODULES)

    @model_validator(mode="after")
    def _refs_coherentes(self) -> "CourseManifest":
        resource_ids = {r.id for r in self.resources}
        if len(resource_ids) != len(self.resources):
            raise ValueError("resources contient des ids dupliqués")
        module_ids = {m.id for m in self.modules}
        if len(module_ids) != len(self.modules):
            raise ValueError("modules contient des ids dupliqués")
        for block in self.blocks:
            if block.resource_ref is not None and block.resource_ref not in resource_ids:
                raise ValueError(f"resource_ref inconnue du manifest : {block.resource_ref}")
            if block.module_ref is not None and block.module_ref not in module_ids:
                raise ValueError(f"module_ref inconnue du manifest : {block.module_ref}")
        return self
