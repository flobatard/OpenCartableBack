"""Recherche publique : cours publics et profs cherchables, par texte libre
et facettes.

Règle d'or : seuls les cours ``visibility = 'public'`` remontent, et un prof
ne remonte que si ``searchable`` (opt-in explicite) AND ``public_name`` non
NULL AND au moins un cours public. Tout est en lecture seule, sans identité —
même régime que ``app/public/`` (aucun JWT, aucun oracle : une facette
inconnue renvoie une page vide, jamais une erreur — une URL partagée avec un
id périmé doit rester servable).

Facettes : matière = sous-arbre entier via le ``code`` (chemin slug complet,
préfixe unique — 2 petits selects, pas de CTE récursive) ; niveau = nœud +
enfants directs (l'arbre a 2 profondeurs max). Les descendants sont résolus
en amont puis filtrés par ``IN`` (index inverses des M2M, cf.
``app/models/course.py``). Les requêtes elles-mêmes sont les builders purs de
:mod:`app.search.queries`.

L'ordre des ``execute`` de chaque fonction est un contrat des tests (fausse
session FIFO).
"""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import Storage
from app.courses.queries import block_counts_by_course, taxonomy_names_by_course
from app.models.course import VISIBILITY_PUBLIC, Course
from app.models.education_level import EducationLevel
from app.models.subject import Subject
from app.models.user import CONTEXT_TEACHING, user_subjects
from app.public.service import _course_read
from app.search.queries import (
    courses_count_stmt,
    courses_page_stmt,
    teachers_count_stmt,
    teachers_page_stmt,
    tsquery,
)
from app.search.schemas import (
    PublicTeacherRead,
    SearchCoursesPage,
    SearchTeachersPage,
)
from app.users.service import avatar_url_for


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
    matières, 7) noms de niveaux, 8) comptes de blocs.
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

    tsq = tsquery(q) if q else None
    total = (await db.execute(courses_count_stmt(tsq, subject_ids, level_ids))).scalar_one()
    courses = (
        (await db.execute(courses_page_stmt(tsq, subject_ids, level_ids, limit, offset)))
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

    tsq = tsquery(q) if q else None
    total = (await db.execute(teachers_count_stmt(tsq, subject_ids, level_ids))).scalar_one()
    rows = (
        await db.execute(teachers_page_stmt(tsq, subject_ids, level_ids, limit, offset))
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
