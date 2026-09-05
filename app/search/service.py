"""Recherche publique (J3) : FTS Postgres sur les cours publics et les profs.

Règle d'or : seuls les cours ``visibility = 'public'`` remontent, et un prof
ne remonte que si ``searchable`` (opt-in explicite) AND ``public_name`` non
NULL AND au moins un cours public. Tout est en lecture seule, sans identité —
même régime que ``app/public/`` (aucun JWT, aucun oracle : une facette
inconnue renvoie une page vide, jamais une erreur — une URL partagée avec un
id périmé doit rester servable).

FTS : config ``french_unaccent`` (extension ``unaccent``, migration J3),
``websearch_to_tsquery`` (syntaxe utilisateur libre, injection-safe par
construction). Les vecteurs sont **stockés** (``courses.search_vector`` poids
titre A / description B ; ``blocks.search_vector`` poids titre B / contenu C,
sans jamais ``expected_answer``) et maintenus par triggers — voir les
docstrings de ``app/models/{course,block}.py``. Les deux vecteurs sont
combinés à la requête (cours OU un de ses blocs), pas consolidés. Le vecteur
des profs est calculé à la volée (table minuscule, texte dépendant des M2M).

Facettes : matière = sous-arbre entier via le ``code`` (chemin slug complet,
préfixe unique — 2 petits selects, pas de CTE récursive) ; niveau = nœud +
enfants directs (l'arbre a 2 profondeurs max). Les descendants sont résolus
en amont puis filtrés par ``IN`` (les index inverses des M2M servent ça,
cf. ``app/models/course.py``).

L'ordre des ``execute`` de chaque fonction est stable et rejoué par une
fausse session FIFO (tests/test_search_api.py) ; les builders ``_*_stmt``
sont purs et testés sur leur SQL compilé (seul moyen de valider la FTS sans
Postgres).
"""

import uuid

from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import Storage
from app.courses.queries import block_counts_by_course, taxonomy_names_by_course
from app.models.block import Block
from app.models.course import VISIBILITY_PUBLIC, Course, course_education_levels, course_subjects
from app.models.education_level import EducationLevel
from app.models.subject import Subject
from app.models.user import (
    CONTEXT_TEACHING,
    User,
    user_education_levels,
    user_subjects,
)
from app.public.service import _course_read
from app.search.schemas import (
    PublicTeacherRead,
    SearchCoursesPage,
    SearchTeachersPage,
)
from app.users.service import avatar_url_for

# Config FTS créée par la migration J3 (schéma public, résolue via search_path).
FTS_CONFIG = "french_unaccent"


def _tsquery(q: str):
    """``websearch_to_tsquery`` : syntaxe libre (guillemets, OR, -), jamais
    d'erreur de parsing, et ``q`` voyage en bind param — injection-safe."""
    return func.websearch_to_tsquery(FTS_CONFIG, q)


# ---------------------------------------------------------------------------
# Builders purs (testables sur leur SQL compilé)
# ---------------------------------------------------------------------------


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


def _courses_count_stmt(tsq, subject_ids, level_ids) -> Select:
    return (
        select(func.count())
        .select_from(Course)
        .where(*_courses_filters(tsq, subject_ids, level_ids))
    )


def _courses_page_stmt(tsq, subject_ids, level_ids, limit: int, offset: int) -> Select:
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


def _teachers_count_stmt(tsq, subject_ids, level_ids) -> Select:
    return (
        select(func.count())
        .select_from(User)
        .where(*_teachers_filters(tsq, subject_ids, level_ids))
    )


def _teachers_page_stmt(tsq, subject_ids, level_ids, limit: int, offset: int) -> Select:
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


# ---------------------------------------------------------------------------
# Résolution des facettes (sous-arbres → listes d'ids, None = pas de filtre)
# ---------------------------------------------------------------------------


async def _subject_filter_ids(
    db: AsyncSession, subject_id: uuid.UUID
) -> list[uuid.UUID] | None:
    """Ids du sous-arbre de la matière (elle comprise), ``None`` si inconnue.

    Le ``code`` est un chemin slug complet unique (``mathematiques.algebre``…),
    en ``[a-z0-9.-]`` — le LIKE de préfixe n'a rien à échapper. Ordre des
    execute : 1) code de la matière, 2) ids du sous-arbre.
    """
    code = (
        (await db.execute(select(Subject.code).where(Subject.id == subject_id)))
        .scalars()
        .one_or_none()
    )
    if code is None:
        return None
    ids = (
        (
            await db.execute(
                select(Subject.id).where(
                    or_(Subject.code == code, Subject.code.like(code + ".%"))
                )
            )
        )
        .scalars()
        .all()
    )
    return list(ids)


