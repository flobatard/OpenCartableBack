import uuid

from pydantic import BaseModel, Field, model_validator


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
    onboarding_complete: bool
    enseignement: ProfilContexte | None
    apprentissage: ProfilContexte | None


class ProfileUpdate(BaseModel):
    est_prof: bool
    est_eleve: bool
    systeme_scolaire: str = Field(min_length=1, max_length=20)
    enseignement: ProfilContexte | None = None
    apprentissage: ProfilContexte | None = None

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
