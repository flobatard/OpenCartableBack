"""Modules interactifs HTML/CSS/JS d'un cours (jalon J4, anticipé).

Un module = trois morceaux de code (``html``, ``css``, ``js``) écrits par le
prof dans l'éditeur intégré du front et **stockés en base** (décision actée :
remplace le bundle .zip sur S3 du cadrage initial — aucun objet S3, aucune
purge storage aux suppressions). Le code est exécuté côté front dans une
``<iframe sandbox>`` **sans** ``allow-same-origin`` (origine opaque, composée
via ``srcdoc``) : il n'a jamais accès aux cookies, au localStorage, aux
tokens ni au DOM de l'app ; seul un pont ``postMessage`` contrôlé
(``oc-module:*`` — auto-resize + événements) le relie à la page.

Les modules forment la **bibliothèque de modules du cours**, indépendante des
blocs (motif ``resources``) : un bloc ``module`` peut en pointer un
(``blocks.module_id``, FK ``CASCADE`` : supprimer le module supprime ses
blocs pointeurs), mais un module existe sans bloc et peut aussi être intégré
dans le markdown d'un bloc texte (``oc-module:<id>``).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Module(Base):
    __tablename__ = "modules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), index=True
    )
    titre: Mapped[str] = mapped_column(String(255))
    html: Mapped[str] = mapped_column(Text, default="", server_default="")
    css: Mapped[str] = mapped_column(Text, default="", server_default="")
    js: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
