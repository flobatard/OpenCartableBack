"""Fichiers S3 rattachés à un cours (documents, images, audio, vidéo, modules).

Le binaire vit sur S3 (bucket privé, URL présignées) ; la base ne porte que
les métadonnées et toute la hiérarchie logique — les clés S3 restent plates
(``uuid/nom-original``, Descriptions.md §5.2). La ligne est créée **avant**
l'upload direct navigateur → S3 (presigned PUT) avec ``statut='en_attente'`` ;
l'endpoint de confirmation vérifie l'objet (HEAD S3) et passe le
statut à ``'disponible'`` — c'est le mécanisme de cohérence DB↔S3.

Les ressources forment la **bibliothèque du cours**, indépendante des blocs :
un bloc ``document`` peut pointer une ressource (``blocks.resource_id``,
FK ``CASCADE`` : supprimer la ressource supprime ses blocs pointeurs), mais
une ressource existe sans bloc.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

TYPE_DOCUMENT = "document"
TYPE_IMAGE = "image"
TYPE_AUDIO = "audio"
TYPE_VIDEO = "video"

STATUT_EN_ATTENTE = "en_attente"
STATUT_DISPONIBLE = "disponible"


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("s3_key", name="uq_resources_s3_key"),
        CheckConstraint(
            f"type IN ('{TYPE_DOCUMENT}', '{TYPE_IMAGE}', '{TYPE_AUDIO}', "
            f"'{TYPE_VIDEO}')",
            name="ck_resources_type",
        ),
        CheckConstraint(
            f"statut IN ('{STATUT_EN_ATTENTE}', '{STATUT_DISPONIBLE}')",
            name="ck_resources_statut",
        ),
        CheckConstraint("taille >= 0", name="ck_resources_taille_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(20))
    # Clé plate « uuid/nom-original » ; 1024 = longueur max d'une clé S3.
    s3_key: Mapped[str] = mapped_column(String(1024))
    nom_original: Mapped[str] = mapped_column(String(255))
    # Octets, déclarée au presign, vérifiée à la confirmation d'upload.
    taille: Mapped[int] = mapped_column(BigInteger)
    mime: Mapped[str] = mapped_column(String(255))
    statut: Mapped[str] = mapped_column(
        String(15), default=STATUT_EN_ATTENTE, server_default=STATUT_EN_ATTENTE
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
