"""separate blocks and resources

Les blocs `lien`/`ressource` disparaissent au profit de `document` (pont vers
une ressource de la bibliothèque du cours, FK CASCADE conservée : supprimer la
ressource supprime les blocs document qui la pointent) et `module`
(placeholder J4) ; la table `modules` (spécialisation d'une ressource, restée
orpheline) est supprimée avec le type de ressource `module`.

Migration largement MANUELLE : autogenerate ne détecte pas la modification
des CheckConstraint et ne génère jamais de migration de données — les DELETE
purgent les lignes qui violeraient les nouveaux CHECK (données de test,
décision actée). downgrade() recrée le schéma d'avant mais ne restaure pas
ces données.

Revision ID: 3a9e7460d86e
Revises: 6c3c07ee9ea3
Create Date: 2026-07-09 15:38:40.610631

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3a9e7460d86e'
down_revision: Union[str, None] = '6c3c07ee9ea3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Purge des données devenues invalides AVANT de resserrer les CHECK
    # (create_check_constraint échouerait sur des lignes violantes).
    op.execute("DELETE FROM blocks WHERE type IN ('lien', 'ressource')")
    op.drop_table('modules')
    # Pas de purge S3 ici (assumé : données de test).
    op.execute("DELETE FROM resources WHERE type = 'module'")

    op.drop_constraint('ck_resources_type', 'resources', type_='check')
    op.create_check_constraint(
        'ck_resources_type', 'resources',
        "type IN ('document', 'image', 'audio', 'video')",
    )

    op.drop_constraint('ck_blocks_type', 'blocks', type_='check')
    op.create_check_constraint(
        'ck_blocks_type', 'blocks',
        "type IN ('texte', 'exercice', 'document', 'module')",
    )

    # Seuls les blocs « document » peuvent porter une FK resource, désormais
    # nullable (un document naît vide). La FK elle-même ne change pas
    # (ondelete CASCADE conservé).
    op.drop_constraint('ck_blocks_ressource_coherence', 'blocks', type_='check')
    op.create_check_constraint(
        'ck_blocks_document_coherence', 'blocks',
        "resource_id IS NULL OR type = 'document'",
    )


def downgrade() -> None:
    # Les blocs document/module violeraient les anciens CHECK stricts.
    op.execute("DELETE FROM blocks WHERE type IN ('document', 'module')")

    op.drop_constraint('ck_blocks_document_coherence', 'blocks', type_='check')
    op.create_check_constraint(
        'ck_blocks_ressource_coherence', 'blocks',
        "(type = 'ressource') = (resource_id IS NOT NULL)",
    )

    op.drop_constraint('ck_blocks_type', 'blocks', type_='check')
    op.create_check_constraint(
        'ck_blocks_type', 'blocks',
        "type IN ('texte', 'exercice', 'ressource', 'lien')",
    )

    op.drop_constraint('ck_resources_type', 'resources', type_='check')
    op.create_check_constraint(
        'ck_resources_type', 'resources',
        "type IN ('document', 'image', 'audio', 'video', 'module')",
    )

    op.create_table('modules',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('resource_id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('version', sa.SMALLINT(), server_default=sa.text("'1'::smallint"), autoincrement=False, nullable=False),
    sa.Column('entrypoint', sa.VARCHAR(length=255), server_default=sa.text("'index.html'::character varying"), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('updated_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.CheckConstraint('version >= 1', name='ck_modules_version_positive'),
    sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], name='modules_resource_id_fkey', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name='modules_pkey'),
    sa.UniqueConstraint('resource_id', name='uq_modules_resource_id', postgresql_include=[], postgresql_nulls_not_distinct=False)
    )
    # ### end Alembic commands ###
