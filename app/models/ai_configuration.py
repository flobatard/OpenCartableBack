"""Configurations IA nommées d'un utilisateur (app/ai_credentials/).

Plusieurs lignes par utilisateur (plafond métier ``MAX_CONFIGURATIONS`` en
service), chacune un couple provider/modèle complet avec sa propre clé API
chiffrée par app/core/crypto.py (AES-256-GCM, blob versionné) et un sel PAR
LIGNE régénéré à chaque écriture de clé. Au plus UNE ligne active par
utilisateur — invariant porté par l'index partiel unique
``uq_ai_configurations_active`` ; aucune ligne active = l'utilisateur
consomme l'IA par défaut du serveur (fallback ``AI_*``, sous quota, voir
``users.ai_daily_call_quota``). Comme avatar_s3_key, la clé (chiffrée ou non)
ne figure dans AUCUN schéma de réponse — seule sort la projection
``api_key_set: bool``.

Les règles PAR provider (clé requise ou non, base_url requise/interdite,
capacités de raisonnement) sont métier → 422 en service, jamais en CHECK
(ajouter un provider ne doit pas exiger de migration). L'index partiel
unique n'est pas différable : désactiver l'ancienne ligne et activer la
nouvelle sont deux instructions séparées, jamais un seul UPDATE.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AIConfiguration(Base):
    __tablename__ = "ai_configurations"
    __table_args__ = (
        CheckConstraint(
            "(api_key_encrypted IS NULL) = (encryption_salt IS NULL)",
            name="ck_ai_configurations_key_salt",
        ),
        # Au plus une configuration active par utilisateur ; aucune = IA par
        # défaut. Index partiel : les lignes inactives ne sont pas contraintes.
        Index(
            "uq_ai_configurations_active",
            "user_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Libellé choisi par l'utilisateur (affiché dans les listes de bascule).
    name: Mapped[str] = mapped_column(String(100))
    # provider ∈ AIProvider (validé Pydantic).
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str | None] = mapped_column(String(2000))
    api_key_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_salt: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Préférences de raisonnement : NULL = défaut du provider/modèle ;
    # reasoning True = demandé et affiché, False = coupé ; reasoning_effort =
    # niveau NATIF du provider (validé Pydantic, REASONING_EFFORT_MAX_LENGTH).
    reasoning: Mapped[bool | None] = mapped_column(Boolean)
    reasoning_effort: Mapped[str | None] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Pas de relation ORM vers users (lazy-load async interdit) : le service
    # fait des selects explicites scopés par user_id.
