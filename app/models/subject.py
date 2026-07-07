"""Taxonomie hiérarchique des matières.

Table auto-référencée : discipline (profondeur 0) > domaine (1) >
sous-domaine (2) > sujet (3). La profondeur est flexible : une branche
peut s'arrêter avant le niveau 3.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

PROFONDEUR_MAX = 3  # 0=discipline, 1=domaine, 2=sous-domaine, 3=sujet


class Subject(Base):
    __tablename__ = "subjects"
    __table_args__ = (
        # NULLS NOT DISTINCT (Postgres 15+) : couvre aussi l'unicité des
        # noms de disciplines racines (parent_id NULL).
        UniqueConstraint(
            "parent_id",
            "nom",
            name="uq_subjects_parent_id_nom",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            f"profondeur >= 0 AND profondeur <= {PROFONDEUR_MAX}",
            name="ck_subjects_profondeur",
        ),
        CheckConstraint("parent_id != id", name="ck_subjects_pas_son_propre_parent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), index=True
    )
    nom: Mapped[str] = mapped_column(String(200))
    # Chemin slug complet, stable et unique (ex. "mathematiques.algebre").
    code: Mapped[str] = mapped_column(String(500), unique=True)
    profondeur: Mapped[int] = mapped_column(SmallInteger)
    position: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relations prêtes pour le CRUD J1 ; l'API de lecture charge la table
    # à plat et n'y touche pas (lazy-load async interdit).
    parent: Mapped["Subject | None"] = relationship(
        back_populates="children", remote_side=[id]
    )
    children: Mapped[list["Subject"]] = relationship(
        back_populates="parent",
        order_by="Subject.position",
        passive_deletes=True,
    )
