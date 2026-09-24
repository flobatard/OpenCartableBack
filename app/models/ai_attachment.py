"""Pièces jointes des conversations de l'assistant IA.

Fichiers que le professeur joint à un message de l'assistant (chat global ou
chat d'édition) pour que le modèle travaille dessus : photo d'un exercice de
manuel, sujet PDF, barème bureautique. Le binaire vit sur S3 (bucket privé,
URL présignées) ; la base ne porte que les métadonnées, motif
:mod:`app.models.resource` — ligne créée **avant** l'upload direct
navigateur → S3 avec ``status='pending'``, l'endpoint de confirmation vérifie
l'objet (HEAD S3 : taille **et** type, motif ``confirm_avatar``) et passe le
statut à ``'available'``.

**Ce n'est pas une ressource du cours** : une ``Resource`` appartient à la
bibliothèque du cours, que le partage public expose **en entier** à tous les
élèves (décision 7) ; une pièce jointe est un appui de travail privé du
professeur, jamais servi par le régime public. D'où la table séparée.

Cycle de vie en deux temps, qui explique les deux FK nullables :

- à l'upload, la pièce n'appartient qu'au couple (cours, propriétaire) — le
  front peut joindre un fichier alors que la conversation est encore un
  brouillon sans id ;
- à l'envoi du message, le flux du tour pose ``conversation_id`` et
  ``message_id`` (:mod:`app.course_assistant.streaming`). Une pièce rattachée
  est **indélébile** (409 sur la route de suppression) : l'``enum`` du tool
  ``read_attachment`` doit rester identique entre l'aller d'un tour et sa
  reprise HITL, sans quoi l'état checkpointé référencerait un tool absent.

Une pièce restée sans ``message_id`` est du déchet (upload abandonné, ou
confirmé puis jamais envoyé) : le job de maintenance ``ai_attachments`` la
purge, ligne et objet S3.

``kind`` est la **famille de traitement**, déduite du mime par la whitelist
fermée :data:`~app.course_assistant.attachments.ATTACHMENT_TYPES` — elle
décide du plafond de taille et de la façon dont le tool sert la pièce au
modèle (image montrée telle quelle, ou texte extrait).
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

KIND_IMAGE = "image"
KIND_PDF = "pdf"
KIND_TEXT = "text"
KIND_OFFICE = "office"

STATUS_PENDING = "pending"
STATUS_AVAILABLE = "available"
# Alias pour les modules qui importent DÉJÀ le STATUS_AVAILABLE des ressources
# (app/course_assistant/tools.py) : deux statuts homonymes, deux tables.
ATTACHMENT_AVAILABLE = STATUS_AVAILABLE


class AIAttachment(Base):
    __tablename__ = "ai_attachments"
    __table_args__ = (
        UniqueConstraint("s3_key", name="uq_ai_attachments_s3_key"),
        CheckConstraint(
            f"kind IN ('{KIND_IMAGE}', '{KIND_PDF}', '{KIND_TEXT}', '{KIND_OFFICE}')",
            name="ck_ai_attachments_kind",
        ),
        CheckConstraint(
            f"status IN ('{STATUS_PENDING}', '{STATUS_AVAILABLE}')",
            name="ck_ai_attachments_status",
        ),
        CheckConstraint("size >= 0", name="ck_ai_attachments_size_positive"),
        # Rattachée à un message ⇒ rattachée à sa conversation, et confirmée :
        # le tour ne lie que des pièces dont l'objet S3 a été vérifié.
        CheckConstraint(
            "message_id IS NULL OR "
            f"(conversation_id IS NOT NULL AND status = '{STATUS_AVAILABLE}')",
            name="ck_ai_attachments_binding",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Nullables jusqu'à l'envoi du message qui les porte (cf. docstring).
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_messages.id", ondelete="CASCADE")
    )
    # « courses/<course_id>/assistant/<attachment_id>/<nom-sanitizé> » ;
    # 1024 = longueur max d'une clé S3. Sous le préfixe « courses/ » balayé
    # par la réconciliation des orphelins (app/maintenance/service.py).
    s3_key: Mapped[str] = mapped_column(String(1024))
    original_name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(255))
    # Famille de traitement déduite du mime (ATTACHMENT_TYPES).
    kind: Mapped[str] = mapped_column(String(20))
    # Octets, déclarée au presign, vérifiée à la confirmation d'upload.
    size: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(
        String(15), default=STATUS_PENDING, server_default=STATUS_PENDING
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
