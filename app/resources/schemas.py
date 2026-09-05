"""Schémas de la bibliothèque de ressources d'un cours (CRUD + flow presigned).

Upload en deux temps : ``ResourceCreate`` déclare le fichier et obtient une URL
présignée d'upload (``ResourcePresign``) ; une fois l'objet poussé sur S3, la
confirmation (sans body) vérifie l'objet et passe la ressource à ``available``
(retour ``ResourceRead``). Les ressources sont indépendantes des blocs : un
bloc ``document`` peut les pointer (``BlockUpdate.resource_id``,
:mod:`app.courses.schemas`), jamais l'inverse.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings

# Types de ressource. Les modules interactifs ne sont pas des ressources :
# leur code vit en base (table ``modules``).
ResourceType = Literal["document", "image", "audio", "video"]


class ResourceCreate(BaseModel):
    """Déclaration d'un fichier à uploader (demande d'URL présignée)."""

    model_config = ConfigDict(extra="forbid")

    original_name: str = Field(min_length=1, max_length=255)
    mime: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0)
    type: ResourceType

    @field_validator("original_name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le nom de fichier ne peut pas être vide")
        return v

    @field_validator("size")
    @classmethod
    def _size_under_cap(cls, v: int) -> int:
        if v > settings.S3_MAX_UPLOAD_BYTES:
            raise ValueError(
                f"Fichier trop volumineux (max {settings.S3_MAX_UPLOAD_BYTES} octets)"
            )
        return v


class ResourcePresign(BaseModel):
    """URL présignée d'upload + métadonnées de la ressource créée (en attente)."""

    resource_id: uuid.UUID
    s3_key: str
    upload_url: str
    status: str
    expires_in: int


class ResourceRead(BaseModel):
    """Ressource de la bibliothèque du cours.

    Pas de ``s3_key`` : détail interne de stockage, le front passe par les
    URL présignées (upload/download).
    """

    id: uuid.UUID
    type: str
    original_name: str
    size: int
    mime: str
    status: str
    created_at: datetime
    updated_at: datetime


class ResourceUpdate(BaseModel):
    """Renommage d'une ressource.

    Seul le nom affiché change (et le ``Content-Disposition`` des prochains
    téléchargements) — la clé S3 reste figée, l'objet n'est pas déplacé.
    """

    model_config = ConfigDict(extra="forbid")

    original_name: str = Field(min_length=1, max_length=255)

    @field_validator("original_name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le nom de fichier ne peut pas être vide")
        return v


class ResourceDownload(BaseModel):
    """URL présignée de lecture/téléchargement (TTL court)."""

    download_url: str
    expires_in: int
