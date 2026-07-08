"""Blocs ordonnés composant le contenu d'un cours.

Un cours = une liste de blocs triés par ``position`` (tri stable
``ORDER BY position, id`` ; pas d'unicité ``(course_id, position)`` en base —
un swap la violerait en cours de transaction — le réordonnancement réécrit
les positions en bloc côté service). ``titre``/``description`` sont des
métadonnées facultatives communes à tous les types (affichage en en-tête du
bloc), distinctes du ``content`` JSONB qui porte le contenu éditorial ; son
schéma est un contrat applicatif, par ``type`` :

- ``texte`` : ``{"markdown": "..."}`` — cours magistral en markdown simple
  (pas de HTML brut), directement consultable par l'IA. Le markdown peut
  contenir des formules LaTeX — ``$…$`` en ligne, ``$$…$$`` centrée —
  stockées telles quelles et rendues par KaTeX côté front.
- ``exercice`` : ``{"enonce": "md", "questions": [{"id": "<uuid4>",
  "enonce": "md", "type": "texte_libre"}]}`` — les ``questions[].id`` sont
  générés côté service et **stables à vie** : les futures soumissions élèves
  (jalon J2) référenceront ``(block_id, question_id)``, et la review IA aussi.
  Ne jamais régénérer ces ids à l'édition.
- ``ressource`` : ``{"legende": "...", "affichage": "inline" |
  "telechargement"}`` (optionnels) — le binaire est dans la ligne
  ``resources`` référencée par ``resource_id`` (seul type de bloc à en
  porter une, CHECK de cohérence).
- ``lien`` : ``{"url": "https://...", "titre": "...", "fournisseur":
  "youtube" | null}`` — embed externe, rien sur S3.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

TYPE_TEXTE = "texte"
TYPE_EXERCICE = "exercice"
TYPE_RESSOURCE = "ressource"
TYPE_LIEN = "lien"


class Block(Base):
    __tablename__ = "blocks"
    __table_args__ = (
        # Couvre aussi la lecture des blocs d'un cours (pas d'index séparé
        # sur course_id).
        Index("ix_blocks_course_id_position", "course_id", "position"),
        CheckConstraint(
            f"type IN ('{TYPE_TEXTE}', '{TYPE_EXERCICE}', "
            f"'{TYPE_RESSOURCE}', '{TYPE_LIEN}')",
            name="ck_blocks_type",
        ),
        # Seuls (et tous) les blocs « ressource » portent une FK resource.
        CheckConstraint(
            f"(type = '{TYPE_RESSOURCE}') = (resource_id IS NOT NULL)",
            name="ck_blocks_ressource_coherence",
        ),
        CheckConstraint("position >= 0", name="ck_blocks_position_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE")
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    type: Mapped[str] = mapped_column(String(20))
    titre: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
