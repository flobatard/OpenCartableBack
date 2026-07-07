"""Cours composés par un prof : suite ordonnée de blocs.

Un cours appartient à un utilisateur (``owner_id``), porte un titre et une
description optionnelle (markdown court), et est classé par matières
(``course_subjects``) et par niveaux d'étude (``course_education_levels``) —
deux M2M sans qualificatif, un cours pouvant relever de plusieurs matières
et viser plusieurs classes. Son contenu vit dans la table ``blocks``
(cf. :mod:`app.models.block`), ses fichiers S3 dans ``resources``
(cf. :mod:`app.models.resource`).

À venir : ``search_vector`` tsvector pour la FTS (jalon J3) et les liens de
partage publics ``share_links`` (jalon J2).
"""

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Table, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    titre: Mapped[str] = mapped_column(String(300))
    # Markdown court, présentation du cours.
    description: Mapped[str | None] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

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
