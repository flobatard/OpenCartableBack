"""Cours composés par un prof : suite ordonnée de blocs.

Un cours appartient à un utilisateur (``owner_id``), porte un titre et une
description optionnelle (markdown court), et est classé par matières
(``course_subjects``) et par niveaux d'étude (``course_education_levels``) —
deux M2M sans qualificatif, un cours pouvant relever de plusieurs matières
et viser plusieurs classes. Son contenu vit dans la table ``blocks``
(cf. :mod:`app.models.block`), ses fichiers S3 dans ``resources``
(cf. :mod:`app.models.resource`).

La ``visibility`` (jalon J2) pilote le régime d'accès élève (routes publiques
``app/public/``, sans JWT) : ``public`` = accessible par URL directe et listé
dans le catalogue public du prof ; ``private`` = accessible uniquement via un
lien de partage valide (cf. :mod:`app.models.share_link`) ; ``draft``
(défaut) = inaccessible publiquement, y compris via un lien existant (les
liens sont suspendus, pas supprimés). Côté prof, la visibilité ne change
jamais rien : ses routes restent scopées ``owner_id``.

``search_vector`` (jalon J3) : tsvector de la recherche plein texte,
title (poids A) + description (poids B), config ``french_unaccent``.
Maintenu exclusivement par trigger PostgreSQL (``trg_courses_search_vector``,
fonction ``courses_tsvector`` partagée avec le backfill de la migration) —
jamais écrit par l'ORM. Le contenu des blocs a son propre vecteur
(cf. :mod:`app.models.block`) ; les deux sont combinés à la requête,
pas consolidés ici.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

VISIBILITY_PUBLIC = "public"
VISIBILITY_PRIVATE = "private"
VISIBILITY_DRAFT = "draft"
VISIBILITIES = (VISIBILITY_PUBLIC, VISIBILITY_PRIVATE, VISIBILITY_DRAFT)


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('public', 'private', 'draft')",
            name="ck_courses_visibility",
        ),
        # Index GIN de la FTS (J3). Créé par la migration J3 ; déclaré ici
        # pour que la metadata reflète la base — sans cette ligne, chaque
        # autogenerate propose un drop_index destructeur (il ne voit que
        # modèles vs base, jamais les migrations).
        Index("ix_courses_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    # Markdown court, présentation du cours.
    description: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Réglages d'affichage de la preview (typographie / mise en page), propriété
    # du cours. Contrat JSONB = interface CourseStyleSettings du front (clés
    # camelCase) : fontSizePx, headingScale, lineHeight, widthCh, paragraphGapEm,
    # font ∈ {"sans","serif"}. Édité via PUT .../preview (remplacement complet,
    # validé par PreviewSettings) ; {} tant que non personnalisé.
    preview_settings: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    # Régime d'accès élève (voir docstring du module). server_default 'draft' :
    # aucun cours existant ne devient public à la migration — publier est un
    # opt-in explicite du prof (PUT .../visibility).
    visibility: Mapped[str] = mapped_column(
        String(10), default=VISIBILITY_DRAFT, server_default=text("'draft'")
    )
    # FTS (J3) : maintenu par trigger PostgreSQL, JAMAIS écrit par l'ORM
    # (voir docstring du module). deferred : aucun service ne le lit, inutile
    # de charger le vecteur à chaque select d'entité (contrainte Pi).
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, deferred=True)

    # Pas de relations ORM vers blocks/resources/subjects/education_levels :
    # lazy-load async interdit, les services font des selects explicites.


# ``course_id`` en tête des PK composites : l'index de PK couvre la lecture
# du classement d'un cours. L'index inverse sert les facettes de recherche
# (« les cours d'une matière / d'un niveau », Descriptions.md §5.4).
course_subjects = Table(
    "course_subjects",
    Base.metadata,
    Column(
        "course_id",
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "subject_id",
        UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_course_subjects_subject_id", "subject_id"),
)

course_education_levels = Table(
    "course_education_levels",
    Base.metadata,
    Column(
        "course_id",
        UUID(as_uuid=True),
        ForeignKey("courses.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "education_level_id",
        UUID(as_uuid=True),
        ForeignKey("education_levels.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_course_education_levels_education_level_id", "education_level_id"),
)
