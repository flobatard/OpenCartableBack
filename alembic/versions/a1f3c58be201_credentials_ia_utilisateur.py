"""credentials ia utilisateur

Revision ID: a1f3c58be201
Revises: c9d4f4607177
Create Date: 2026-08-30 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f3c58be201'
down_revision: Union[str, None] = 'c9d4f4607177'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Les CHECKs sont posés à la main : autogenerate ne détecte pas les
# CheckConstraint (piège documenté dans CLAUDE.md). Schéma de chiffrement de
# ai_api_key_chiffree / ai_chiffrement_sel : voir app/core/crypto.py (v1).


def upgrade() -> None:
    op.add_column('users', sa.Column('ai_provider', sa.String(length=50), nullable=True))
    op.add_column('users', sa.Column('ai_model', sa.String(length=200), nullable=True))
    op.add_column('users', sa.Column('ai_base_url', sa.String(length=2000), nullable=True))
    op.add_column('users', sa.Column('ai_api_key_chiffree', sa.LargeBinary(), nullable=True))
    op.add_column('users', sa.Column('ai_chiffrement_sel', sa.LargeBinary(), nullable=True))
    op.create_check_constraint(
        'ck_users_ai_coherence',
        'users',
        "(ai_provider IS NULL AND ai_model IS NULL AND ai_base_url IS NULL "
        "AND ai_api_key_chiffree IS NULL AND ai_chiffrement_sel IS NULL) "
        "OR (ai_provider IS NOT NULL AND ai_model IS NOT NULL)",
    )
    op.create_check_constraint(
        'ck_users_ai_cle_sel',
        'users',
        "(ai_api_key_chiffree IS NULL) = (ai_chiffrement_sel IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint('ck_users_ai_cle_sel', 'users', type_='check')
    op.drop_constraint('ck_users_ai_coherence', 'users', type_='check')
    op.drop_column('users', 'ai_chiffrement_sel')
    op.drop_column('users', 'ai_api_key_chiffree')
    op.drop_column('users', 'ai_base_url')
    op.drop_column('users', 'ai_model')
    op.drop_column('users', 'ai_provider')
