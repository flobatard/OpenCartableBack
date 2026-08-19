"""avatar utilisateur

Revision ID: c9d4f4607177
Revises: feb3a436271b
Create Date: 2026-08-19 12:13:40.984051

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9d4f4607177'
down_revision: Union[str, None] = 'feb3a436271b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



# Le CHECK est posé à la main : autogenerate ne détecte pas les
# CheckConstraint (piège documenté dans CLAUDE.md). Les index GIN de la FTS,
# eux, sont désormais déclarés dans les modèles (course.py/block.py) — plus
# de drop_index parasite à retirer ici.


def upgrade() -> None:
    op.add_column('users', sa.Column('avatar_s3_key', sa.String(length=1024), nullable=True))
    op.add_column('users', sa.Column('avatar_mime', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('avatar_statut', sa.String(length=20), nullable=True))
    op.create_check_constraint(
        'ck_users_avatar_coherence',
        'users',
        "(avatar_s3_key IS NULL AND avatar_mime IS NULL AND avatar_statut IS NULL) "
        "OR (avatar_s3_key IS NOT NULL AND avatar_mime IS NOT NULL "
        "AND avatar_statut IN ('en_attente', 'disponible'))",
    )


def downgrade() -> None:
    op.drop_constraint('ck_users_avatar_coherence', 'users', type_='check')
    op.drop_column('users', 'avatar_statut')
    op.drop_column('users', 'avatar_mime')
    op.drop_column('users', 'avatar_s3_key')

