"""Utilisateurs authentifiés de l'application (profs et/ou élèves).

La ligne est créée par auto-provisioning au premier ``GET /users/me``
porteur d'un JWT valide : ``sub`` est l'identifiant OIDC opaque (Zitadel
aujourd'hui — aucune autre donnée IdP n'est persistée), ``id`` l'identifiant
interne, seul à référencer depuis les autres tables. Les rôles sont
cumulables (un enseignant peut aussi apprendre) ; le profil est complet
quand ``onboarded_at`` est posé. Matières et niveaux du profil vivent dans
les tables d'association, qualifiées par ``contexte`` (« enseigne » /
« apprend ») — c'est lui, pas le rôle, qui porte la sémantique d'une ligne.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    String,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

CONTEXTE_ENSEIGNE = "enseigne"
CONTEXTE_APPREND = "apprend"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("sub", name="uq_users_sub"),
        CheckConstraint(
            "onboarded_at IS NULL OR est_prof OR est_eleve",
            name="ck_users_onboarde_implique_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    sub: Mapped[str] = mapped_column(String(255))
    # Snapshot du claim, rafraîchi à chaque lecture du profil si différent.
    email: Mapped[str | None] = mapped_column(String(320))
    est_prof: Mapped[bool] = mapped_column(default=False, server_default="false")
    est_eleve: Mapped[bool] = mapped_column(default=False, server_default="false")
    # Même dimension que education_levels.systeme ; validé en service
    # (pas de FK possible : les systèmes ne sont pas une table).
    systeme_scolaire: Mapped[str | None] = mapped_column(String(20))
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Pas de relations ORM vers subjects/education_levels : lazy-load async
    # interdit, le service fait des selects explicites sur les tables Core.


# ``user_id`` en tête des PK composites : l'index de PK couvre la lecture
# du profil (WHERE user_id = ...), pas d'index séparé nécessaire.
user_subjects = Table(
    "user_subjects",
    Base.metadata,
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "subject_id",
        UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("contexte", String(10), primary_key=True),
    CheckConstraint(
        f"contexte IN ('{CONTEXTE_ENSEIGNE}', '{CONTEXTE_APPREND}')",
        name="ck_user_subjects_contexte",
    ),
)

user_education_levels = Table(
    "user_education_levels",
    Base.metadata,
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "education_level_id",
        UUID(as_uuid=True),
        ForeignKey("education_levels.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("contexte", String(10), primary_key=True),
    CheckConstraint(
        f"contexte IN ('{CONTEXTE_ENSEIGNE}', '{CONTEXTE_APPREND}')",
        name="ck_user_education_levels_contexte",
    ),
)
