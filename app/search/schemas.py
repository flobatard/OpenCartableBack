"""Schémas de la recherche publique — lecture seule, sans identité.

Même règle d'or que ``app/public/schemas.py`` : ne JAMAIS exposer une donnée
réservée au prof. Les cartes de cours réutilisent ``PublicCourseRead`` (même
carte côté front) ; un prof n'expose que ``public_name`` et
ses matières enseignées (noms dénormalisés).

Enveloppe paginée de référence de l'API : ``{items, total, limit,
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
    """Prof cherchable : opt-in ``searchable`` + ``public_name`` + au moins un
    cours public (règle du service). Jamais d'email ni de ``sub``."""

    id: uuid.UUID
    public_name: str
    # URL présignée inline de la photo de profil (TTL court), None si le prof
    # n'en a pas — jamais la clé S3 (règle d'or ci-dessus).
    avatar_url: str | None
    # Matières que le prof déclare enseigner (profil « teaching »),
    # noms dénormalisés triés (comme les cartes de cours).
    subjects: list[str]
    public_course_count: int


class SearchTeachersPage(BaseModel):
    items: list[PublicTeacherRead]
    total: int
    limit: int
    offset: int
