"""Comptage quotidien des appels servis par l'IA PAR DÉFAUT (fallback AI_*).

Une ligne par (utilisateur, jour **UTC**), créée au premier appel du jour puis
incrémentée par un ``INSERT … ON CONFLICT DO UPDATE`` **atomique** dont le
plafond vit dans le WHERE (:mod:`app.ai_credentials.service`) : deux requêtes
concurrentes ne peuvent pas dépasser le quota. Les appels BYO token (config
explicite de la requête ou credential utilisateur) ne sont jamais comptés.
Le plafond effectif (``users.ai_quota_appels`` sinon
``settings.AI_DEFAULT_DAILY_QUOTA``, 0 = illimité) n'est PAS stocké ici :
il est résolu à chaque appel.
"""

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AIDailyUsage(Base):
    __tablename__ = "ai_daily_usage"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # Jour UTC (pas le fuseau du serveur) : la fenêtre bascule à minuit UTC.
    jour: Mapped[date] = mapped_column(Date, primary_key=True)
    appels: Mapped[int] = mapped_column(Integer, nullable=False)

    # Pas de relation ORM (lazy-load async interdit) ni de purge automatique :
    # une ligne par utilisateur actif et par jour, volume négligeable à court
    # terme — stratégie de purge à prévoir, suivie dans le TODO.md racine.
