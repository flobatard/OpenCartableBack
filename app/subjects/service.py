from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subject import Subject
from app.subjects.schemas import SubjectRead


async def list_subjects(db: AsyncSession) -> Sequence[Subject]:
    stmt = select(Subject).order_by(Subject.profondeur, Subject.position, Subject.nom)
    return (await db.execute(stmt)).scalars().all()


def build_tree(subjects: Sequence[Subject]) -> list[SubjectRead]:
    """Assemble l'arbre en O(n).

    Pré-requis : lignes triées par profondeur croissante, donc chaque parent
    est déjà dans ``nodes`` quand son enfant arrive. Un orphelin (parent
    absent) est toléré et rattaché aux racines plutôt que de planter.
    """
    nodes: dict = {}
    roots: list[SubjectRead] = []
    for s in subjects:
        node = SubjectRead(
            id=s.id,
            parent_id=s.parent_id,
            nom=s.nom,
            code=s.code,
            profondeur=s.profondeur,
            position=s.position,
            children=[],
        )
        nodes[s.id] = node
        parent = nodes.get(s.parent_id) if s.parent_id else None
        (parent.children if parent else roots).append(node)
    return roots


async def get_subject_tree(db: AsyncSession) -> list[SubjectRead]:
    return build_tree(await list_subjects(db))
