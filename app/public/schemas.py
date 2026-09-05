"""Schémas des routes publiques élèves — lecture seule, sans identité.

Règle d'or : ne JAMAIS exposer ici une donnée réservée au prof. Concrètement :
- ``PublicQuestionRead`` n'a **pas de champ** ``expected_answer`` — le
  filtrage du corrigé est structurel (reconstruction du content exercice
  dans le service), pas un ``exclude`` fragile ;
- pas d'``owner_id``, pas d'email, pas de ``s3_key`` ; la seule donnée
  d'identité publique est ``users.public_name`` (choisie par le prof) ;
- les matières/niveaux sont dénormalisés en **noms** : les cartes front n'ont
  aucune résolution d'arbre à faire (les arbres sont aussi exposés en lecture
  publique, ``/public/*/tree``, pour les facettes de recherche — la
  dénormalisation reste la forme des cartes).
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class PublicQuestionRead(BaseModel):
    """Question d'exercice vue élève — sans le corrigé, par construction."""

    id: str
    statement: str
    type: str


class PublicBlockRead(BaseModel):
    # ``content`` : écho brut du JSONB pour texte/document/module ; pour un
    # bloc exercice, dict reconstruit sans les ``expected_answer``.
    id: uuid.UUID
    position: int
    type: str
    title: str | None
    description: str | None
    content: dict[str, Any]
    resource_id: uuid.UUID | None
    module_id: uuid.UUID | None


class PublicResourceRead(BaseModel):
    """Ressource ``available`` du cours (jamais de ``s3_key``).

    Embarquée dans le détail public : le front résout ses références
    ``oc-resource:`` sans endpoint de liste dédié.
    """

    id: uuid.UUID
    type: str
    original_name: str
    size: int
    mime: str


class PublicModuleSummary(BaseModel):
    """Module interactif du cours — titre seul, JAMAIS le code.

    Embarqué dans le détail public (motif ``PublicResourceRead``) pour que
    l'onglet Modules de la vue élève liste la bibliothèque sans requête
    supplémentaire ; le code est servi à la demande, module par module, par
    ``/public/courses/{id}/modules/{mid}``. Miroir public de ``ModuleSummary``
    sans les timestamps : aucun consommateur côté rendu.
    """

    id: uuid.UUID
    title: str


class PublicCourseRead(BaseModel):
    id: uuid.UUID
    title: str
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
    modules: list[PublicModuleSummary]


class PublicModuleRead(BaseModel):
    """Code d'un module interactif — exécuté en iframe sandbox côté front."""

    id: uuid.UUID
    title: str
    html: str
    css: str
    js: str


class PublicDownloadRead(BaseModel):
    """URL présignée de lecture (TTL court) — miroir de ``ResourceDownload``."""

    download_url: str
    expires_in: int


class PublicProfessorRead(BaseModel):
    """Catalogue public d'un prof : ses cours ``public`` uniquement.

    ``public_name`` est ``None`` si le prof n'en a pas choisi (catalogue
    anonyme) — et aussi si l'utilisateur n'existe pas : la réponse est
    identique (liste vide), pas d'oracle d'existence d'un compte.
    ``avatar_url`` suit la même règle (URL présignée inline, jamais la clé
    S3 — règle d'or ci-dessus ; ``None`` aussi pour un utilisateur inconnu).
    """

    public_name: str | None
    avatar_url: str | None
    courses: list[PublicCourseRead]
