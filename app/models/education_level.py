"""Classification hiérarchique des niveaux d'étude.

Table auto-référencée à deux profondeurs : cycle (0, ex. « Collège ») >
classe (1, ex. « 6e »). Chaque arbre appartient à un système scolaire
(``systeme``, « fr » pour l'instant) : les noms de cycles/classes sont des
noms propres nationaux, jamais traduits. Le rapprochement entre systèmes
passe par les pivots internationaux ``cite`` (CITE/ISCED 2011, UNESCO,
NULL quand le nœud couvre plusieurs niveaux CITE — ex. « Supérieur ») et
``age_min``/``age_max`` (âges typiques).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

PROFONDEUR_MAX = 1  # 0=cycle, 1=classe
CITE_MAX = 8  # CITE/ISCED 2011 : 0 (préélémentaire) à 8 (doctorat)


class EducationLevel(Base):
    __tablename__ = "education_levels"
    __table_args__ = (
        # NULLS NOT DISTINCT (Postgres 15+) : couvre l'unicité des cycles
        # racines (parent_id NULL) ; ``systeme`` en tête pour qu'un futur
        # arbre étranger puisse réutiliser un nom de racine (ex. « Primaire »).
        UniqueConstraint(
            "systeme",
            "parent_id",
            "nom",
            name="uq_education_levels_systeme_parent_id_nom",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            f"profondeur >= 0 AND profondeur <= {PROFONDEUR_MAX}",
            name="ck_education_levels_profondeur",
        ),
        CheckConstraint("parent_id != id", name="ck_education_levels_pas_son_propre_parent"),
        CheckConstraint(
            f"cite IS NULL OR (cite >= 0 AND cite <= {CITE_MAX})",
            name="ck_education_levels_cite",
        ),
        CheckConstraint(
            "(age_min IS NULL OR age_min >= 0)"
            " AND (age_max IS NULL OR age_max >= 0)"
            " AND (age_min IS NULL OR age_max IS NULL OR age_min <= age_max)",
            name="ck_education_levels_ages",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("education_levels.id", ondelete="CASCADE"), index=True
    )
    nom: Mapped[str] = mapped_column(String(200))
    # Slug stable écrit à la main, préfixé par le système (ex. "fr.college.6e") —
    # contrairement aux subjects, JAMAIS dérivé du nom affiché.
    code: Mapped[str] = mapped_column(String(100), unique=True)
    systeme: Mapped[str] = mapped_column(String(20))
    cite: Mapped[int | None] = mapped_column(SmallInteger)
    age_min: Mapped[int | None] = mapped_column(SmallInteger)
    age_max: Mapped[int | None] = mapped_column(SmallInteger)
    profondeur: Mapped[int] = mapped_column(SmallInteger)
    position: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Pas de relations ORM parent/children : aucun CRUD prévu (classification
    # figée, ajouts par data migrations) et lazy-load async interdit.
