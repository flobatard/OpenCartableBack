"""seed subjects taxonomy

Insère la taxonomie pré-remplie des matières (~475 nœuds, lycée -> master).
Idempotent : les IDs sont des uuid5 déterministes dérivés du ``code``, donc
ON CONFLICT DO NOTHING permet de rejouer la migration sans doublon. La seule
dépendance applicative est ``app.subjects.seed_data``, module de données pur
et append-only (contrat documenté dans son docstring).

Revision ID: d72c77c8bf86
Revises: 5810c28ffd87
Create Date: 2026-07-07 00:00:41.894841

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from alembic import op
from app.subjects.seed_data import iter_rows

# revision identifiers, used by Alembic.
revision: str = 'd72c77c8bf86'
down_revision: str | None = '5810c28ffd87'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Table légère : on n'importe pas le modèle ORM dans une migration.
subjects = sa.table(
    "subjects",
    sa.column("id", sa.UUID()),
    sa.column("parent_id", sa.UUID()),
    sa.column("nom", sa.String()),
    sa.column("code", sa.String()),
    sa.column("profondeur", sa.SmallInteger()),
    sa.column("position", sa.SmallInteger()),
)


def upgrade() -> None:
    # iter_rows() yield les parents avant leurs enfants : l'ordre FK est
    # respecté au sein d'un même executemany.
    rows = list(iter_rows())
    op.get_bind().execute(
        pg_insert(subjects).on_conflict_do_nothing(index_elements=["id"]), rows
    )


def downgrade() -> None:
    # Supprimer les racines suffit : ondelete=CASCADE emporte les sous-arbres.
    # Destructif par nature : d'éventuels nœuds créés à la main sous une
    # discipline seedée seraient emportés aussi.
    root_ids = [r["id"] for r in iter_rows() if r["parent_id"] is None]
    op.get_bind().execute(sa.delete(subjects).where(subjects.c.id.in_(root_ids)))
