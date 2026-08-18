"""Schémas de la recherche publique (J3) — lecture seule, sans identité.

Même règle d'or que ``app/public/schemas.py`` : ne JAMAIS exposer une donnée
réservée au prof. Les cartes de cours réutilisent ``PublicCourseRead`` (contrat
J2 inchangé, même carte côté front) ; un prof n'expose que ``nom_public`` et
ses matières enseignées (noms dénormalisés).

Première enveloppe paginée de l'API (précédent J3) : ``{items, total, limit,
offset}`` — ``total`` est le nombre de résultats toute pagination confondue,
``limit``/``offset`` sont l'écho des paramètres effectifs de la requête.
"""

import uuid

from pydantic import BaseModel

from app.public.schemas import PublicCourseRead


class SearchCoursesPage(BaseModel):
    items: list[PublicCourseRead]
    total: int
    limit: int
    offset: int


class PublicTeacherRead(BaseModel):
    """Prof cherchable : opt-in ``cherchable`` + ``nom_public`` + au moins un
    cours public (règle du service). Jamais d'email ni de ``sub``."""

    id: uuid.UUID
    nom_public: str
    # Matières que le prof déclare enseigner (profil « enseigne »),
    # noms dénormalisés triés — motif J2.
    subjects: list[str]
    public_course_count: int


class SearchTeachersPage(BaseModel):
    items: list[PublicTeacherRead]
    total: int
    limit: int
    offset: int
