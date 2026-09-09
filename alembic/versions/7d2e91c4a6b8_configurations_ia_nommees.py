"""configurations ia nommees

Revision ID: 7d2e91c4a6b8
Revises: bdc4cf611389
Create Date: 2026-09-09 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '7d2e91c4a6b8'
down_revision: Union[str, None] = 'bdc4cf611389'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Migration écrite à la main (motif a1f3c58be201) : autogenerate ne voit ni
# le CHECK ni la copie des données. Le credential unique porté par les
# colonnes users.ai_* devient la première configuration nommée (active) de
# chaque utilisateur qui en avait une, puis les colonnes sont supprimées.
# La copie DOIT précéder les drop_column.

USER_AI_COLUMNS = (
    'ai_provider',
    'ai_model',
    'ai_base_url',
    'ai_api_key_encrypted',
    'ai_encryption_salt',
    'ai_reasoning',
    'ai_reasoning_effort',
)


def upgrade() -> None:
    op.create_table(
        'ai_configurations',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('model', sa.String(length=200), nullable=False),
        sa.Column('base_url', sa.String(length=2000), nullable=True),
        sa.Column('api_key_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('encryption_salt', sa.LargeBinary(), nullable=True),
        sa.Column('reasoning', sa.Boolean(), nullable=True),
        sa.Column('reasoning_effort', sa.String(length=20), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='false', nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_ai_configurations_user_id', 'ai_configurations', ['user_id'], unique=False
    )
    # Au plus une configuration active par utilisateur (index partiel).
    op.create_index(
        'uq_ai_configurations_active',
        'ai_configurations',
        ['user_id'],
        unique=True,
        postgresql_where=sa.text('is_active'),
    )
    op.create_check_constraint(
        'ck_ai_configurations_key_salt',
        'ai_configurations',
        '(api_key_encrypted IS NULL) = (encryption_salt IS NULL)',
    )

    # Copie du credential existant en première configuration ACTIVE, nommée
    # « provider · modèle » (tronqué à 100). gen_random_uuid() est natif
    # depuis PostgreSQL 13.
    op.execute(
        """
        INSERT INTO ai_configurations (
            id, user_id, name, provider, model, base_url, api_key_encrypted,
            encryption_salt, reasoning, reasoning_effort, is_active, created_at, updated_at
        )
        SELECT gen_random_uuid(), id, left(ai_provider || ' · ' || ai_model, 100),
            ai_provider, ai_model, ai_base_url, ai_api_key_encrypted, ai_encryption_salt,
            ai_reasoning, ai_reasoning_effort, true, now(), now()
        FROM users
        WHERE ai_provider IS NOT NULL AND ai_model IS NOT NULL
        """
    )

    op.drop_constraint('ck_users_ai_key_salt', 'users', type_='check')
    op.drop_constraint('ck_users_ai_consistency', 'users', type_='check')
    for column in USER_AI_COLUMNS:
        op.drop_column('users', column)


def downgrade() -> None:
    # Best-effort : seule la configuration ACTIVE de chaque utilisateur
    # revient sur users.ai_* ; les configurations inactives sont PERDUES.
    op.add_column('users', sa.Column('ai_provider', sa.String(length=50), nullable=True))
    op.add_column('users', sa.Column('ai_model', sa.String(length=200), nullable=True))
    op.add_column('users', sa.Column('ai_base_url', sa.String(length=2000), nullable=True))
    op.add_column('users', sa.Column('ai_api_key_encrypted', sa.LargeBinary(), nullable=True))
    op.add_column('users', sa.Column('ai_encryption_salt', sa.LargeBinary(), nullable=True))
    op.add_column('users', sa.Column('ai_reasoning', sa.Boolean(), nullable=True))
    op.add_column('users', sa.Column('ai_reasoning_effort', sa.String(length=20), nullable=True))
    op.execute(
        """
        UPDATE users u SET
            ai_provider = c.provider,
            ai_model = c.model,
            ai_base_url = c.base_url,
            ai_api_key_encrypted = c.api_key_encrypted,
            ai_encryption_salt = c.encryption_salt,
            ai_reasoning = c.reasoning,
            ai_reasoning_effort = c.reasoning_effort
        FROM ai_configurations c
        WHERE c.user_id = u.id AND c.is_active
        """
    )
    op.create_check_constraint(
        'ck_users_ai_consistency',
        'users',
        "(ai_provider IS NULL AND ai_model IS NULL AND ai_base_url IS NULL "
        "AND ai_api_key_encrypted IS NULL AND ai_encryption_salt IS NULL) "
        "OR (ai_provider IS NOT NULL AND ai_model IS NOT NULL)",
    )
    op.create_check_constraint(
        'ck_users_ai_key_salt',
        'users',
        "(ai_api_key_encrypted IS NULL) = (ai_encryption_salt IS NULL)",
    )
    op.drop_index('uq_ai_configurations_active', table_name='ai_configurations')
    op.drop_index('ix_ai_configurations_user_id', table_name='ai_configurations')
    op.drop_table('ai_configurations')
