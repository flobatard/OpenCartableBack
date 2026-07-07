"""Modules pédagogiques interactifs : métadonnées des bundles HTML/JS.

Spécialisation 0..1 d'une ressource (``RESOURCE ||--o| MODULE``) : un module
est une ressource de ``type='module'`` — bundle HTML/CSS/JS auto-porté sur S3
(Descriptions.md §5.5) — complétée par sa version et son point d'entrée. Le
versionnage passe par la clé S3 (``module-id/vN/...``) ; le rendu se fera en
``<iframe sandbox>`` servie depuis une origine séparée (jalon J4).

La cohérence « ``resource_id`` pointe une ressource de ``type='module'`` »
n'est pas exprimable en CHECK (cross-table) — validation en service au J4.
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


class Module(Base):
    __tablename__ = "modules"
    __table_args__ = (
        # ||--o| : au plus un module par ressource.
        UniqueConstraint("resource_id", name="uq_modules_resource_id"),
        CheckConstraint("version >= 1", name="ck_modules_version_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    entrypoint: Mapped[str] = mapped_column(
        String(255), default="index.html", server_default="index.html"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
