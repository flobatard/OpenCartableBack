"""Messages d'une conversation de l'assistant IA.

Trois rôles, miroir du ``ChatMessage`` de :mod:`app.core.ai` (les tours
``system`` — le contexte du cours — sont recomposés à chaque appel, jamais
persistés ; le « thinking » streamé ne l'est pas non plus, les providers ne le
restituant pas au replay) :

- ``user`` : la question du prof ;
- ``assistant`` : un segment de réponse du modèle — quand il demande des
  tools, ``tool_calls`` porte ``[{"id", "name", "arguments"}]`` (données de
  **replay**, ids conservés tels quels — leur format est propre au provider,
  d'où la colonne ``provider`` qui permet au service de replier en texte les
  rounds issus d'un autre provider que la config courante) ; le segment final
  porte en plus ``sources`` et l'usage ;
- ``tool`` : le résultat d'UN appel (``tool_call_id`` l'apparie au
  ``tool_calls`` du segment assistant précédent, ``is_error`` relaie l'échec
  métier au modèle).

Contrat du JSONB ``sources`` (assistant final uniquement, ``{}`` sinon) :
``{"blocks": [uuid-str…], "resources": [uuid-str…]}`` — ids **validés** par le
service (citations hallucinées filtrées), exploités par le front pour les
liens ``oc-block:``/``oc-resource:`` cliquables.

``position`` est posé en service (nombre de messages existants) ; tri stable
``ORDER BY position, id`` (motif ``blocks``).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"


class AIMessage(Base):
    __tablename__ = "ai_messages"
    __table_args__ = (
        CheckConstraint(
            f"role IN ('{ROLE_USER}', '{ROLE_ASSISTANT}', '{ROLE_TOOL}')",
            name="ck_ai_messages_role",
        ),
        # L'appariement est réservé aux tours tool, et obligatoire pour eux.
        CheckConstraint(
            f"(role = '{ROLE_TOOL}') = (tool_call_id IS NOT NULL)",
            name="ck_ai_messages_tool_call_id",
        ),
        CheckConstraint("position >= 0", name="ck_ai_messages_position_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(15))
    position: Mapped[int] = mapped_column(SmallInteger)
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    tool_calls: Mapped[list] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    is_error: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    provider: Mapped[str | None] = mapped_column(String(30))
    sources: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
