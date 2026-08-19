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


class ProfilContexte(BaseModel):
    """Sélections d'un contexte (« enseigne » ou « apprend »)."""

    education_level_ids: list[uuid.UUID]
    subject_ids: list[uuid.UUID]


class UserProfileRead(BaseModel):
    id: uuid.UUID
    sub: str
    email: str | None
    est_prof: bool
    est_eleve: bool
    systeme_scolaire: str | None
    # Nom d'affichage des pages publiques (catalogue, J2) ; None = anonyme.
    nom_public: str | None
    # Opt-in à la recherche publique de profs (J3). Le flag seul ne suffit
    # pas à remonter (voir app/search/service.py).
    cherchable: bool
    # URL présignée inline de l'avatar (TTL court, re-mintée à chaque lecture) ;
    # None si pas d'avatar ou upload non confirmé. Jamais la clé S3.
    avatar_url: str | None
    onboarding_complete: bool
    enseignement: ProfilContexte | None
    apprentissage: ProfilContexte | None


class ProfileUpdate(BaseModel):
    est_prof: bool
    est_eleve: bool
    systeme_scolaire: str = Field(min_length=1, max_length=20)
    # Optionnel : seule donnée d'identité montrée sur les pages publiques
    # (jamais l'email). Blanc = None (catalogue anonyme).
    nom_public: str | None = Field(default=None, max_length=100)
    # Opt-in recherche publique (J3). Défaut False : le PUT est un
    # remplacement complet — un payload sans le champ « décoche ».
    # Toléré sans nom_public : la règle de visibilité (cherchable AND
    # nom_public AND ≥1 cours public) vit dans le service de recherche.
    cherchable: bool = False
    enseignement: ProfilContexte | None = None
    apprentissage: ProfilContexte | None = None

    @field_validator("nom_public")
    @classmethod
    def _nom_public_blanc_devient_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @model_validator(mode="after")
    def _roles_et_blocs_coherents(self) -> "ProfileUpdate":
        if not (self.est_prof or self.est_eleve):
            raise ValueError("Au moins un rôle (est_prof ou est_eleve) est requis")
        for role, bloc, nom in (
            (self.est_prof, self.enseignement, "enseignement"),
            (self.est_eleve, self.apprentissage, "apprentissage"),
        ):
            if role and bloc is None:
                raise ValueError(f"Le bloc '{nom}' est requis pour ce rôle")
            if not role and bloc is not None:
                raise ValueError(f"Le bloc '{nom}' est fourni sans le rôle correspondant")
            if bloc is not None and (
                not bloc.education_level_ids or not bloc.subject_ids
            ):
                raise ValueError(
                    f"Le bloc '{nom}' doit contenir au moins un niveau et une matière"
                )
        return self


class AvatarCreate(BaseModel):
    """Déclaration d'upload d'avatar (motif ResourceCreate, réduit).

    Le front envoie toujours un carré recadré côté navigateur (JPEG) ; la
    whitelist reste à trois formats pour les clients hors navigateur.
    """

    model_config = ConfigDict(extra="forbid")

    mime: Literal["image/jpeg", "image/png", "image/webp"]
    taille: int = Field(ge=1)

    @field_validator("taille")
    @classmethod
    def _taille_sous_plafond(cls, v: int) -> int:
        if v > settings.AVATAR_MAX_BYTES:
            raise ValueError(
                f"Image trop volumineuse (max {settings.AVATAR_MAX_BYTES} octets)"
            )
        return v


class AvatarPresign(BaseModel):
    """URL présignée d'upload de l'avatar (motif ResourcePresign, sans s3_key)."""

    upload_url: str
    expires_in: int
