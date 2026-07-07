"""seed education levels

Insère la classification pré-remplie des niveaux d'étude (233 nœuds, 12
systèmes scolaires : fr, de, uk, es, it, be, ch, nl, pt, us, ca, ca-qc —
voie générale, hors préélémentaire). Idempotent : les IDs sont des
uuid5 déterministes dérivés du ``code``, donc ON CONFLICT DO NOTHING permet de
rejouer la migration sans doublon. La seule dépendance applicative est
``app.education_levels.seed_data``, module de données pur et append-only
(contrat documenté dans son docstring).

Revision ID: b69a6dbfa0b9
Revises: ffacdb1757a4
Create Date: 2026-07-07 14:12:54.592978

"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from alembic import op
from app.education_levels.seed_data import iter_rows

# revision identifiers, used by Alembic.
revision: str = 'b69a6dbfa0b9'
down_revision: str | None = 'ffacdb1757a4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Table légère : on n'importe pas le modèle ORM dans une migration.
education_levels = sa.table(
    "education_levels",
    sa.column("id", sa.UUID()),
    sa.column("parent_id", sa.UUID()),
    sa.column("nom", sa.String()),
    sa.column("code", sa.String()),
    sa.column("systeme", sa.String()),
    sa.column("cite", sa.SmallInteger()),
    sa.column("age_min", sa.SmallInteger()),
    sa.column("age_max", sa.SmallInteger()),
    sa.column("profondeur", sa.SmallInteger()),
    sa.column("position", sa.SmallInteger()),
)


def upgrade() -> None:
    # iter_rows() yield les parents avant leurs enfants : l'ordre FK est
    # respecté au sein d'un même executemany.
    rows = list(iter_rows())
    op.get_bind().execute(
        pg_insert(education_levels).on_conflict_do_nothing(index_elements=["id"]), rows
    )


def downgrade() -> None:
    # Supprimer les racines suffit : ondelete=CASCADE emporte les sous-arbres.
    # Destructif par nature : d'éventuels nœuds créés à la main sous un cycle
    # seedé seraient emportés aussi.
    root_ids = [r["id"] for r in iter_rows() if r["parent_id"] is None]
    op.get_bind().execute(
        sa.delete(education_levels).where(education_levels.c.id.in_(root_ids))
    )
