"""Schémas de l'upload de ressources (flow presigned S3).

Deux temps : ``ResourceCreate`` déclare le fichier et obtient une URL présignée
d'upload (``ResourcePresign``) ; une fois l'objet poussé sur S3, ``ResourceConfirm``
confirme l'upload et matérialise le bloc ``ressource`` du cours (retour
``BlockRead`` de :mod:`app.courses.schemas`).
"""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings

# Types de ressource ouverts au MVP (``module`` = sandbox HTML/JS, jalon J4).
ResourceType = Literal["document", "image", "audio", "video"]


class ResourceCreate(BaseModel):
    """Déclaration d'un fichier à uploader (demande d'URL présignée)."""

    model_config = ConfigDict(extra="forbid")

    nom_original: str = Field(min_length=1, max_length=255)
    mime: str = Field(min_length=1, max_length=255)
    taille: int = Field(ge=0)
    type: ResourceType

    @field_validator("nom_original")
    @classmethod
    def _nom_non_blanc(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le nom de fichier ne peut pas être vide")
        return v

    @field_validator("taille")
    @classmethod
    def _taille_sous_plafond(cls, v: int) -> int:
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
    statut: str
    expires_in: int


class ResourceConfirm(BaseModel):
    """Confirmation d'upload : métadonnées d'en-tête du futur bloc ``ressource``.

    Tous les champs sont optionnels — le bloc peut naître sans en-tête.
    ``affichage`` suit le contrat ``content`` de :mod:`app.models.block`.
    """

    model_config = ConfigDict(extra="forbid")

    titre: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=500)
    legende: str | None = Field(default=None, max_length=500)
    affichage: Literal["inline", "telechargement"] = "inline"


class ResourceDownload(BaseModel):
    """URL présignée de lecture/téléchargement (TTL court)."""

    download_url: str
    expires_in: int
