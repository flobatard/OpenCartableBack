"""Liens de partage publics d'un cours (jalon J2).

Un lien = une *capability URL* : le ``token`` opaque (``secrets.token_urlsafe(32)``,
256 bits d'entropie, URL-safe) est l'unique credential d'accès en lecture seule
au cours pour les élèves sans compte (Descriptions.md §5.6). Il est stocké
**en clair** — décision actée : l'UI prof doit pouvoir recopier l'URL à tout
moment, le contenu partagé est volontairement diffusable et 256 bits rendent
le brute-force en ligne impossible. Durcissement possible plus tard : stocker
un hash SHA-256 et ne montrer le token qu'à la création.

Un cours peut porter plusieurs liens (un par classe, par exemple), chacun
révocable individuellement (``revoked``, soft delete — le lien reste listé
côté prof) et expirant à ``expires_at`` (``SHARE_LINK_TTL_DAYS``, 9 mois par
défaut). Un lien valide n'ouvre le cours que si sa visibilité l'autorise :
``courses.visibility = 'draft'`` suspend tous les liens du cours sans les
supprimer (vérification à chaque accès, cf. ``app/public/service.py``).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ShareLink(Base):
    __tablename__ = "share_links"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    # token_urlsafe(32) = 43 caractères ; l'index unique sert aussi de lookup.
    token: Mapped[str] = mapped_column(String(64), unique=True)
    # Aide-mémoire du prof pour distinguer ses liens (« 6eB 2026 »).
    label: Mapped[str | None] = mapped_column(String(200))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
