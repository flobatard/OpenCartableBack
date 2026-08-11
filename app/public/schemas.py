"""Schémas des routes publiques élèves (J2) — lecture seule, sans identité.

Règle d'or : ne JAMAIS exposer ici une donnée réservée au prof. Concrètement :
- ``PublicQuestionRead`` n'a **pas de champ** ``reponse_attendue`` — le
  filtrage du corrigé est structurel (reconstruction du content exercice
  dans le service), pas un ``exclude`` fragile ;
- pas d'``owner_id``, pas d'email, pas de ``s3_key`` ; la seule donnée
  d'identité publique est ``users.nom_public`` (choisie par le prof) ;
- les matières/niveaux sont dénormalisés en **noms** (les arbres
  ``/subjects/tree`` et ``/education-levels/tree`` sont derrière JWT).
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class PublicQuestionRead(BaseModel):
    """Question d'exercice vue élève — sans le corrigé, par construction."""

    id: str
    enonce: str
    type: str


class PublicBlockRead(BaseModel):
    # ``content`` : écho brut du JSONB pour texte/document/module ; pour un
    # bloc exercice, dict reconstruit sans les ``reponse_attendue``.
    id: uuid.UUID
    position: int
    type: str
    titre: str | None
    description: str | None
    content: dict[str, Any]
    resource_id: uuid.UUID | None
    module_id: uuid.UUID | None


class PublicResourceRead(BaseModel):
    """Ressource ``disponible`` du cours (jamais de ``s3_key``).

    Embarquée dans le détail public : le front résout ses références
    ``oc-resource:`` sans endpoint de liste dédié.
    """

    id: uuid.UUID
    type: str
    nom_original: str
    taille: int
    mime: str


class PublicCourseRead(BaseModel):
    id: uuid.UUID
    titre: str
    description: str | None
    # Noms dénormalisés, triés — pas d'ids : les taxonomies sont privées.
    subjects: list[str]
    education_levels: list[str]
    block_count: int
    # Écho brut du JSONB (style de lecture du cours, appliqué par le front).
    preview_settings: dict[str, Any]
    updated_at: datetime


class PublicCourseDetailRead(PublicCourseRead):
    blocks: list[PublicBlockRead]
    resources: list[PublicResourceRead]


class PublicModuleRead(BaseModel):
    """Code d'un module interactif — exécuté en iframe sandbox côté front."""

    id: uuid.UUID
    titre: str
    html: str
    css: str
    js: str


class PublicDownloadRead(BaseModel):
    """URL présignée de lecture (TTL court) — miroir de ``ResourceDownload``."""

    download_url: str
    expires_in: int


class PublicProfessorRead(BaseModel):
    """Catalogue public d'un prof : ses cours ``public`` uniquement.

    ``nom_public`` est ``None`` si le prof n'en a pas choisi (catalogue
    anonyme) — et aussi si l'utilisateur n'existe pas : la réponse est
    identique (liste vide), pas d'oracle d'existence d'un compte.
    """

    nom_public: str | None
    courses: list[PublicCourseRead]
