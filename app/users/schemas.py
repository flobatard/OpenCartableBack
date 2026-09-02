import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings

# Whitelist fermée des formats d'avatar : elle donne aussi l'extension de la
# clé S3 (le nom de fichier de l'utilisateur n'est jamais persisté — rien à
# sanitizer, l'avatar est servi en inline sous un nom constant).
AVATAR_EXTENSIONS: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


class ProfileContext(BaseModel):
    """Sélections d'un contexte (« teaching » ou « learning »)."""

    education_level_ids: list[uuid.UUID]
    subject_ids: list[uuid.UUID]


class UserProfileRead(BaseModel):
    id: uuid.UUID
    sub: str
    email: str | None
    is_teacher: bool
    is_student: bool
    school_system: str | None
    # Nom d'affichage des pages publiques (catalogue, J2) ; None = anonyme.
    public_name: str | None
    # Opt-in à la recherche publique de profs (J3). Le flag seul ne suffit
    # pas à remonter (voir app/search/service.py).
    searchable: bool
    # URL présignée inline de l'avatar (TTL court, re-mintée à chaque lecture) ;
    # None si pas d'avatar ou upload non confirmé. Jamais la clé S3.
    avatar_url: str | None
    onboarding_complete: bool
    teaching: ProfileContext | None
    learning: ProfileContext | None


class ProfileUpdate(BaseModel):
    is_teacher: bool
    is_student: bool
    school_system: str = Field(min_length=1, max_length=20)
    # Optionnel : seule donnée d'identité montrée sur les pages publiques
    # (jamais l'email). Blanc = None (catalogue anonyme).
    public_name: str | None = Field(default=None, max_length=100)
    # Opt-in recherche publique (J3). Défaut False : le PUT est un
    # remplacement complet — un payload sans le champ « décoche ».
    # Toléré sans public_name : la règle de visibilité (searchable AND
    # public_name AND ≥1 cours public) vit dans le service de recherche.
    searchable: bool = False
    teaching: ProfileContext | None = None
    learning: ProfileContext | None = None

    @field_validator("public_name")
    @classmethod
    def _blank_public_name_becomes_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @model_validator(mode="after")
    def _roles_and_blocks_consistent(self) -> "ProfileUpdate":
        if not (self.is_teacher or self.is_student):
            raise ValueError("Au moins un rôle (is_teacher ou is_student) est requis")
        for role, block, name in (
            (self.is_teacher, self.teaching, "teaching"),
            (self.is_student, self.learning, "learning"),
        ):
            if role and block is None:
                raise ValueError(f"Le bloc '{name}' est requis pour ce rôle")
            if not role and block is not None:
                raise ValueError(f"Le bloc '{name}' est fourni sans le rôle correspondant")
            if block is not None and (
                not block.education_level_ids or not block.subject_ids
            ):
                raise ValueError(
                    f"Le bloc '{name}' doit contenir au moins un niveau et une matière"
                )
        return self


class AvatarCreate(BaseModel):
    """Déclaration d'upload d'avatar (motif ResourceCreate, réduit).

    Le front envoie un carré recadré côté navigateur, en **WebP** (seul
    format de la whitelist qui préserve la transparence à poids raisonnable)
    — ou en PNG si le navigateur n'encode pas le WebP, ``canvas.toBlob`` y
    retombant silencieusement. Le JPEG reste accepté pour les clients hors
    navigateur.
    """

    model_config = ConfigDict(extra="forbid")

    mime: Literal["image/jpeg", "image/png", "image/webp"]
    size: int = Field(ge=1)

    @field_validator("size")
    @classmethod
    def _size_under_cap(cls, v: int) -> int:
        if v > settings.AVATAR_MAX_BYTES:
            raise ValueError(
                f"Image trop volumineuse (max {settings.AVATAR_MAX_BYTES} octets)"
            )
        return v


class AvatarPresign(BaseModel):
    """URL présignée d'upload de l'avatar (motif ResourcePresign, sans s3_key)."""

    upload_url: str
    expires_in: int
