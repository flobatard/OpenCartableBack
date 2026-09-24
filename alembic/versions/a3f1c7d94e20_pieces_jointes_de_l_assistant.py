"""pièces jointes de l'assistant

Revision ID: a3f1c7d94e20
Revises: bf482c1b00fb
Create Date: 2026-09-24 10:35:00.000000

Table `ai_attachments` : fichiers joints par le professeur à un message de
l'assistant. Métadonnées seules, le binaire vit sur S3.

Les quatre CHECK sont posés À LA MAIN : l'autogenerate d'Alembic ne voit pas
les `CheckConstraint` d'un modèle.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3f1c7d94e20'
down_revision: Union[str, None] = 'bf482c1b00fb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'ai_attachments',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('course_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('owner_id', postgresql.UUID(as_uuid=True), nullable=False),
        # Nullables jusqu'à l'envoi du message qui les porte : le front peut
        # joindre un fichier alors que la conversation est un brouillon sans id.
        sa.Column('conversation_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('message_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('s3_key', sa.String(length=1024), nullable=False),
        sa.Column('original_name', sa.String(length=255), nullable=False),
        sa.Column('mime', sa.String(length=255), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column(
            'status', sa.String(length=15), server_default='pending', nullable=False
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['ai_conversations.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(['message_id'], ['ai_messages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('s3_key', name='uq_ai_attachments_s3_key'),
    )
    op.create_index(
        op.f('ix_ai_attachments_course_id'), 'ai_attachments', ['course_id'], unique=False
    )
    op.create_index(
        op.f('ix_ai_attachments_owner_id'), 'ai_attachments', ['owner_id'], unique=False
    )
    op.create_index(
        op.f('ix_ai_attachments_conversation_id'),
        'ai_attachments',
        ['conversation_id'],
        unique=False,
    )
    # Invisibles de l'autogenerate : posés à la main.
    op.create_check_constraint(
        'ck_ai_attachments_kind',
        'ai_attachments',
        "kind IN ('image', 'pdf', 'text', 'office')",
    )
    op.create_check_constraint(
        'ck_ai_attachments_status',
        'ai_attachments',
        "status IN ('pending', 'available')",
    )
    op.create_check_constraint(
        'ck_ai_attachments_size_positive', 'ai_attachments', 'size >= 0'
    )
    # Rattachée à un message ⇒ rattachée à sa conversation, et confirmée.
    op.create_check_constraint(
        'ck_ai_attachments_binding',
        'ai_attachments',
        "message_id IS NULL OR (conversation_id IS NOT NULL AND status = 'available')",
    )


def downgrade() -> None:
    # Index et contraintes partent avec la table.
    op.drop_table('ai_attachments')
