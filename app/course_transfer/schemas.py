"""Schémas du format d'échange ``.zip`` d'un cours (export/import).

Le zip contient ``manifest.json`` (ce schéma, ``CourseManifest``) et une entrée
binaire ``resources/<uuid>`` par ressource déclarée. Le manifest est la seule
source de vérité de l'import : aucune entrée du zip n'est lue si elle n'y est
pas déclarée. Tout est ``extra="forbid"`` : un manifest inconnu ou d'une
version future échoue en 422 propre plutôt que d'importer à moitié.

Versions du format : la v2 (courante) est tout-anglais (``title``,
``original_name``, ``size``, types de bloc ``text``/``exercise``, clés de
content ``statement``/``expected_answer``/``caption``/``display``…). Les
archives v1 (clés/valeurs françaises historiques) restent importables :
``normalize_manifest_v1`` traduit le dict brut AVANT validation Pydantic,
puis tout le pipeline ne voit que du v2.

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
from app.courses.schemas import DocumentContent, ExerciseContent, TextContent
from app.models.block import TYPE_DOCUMENT, TYPE_EXERCISE, TYPE_MODULE, TYPE_TEXT
from app.modules.schemas import MAX_CODE_LENGTH

FORMAT = "opencartable-course"
FORMAT_VERSION = 2

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

    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    # Écho brut du JSONB (motif CourseRead) : {} tant que non personnalisé.
    preview_settings: dict[str, Any] = {}
    subject_codes: list[str] = Field(default=[], max_length=100)
    education_level_codes: list[str] = Field(default=[], max_length=100)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le titre ne peut pas être vide")
        return v


class ManifestResource(BaseModel):
    """Métadonnées d'une ressource ; son binaire vit dans ``resources/<id>``.

    Seules les ressources ``available`` sont exportées ; le ``status`` et la
    ``s3_key`` ne voyagent pas (recalculés à l'import).
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    type: Literal["document", "image", "audio", "video"]
    original_name: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1)
    mime: str = Field(min_length=1, max_length=255)

    @field_validator("size")
    @classmethod
    def _size_under_cap(cls, v: int) -> int:
        if v > settings.S3_MAX_UPLOAD_BYTES:
            raise ValueError(
                f"size au-dessus du plafond ({settings.S3_MAX_UPLOAD_BYTES} octets)"
            )
        return v


class ManifestModule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    title: str = Field(min_length=1, max_length=255)
    html: str = Field(max_length=MAX_CODE_LENGTH)
    css: str = Field(max_length=MAX_CODE_LENGTH)
    js: str = Field(max_length=MAX_CODE_LENGTH)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le titre ne peut pas être vide")
        return v


# Forme de content par type de bloc (contrat de app/models/block.py) — les
# blocs « module » ont un content {} strict, validé à part.
_CONTENT_BY_TYPE: dict[str, type[BaseModel]] = {
    TYPE_TEXT: TextContent,
    TYPE_EXERCISE: ExerciseContent,
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
    type: Literal["text", "exercise", "document", "module"]
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=500)
    content: dict[str, Any] = {}
    resource_ref: uuid.UUID | None = None
    module_ref: uuid.UUID | None = None

    @model_validator(mode="after")
    def _consistency(self) -> "ManifestBlock":
        # Miroir des CHECKs ck_blocks_document_consistency / ck_blocks_module_consistency.
        if self.resource_ref is not None and self.type != TYPE_DOCUMENT:
            raise ValueError("resource_ref ne s'applique qu'aux blocs « document »")
        if self.module_ref is not None and self.type != TYPE_MODULE:
            raise ValueError("module_ref ne s'applique qu'aux blocs « module »")
        if self.type == TYPE_MODULE:
            if self.content != {}:
                raise ValueError("le content d'un bloc « module » doit être {}")
            return self
        shape = _CONTENT_BY_TYPE[self.type]
        self.content = shape.model_validate(self.content).model_dump(mode="json")
        return self


class CourseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Littéraux stricts : un format/une version inconnus = 422 propre.
    # La v1 est acceptée en amont via normalize_manifest_v1 (dict brut
    # traduit puis validé ici comme du v2).
    format: Literal["opencartable-course"]
    format_version: Literal[2]
    exported_at: datetime
    course: ManifestCourse
    blocks: list[ManifestBlock] = Field(default=[], max_length=MAX_BLOCKS)
    resources: list[ManifestResource] = Field(default=[], max_length=MAX_RESOURCES)
    modules: list[ManifestModule] = Field(default=[], max_length=MAX_MODULES)

    @model_validator(mode="after")
    def _refs_consistent(self) -> "CourseManifest":
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


# --- Compatibilité v1 → v2 -------------------------------------------------
#
# Tables de traduction du manifest v1 (nomenclature française historique).
# ``normalize_manifest_v1`` est une fonction PURE (nouveau dict, jamais de
# mutation de l'entrée) appliquée au JSON brut AVANT la validation Pydantic :
# tout ce qui ne ressemble pas à la forme attendue est laissé tel quel — c'est
# ``CourseManifest`` (extra="forbid") qui rejette ensuite en 422.

_V1_COURSE_KEYS = {"titre": "title"}
_V1_RESOURCE_KEYS = {"nom_original": "original_name", "taille": "size"}
_V1_MODULE_KEYS = {"titre": "title"}
_V1_BLOCK_KEYS = {"titre": "title"}
_V1_BLOCK_TYPES = {"texte": "text", "exercice": "exercise"}
_V1_CONTENT_KEYS = {
    "enonce": "statement",
    "reponse_attendue": "expected_answer",
    "legende": "caption",
    "affichage": "display",
}
_V1_QUESTION_TYPES = {"texte_libre": "free_text"}
_V1_DISPLAY_VALUES = {"telechargement": "download"}


def _rename_keys(value: Any, mapping: dict[str, str]) -> Any:
    """Nouveau dict aux clés traduites ; toute autre valeur passe verbatim."""
    if not isinstance(value, dict):
        return value
    return {mapping.get(k, k): v for k, v in value.items()}


def _normalize_content_v1(content: Any) -> Any:
    """Traduit le ``content`` JSONB d'un bloc v1 (questions comprises)."""
    if not isinstance(content, dict):
        return content
    content = _rename_keys(content, _V1_CONTENT_KEYS)
    if isinstance(content.get("questions"), list):
        questions = []
        for question in content["questions"]:
            if isinstance(question, dict):
                question = _rename_keys(question, _V1_CONTENT_KEYS)
                q_type = question.get("type")
                if q_type in _V1_QUESTION_TYPES:
                    question["type"] = _V1_QUESTION_TYPES[q_type]
            questions.append(question)
        content["questions"] = questions
    display = content.get("display")
    if display in _V1_DISPLAY_VALUES:
        content["display"] = _V1_DISPLAY_VALUES[display]
    return content


def normalize_manifest_v1(raw: dict) -> dict:
    """Traduit un manifest.json v1 (dict brut) en dict v2, prêt à valider.

    Fonction pure et défensive : clés françaises renommées (cours,
    ressources, modules, blocs), types de bloc ``texte``/``exercice``
    traduits, clés et valeurs des ``content`` traduites (questions incluses,
    type ``texte_libre`` → ``free_text``, affichage ``telechargement`` →
    ``download``), ``format_version`` porté à 2. Toute structure inattendue
    est laissée telle quelle — la validation ``CourseManifest`` tranche.
    """
    data = dict(raw)
    data["format_version"] = FORMAT_VERSION
    if "course" in data:
        data["course"] = _rename_keys(data["course"], _V1_COURSE_KEYS)
    if isinstance(data.get("resources"), list):
        data["resources"] = [
            _rename_keys(r, _V1_RESOURCE_KEYS) for r in data["resources"]
        ]
    if isinstance(data.get("modules"), list):
        data["modules"] = [_rename_keys(m, _V1_MODULE_KEYS) for m in data["modules"]]
    if isinstance(data.get("blocks"), list):
        blocks = []
        for block in data["blocks"]:
            if isinstance(block, dict):
                block = _rename_keys(block, _V1_BLOCK_KEYS)
                b_type = block.get("type")
                if b_type in _V1_BLOCK_TYPES:
                    block["type"] = _V1_BLOCK_TYPES[b_type]
                if "content" in block:
                    block["content"] = _normalize_content_v1(block["content"])
            blocks.append(block)
        data["blocks"] = blocks
    return data
