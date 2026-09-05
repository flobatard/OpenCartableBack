"""Builders purs des requêtes de recherche (FTS Postgres), testables sur leur
SQL compilé — seul moyen de valider la FTS sans Postgres
(``stmt.compile(dialect=postgresql.dialect())``).

Config ``french_unaccent`` (extension ``unaccent`` + copie de ``french``,
créées par la migration FTS), ``websearch_to_tsquery`` (syntaxe utilisateur
libre, injection-safe par construction). Les vecteurs des cours et des blocs
sont **stockés** et maintenus par triggers (docstrings de
``app/models/{course,block}.py`` — jamais ``expected_answer``) et combinés à
la requête (cours OU un de ses blocs), pas consolidés ; le vecteur des profs
est calculé à la volée (table minuscule).
"""

from sqlalchemy import Select, exists, func, or_, select

from app.models.block import Block
from app.models.course import VISIBILITY_PUBLIC, Course, course_education_levels, course_subjects
from app.models.subject import Subject
from app.models.user import (
    CONTEXT_TEACHING,
    User,
    user_education_levels,
    user_subjects,
)

# Config FTS (schéma public, résolue via search_path).
FTS_CONFIG = "french_unaccent"


def tsquery(q: str):
    """``websearch_to_tsquery`` : syntaxe libre (guillemets, OR, -), jamais
    d'erreur de parsing, et ``q`` voyage en bind param — injection-safe."""
    return func.websearch_to_tsquery(FTS_CONFIG, q)


# --- Cours -----------------------------------------------------------------


def _courses_filters(tsq, subject_ids, level_ids) -> list:
    filters: list = [Course.visibility == VISIBILITY_PUBLIC]
    if subject_ids is not None:
        filters.append(
            exists(
                select(1).where(
                    course_subjects.c.course_id == Course.id,
                    course_subjects.c.subject_id.in_(subject_ids),
                )
            )
        )
    if level_ids is not None:
        filters.append(
            exists(
                select(1).where(
                    course_education_levels.c.course_id == Course.id,
                    course_education_levels.c.education_level_id.in_(level_ids),
                )
            )
        )
    if tsq is not None:
        blocks_match = exists(
            select(1).where(
                Block.course_id == Course.id,
                Block.search_vector.bool_op("@@")(tsq),
            )
        )
        filters.append(or_(Course.search_vector.bool_op("@@")(tsq), blocks_match))
    return filters


def _courses_rank(tsq):
    """Pertinence d'un cours : son propre vecteur + la meilleure de ses
    correspondances de blocs, pondérée 0.5 (heuristique de départ)."""
    block_rank = (
        select(func.max(func.ts_rank(Block.search_vector, tsq)))
        .where(
            Block.course_id == Course.id,
            Block.search_vector.bool_op("@@")(tsq),
        )
        .scalar_subquery()
    )
    return func.coalesce(func.ts_rank(Course.search_vector, tsq), 0.0) + (
        0.5 * func.coalesce(block_rank, 0.0)
    )


def courses_count_stmt(tsq, subject_ids, level_ids) -> Select:
    return (
        select(func.count())
        .select_from(Course)
        .where(*_courses_filters(tsq, subject_ids, level_ids))
    )


def courses_page_stmt(tsq, subject_ids, level_ids, limit: int, offset: int) -> Select:
    stmt = select(Course).where(*_courses_filters(tsq, subject_ids, level_ids))
    if tsq is not None:
        stmt = stmt.order_by(
            _courses_rank(tsq).desc(), Course.updated_at.desc(), Course.id
        )
    else:
        # Sans texte libre, la recherche est un catalogue : du plus récent
        # au plus ancien (tri de référence du repo).
        stmt = stmt.order_by(Course.updated_at.desc(), Course.id)
    return stmt.limit(limit).offset(offset)


# --- Professeurs -----------------------------------------------------------


def _teachers_vector():
    """tsvector du prof, calculé à la volée : nom public + noms des matières
    qu'il déclare enseigner (profil « teaching »)."""
    subjects_agg = (
        select(func.string_agg(Subject.name, " "))
        .select_from(
            user_subjects.join(Subject, Subject.id == user_subjects.c.subject_id)
        )
        .where(
            user_subjects.c.user_id == User.id,
            user_subjects.c.context == CONTEXT_TEACHING,
        )
        .scalar_subquery()
    )
    return func.to_tsvector(FTS_CONFIG, func.concat_ws(" ", User.public_name, subjects_agg))


def _teachers_filters(tsq, subject_ids, level_ids) -> list:
    filters: list = [
        User.searchable.is_(True),
        User.public_name.is_not(None),
        # Jamais de profil sans contenu : au moins un cours public.
        exists(
            select(1).where(
                Course.owner_id == User.id,
                Course.visibility == VISIBILITY_PUBLIC,
            )
        ),
    ]
    if subject_ids is not None:
        filters.append(
            exists(
                select(1).where(
                    user_subjects.c.user_id == User.id,
                    user_subjects.c.context == CONTEXT_TEACHING,
                    user_subjects.c.subject_id.in_(subject_ids),
                )
            )
        )
    if level_ids is not None:
        filters.append(
            exists(
                select(1).where(
                    user_education_levels.c.user_id == User.id,
                    user_education_levels.c.context == CONTEXT_TEACHING,
                    user_education_levels.c.education_level_id.in_(level_ids),
                )
            )
        )
    if tsq is not None:
        filters.append(_teachers_vector().bool_op("@@")(tsq))
    return filters


def teachers_count_stmt(tsq, subject_ids, level_ids) -> Select:
    return (
        select(func.count())
        .select_from(User)
        .where(*_teachers_filters(tsq, subject_ids, level_ids))
    )


def teachers_page_stmt(tsq, subject_ids, level_ids, limit: int, offset: int) -> Select:
    # avatar_s3_key/avatar_status ne sortent jamais de l'API : ils servent à
    # minter l'avatar_url présignée (le mime est inutile au presign GET).
    stmt = select(
        User.id, User.public_name, User.avatar_s3_key, User.avatar_status
    ).where(*_teachers_filters(tsq, subject_ids, level_ids))
    if tsq is not None:
        stmt = stmt.order_by(
            func.ts_rank(_teachers_vector(), tsq).desc(), User.public_name, User.id
        )
    else:
        stmt = stmt.order_by(User.public_name, User.id)
    return stmt.limit(limit).offset(offset)
