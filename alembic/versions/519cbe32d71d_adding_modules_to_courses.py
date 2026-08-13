"""Adding modules to courses

Revision ID: 519cbe32d71d
Revises: 7c24ca14606b
Create Date: 2026-08-13 20:13:21.734725

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '519cbe32d71d'
down_revision: Union[str, None] = '7c24ca14606b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('modules',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('course_id', sa.UUID(), nullable=False),
    sa.Column('titre', sa.String(length=255), nullable=False),
    sa.Column('html', sa.Text(), server_default='', nullable=False),
    sa.Column('css', sa.Text(), server_default='', nullable=False),
    sa.Column('js', sa.Text(), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_modules_course_id'), 'modules', ['course_id'], unique=False)
    op.add_column('blocks', sa.Column('module_id', sa.UUID(), nullable=True))
    op.create_index(op.f('ix_blocks_module_id'), 'blocks', ['module_id'], unique=False)
    op.create_foreign_key(None, 'blocks', 'modules', ['module_id'], ['id'], ondelete='CASCADE')
    # Ajout manuel : autogenerate ne voit pas les CheckConstraint (piège
    # documenté dans CLAUDE.md — même motif que ck_blocks_document_coherence,
    # créé à la main dans 3a9e7460d86e).
    op.create_check_constraint(
        'ck_blocks_module_coherence',
        'blocks',
        "module_id IS NULL OR type = 'module'",
    )


def downgrade() -> None:
    op.drop_constraint('ck_blocks_module_coherence', 'blocks', type_='check')
    # La FK a été créée sans nom : Postgres l'a nommée blocks_module_id_fkey
    # (même convention que blocks_resource_id_fkey, cf. CLAUDE.md).
    op.drop_constraint('blocks_module_id_fkey', 'blocks', type_='foreignkey')
    op.drop_index(op.f('ix_blocks_module_id'), table_name='blocks')
    op.drop_column('blocks', 'module_id')
    op.drop_index(op.f('ix_modules_course_id'), table_name='modules')
    op.drop_table('modules')
