from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.education_levels.schemas import EducationLevelRead
from app.models.education_level import EducationLevel


async def list_education_levels(db: AsyncSession) -> Sequence[EducationLevel]:
    stmt = select(EducationLevel).order_by(
        EducationLevel.profondeur, EducationLevel.position, EducationLevel.nom
    )
    return (await db.execute(stmt)).scalars().all()


def build_tree(levels: Sequence[EducationLevel]) -> list[EducationLevelRead]:
    """Assemble l'arbre en O(n).

    Pré-requis : lignes triées par profondeur croissante, donc chaque parent
    est déjà dans ``nodes`` quand son enfant arrive. Un orphelin (parent
    absent) est toléré et rattaché aux racines plutôt que de planter.
    """
    nodes: dict = {}
    roots: list[EducationLevelRead] = []
    for lvl in levels:
        node = EducationLevelRead(
            id=lvl.id,
            parent_id=lvl.parent_id,
            nom=lvl.nom,
            code=lvl.code,
            systeme=lvl.systeme,
            cite=lvl.cite,
            age_min=lvl.age_min,
            age_max=lvl.age_max,
            profondeur=lvl.profondeur,
            position=lvl.position,
            children=[],
        )
        nodes[lvl.id] = node
        parent = nodes.get(lvl.parent_id) if lvl.parent_id else None
        (parent.children if parent else roots).append(node)
    return roots


async def get_education_level_tree(db: AsyncSession) -> list[EducationLevelRead]:
    return build_tree(await list_education_levels(db))