async def _level_filter_ids(
    db: AsyncSession, education_level_id: uuid.UUID
) -> list[uuid.UUID] | None:
    """Ids du niveau et de ses enfants directs (2 profondeurs max — pas de
    récursion possible), ``None`` si inconnu. Ordre des execute : 1) ids."""
    ids = (
        (
            await db.execute(
                select(EducationLevel.id).where(
                    or_(
                        EducationLevel.id == education_level_id,
                        EducationLevel.parent_id == education_level_id,
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    return list(ids) or None


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


async def search_courses(
    db: AsyncSession,
    *,
    q: str | None,
    subject_id: uuid.UUID | None,
    education_level_id: uuid.UUID | None,
    limit: int,
    offset: int,
) -> SearchCoursesPage:
    """Page de cours publics correspondant au texte libre et aux facettes.

    Ordre des execute : si ``subject_id`` : 1) code, 2) ids du sous-arbre —
    matière inconnue ⇒ page vide immédiate, aucun autre execute ; si
    ``education_level_id`` : 3) ids — inconnu ⇒ page vide ; puis 4) count,
    5) page (cours triés rank/updated_at) ; et si items : 6) noms de
    matières, 7) noms de niveaux, 8) comptes de blocs (assemblage identique
    à ``list_public_courses_by_professor``).
    """
    q = (q or "").strip() or None  # websearch_to_tsquery('') ne matche rien
    subject_ids: list[uuid.UUID] | None = None
    level_ids: list[uuid.UUID] | None = None
    if subject_id is not None:
        subject_ids = await _subject_filter_ids(db, subject_id)
        if subject_ids is None:
            return SearchCoursesPage(items=[], total=0, limit=limit, offset=offset)
    if education_level_id is not None:
        level_ids = await _level_filter_ids(db, education_level_id)
        if level_ids is None:
            return SearchCoursesPage(items=[], total=0, limit=limit, offset=offset)

    tsq = _tsquery(q) if q else None
    total = (
        await db.execute(_courses_count_stmt(tsq, subject_ids, level_ids))
    ).scalar_one()
    courses = (
        (await db.execute(_courses_page_stmt(tsq, subject_ids, level_ids, limit, offset)))
        .scalars()
        .all()
    )
    if not courses:
        return SearchCoursesPage(items=[], total=total, limit=limit, offset=offset)

    course_ids = [c.id for c in courses]
    subjects, levels = await taxonomy_names_by_course(db, course_ids)
    counts = await block_counts_by_course(db, course_ids)
    return SearchCoursesPage(
        items=[
            _course_read(c, subjects[c.id], levels[c.id], counts.get(c.id, 0))
            for c in courses
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


async def search_teachers(
    db: AsyncSession,
    *,
    q: str | None,
    subject_id: uuid.UUID | None,
    education_level_id: uuid.UUID | None,
    limit: int,
    offset: int,
    storage: Storage,
) -> SearchTeachersPage:
    """Page de profs cherchables (voir critères en tête de module).

    Ordre des execute : résolution des facettes comme ``search_courses``
    (inconnue ⇒ page vide immédiate) ; puis count, page (id + public_name +
    colonnes avatar, tri rank/nom) ; et si items : noms des matières
    enseignées, comptes de cours publics. L'``avatar_url`` est présignée
    localement par item (aucune I/O).
    """
    q = (q or "").strip() or None
    subject_ids: list[uuid.UUID] | None = None
    level_ids: list[uuid.UUID] | None = None
    if subject_id is not None:
        subject_ids = await _subject_filter_ids(db, subject_id)
        if subject_ids is None:
            return SearchTeachersPage(items=[], total=0, limit=limit, offset=offset)
    if education_level_id is not None:
        level_ids = await _level_filter_ids(db, education_level_id)
        if level_ids is None:
            return SearchTeachersPage(items=[], total=0, limit=limit, offset=offset)

    tsq = _tsquery(q) if q else None
    total = (
        await db.execute(_teachers_count_stmt(tsq, subject_ids, level_ids))
    ).scalar_one()
    rows = (
        await db.execute(_teachers_page_stmt(tsq, subject_ids, level_ids, limit, offset))
    ).all()
    if not rows:
        return SearchTeachersPage(items=[], total=total, limit=limit, offset=offset)

    user_ids = [row[0] for row in rows]
    subjects: dict[uuid.UUID, list[str]] = {uid: [] for uid in user_ids}
    subject_rows = (
        await db.execute(
            select(user_subjects.c.user_id, Subject.name)
            .select_from(
                user_subjects.join(Subject, Subject.id == user_subjects.c.subject_id)
            )
            .where(
                user_subjects.c.user_id.in_(user_ids),
                user_subjects.c.context == CONTEXT_TEACHING,
            )
            .order_by(user_subjects.c.user_id, Subject.name)
        )
    ).all()
    for user_id, name in subject_rows:
        subjects[user_id].append(name)

    counts = dict(
        (
            await db.execute(
                select(Course.owner_id, func.count())
                .where(
                    Course.owner_id.in_(user_ids),
                    Course.visibility == VISIBILITY_PUBLIC,
                )
                .group_by(Course.owner_id)
            )
        ).all()
    )
    return SearchTeachersPage(
        items=[
            PublicTeacherRead(
                id=user_id,
                public_name=public_name,
                avatar_url=avatar_url_for(avatar_s3_key, avatar_status, storage),
                subjects=subjects[user_id],
                public_course_count=counts.get(user_id, 0),
            )
            for user_id, public_name, avatar_s3_key, avatar_status in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
