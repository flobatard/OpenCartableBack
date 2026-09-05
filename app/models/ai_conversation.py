"""Conversations de l'assistant IA d'un cours.

Une conversation rattache un fil de messages (:mod:`app.models.ai_message`) à
un cours, son propriétaire et un **contexte** d'usage — les flux HITL de
l'assistant diffèrent par contexte mais partagent ce modèle :

- ``course`` : assistant global du cours (critique, exploration, synthèse) ;
- ``block_text`` / ``block_exercise`` : aide à l'édition d'un bloc (le bloc
  visé vit dans ``block_id``) ;
- ``module`` : aide à l'édition d'un module (``module_id``).

Les flux HITL des contextes d'édition sont décrits par les descripteurs de
:mod:`app.course_assistant.editing`.

La résolution d'exercice côté élève n'est **pas une
conversation** : elle a sa propre table par tentative et par question
(:mod:`app.models.exercise_submission`, tuteur de :mod:`app.student_exercises`)
— elle n'apparaît donc pas dans le CHECK.

Cohérence cible (CHECK symétrique, motif ``blocks``) : ``course`` ne pointe
rien ; les contextes ``block_*`` exigent ``block_id`` (et jamais
``module_id``) ; ``module`` exige ``module_id`` (et jamais ``block_id``).
FK toutes en ``CASCADE`` : supprimer le cours, le bloc ou le module visé
emporte les conversations attenantes.

``updated_at`` sert au tri de la liste des conversations : il est bumpé **côté
Python** par le service à chaque message persisté (le ``onupdate`` SQL ne
tirerait pas sans écriture d'une autre colonne).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

CONTEXT_COURSE = "course"
CONTEXT_BLOCK_TEXT = "block_text"
CONTEXT_BLOCK_EXERCISE = "block_exercise"
CONTEXT_MODULE = "module"


class AIConversation(Base):
    __tablename__ = "ai_conversations"
    __table_args__ = (
        CheckConstraint(
            f"context IN ('{CONTEXT_COURSE}', '{CONTEXT_BLOCK_TEXT}', "
            f"'{CONTEXT_BLOCK_EXERCISE}', '{CONTEXT_MODULE}')",
            name="ck_ai_conversations_context",
        ),
        # Cible cohérente avec le contexte (voir docstring du module).
        CheckConstraint(
            f"(context = '{CONTEXT_COURSE}' AND block_id IS NULL AND module_id IS NULL) "
            f"OR (context IN ('{CONTEXT_BLOCK_TEXT}', '{CONTEXT_BLOCK_EXERCISE}') "
            "AND block_id IS NOT NULL AND module_id IS NULL) "
            f"OR (context = '{CONTEXT_MODULE}' AND module_id IS NOT NULL AND block_id IS NULL)",
            name="ck_ai_conversations_target",
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
    context: Mapped[str] = mapped_column(String(20))
    block_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("blocks.id", ondelete="CASCADE")
    )
    module_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("modules.id", ondelete="CASCADE")
    )
    # Posé par le service au premier message utilisateur (tronqué), renommable.
    title: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
