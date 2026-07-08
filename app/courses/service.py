"""Cours du prof : création/liste et structure des blocs (sans leur contenu).

L'ordre des ``execute`` de chaque fonction est stable et documenté : les
tests le rejouent avec une fausse session FIFO (voir tests/test_courses_api.py).
Toute lecture/écriture est scopée au propriétaire (``owner_id``) : un cours
d'autrui est introuvable (404), jamais interdit (403) — on ne divulgue pas
son existence.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import bindparam, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.courses.schemas import (
    BlockCreate,
    BlockOrderUpdate,
    BlockRead,
    BlockUpdate,
    CourseCreate,
    CourseDetailRead,
    CourseRead,
)
from app.models.block import TYPE_EXERCICE, TYPE_LIEN, TYPE_TEXTE, Block
from app.models.course import Course, course_education_levels, course_subjects
from app.models.education_level import EducationLevel
from app.models.subject import Subject
from app.models.user import User


def _dedupe(ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """Dédoublonne en préservant l'ordre de première apparition."""
    return list(dict.fromkeys(ids))


def _invalide(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _introuvable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _contenu_par_defaut(type_: str) -> dict:
    """Contenu JSONB initial d'un bloc, conforme au contrat de block.py.

    Les éditeurs dédiés (scope ultérieur) rempliront ces gabarits ; les
    ``questions[].id`` des exercices seront générés à l'ajout des questions.
    """
    return {
        TYPE_TEXTE: lambda: {"markdown": ""},
        TYPE_EXERCICE: lambda: {"enonce": "", "questions": []},
        TYPE_LIEN: lambda: {"url": "", "titre": "", "fournisseur": None},
    }[type_]()


def _course_read(
    course: Course,
    subject_ids: list[uuid.UUID],
    education_level_ids: list[uuid.UUID],
    block_count: int,
) -> CourseRead:
    return CourseRead(
        id=course.id,
        titre=course.titre,
        description=course.description,
        subject_ids=subject_ids,
        education_level_ids=education_level_ids,
        block_count=block_count,
        created_at=course.created_at,
        updated_at=course.updated_at,
    )


def _block_read(block: Block) -> BlockRead:
    return BlockRead(
        id=block.id,
        position=block.position,
        type=block.type,
        titre=block.titre,
        description=block.description,
        content=block.content,
        resource_id=block.resource_id,
    )


async def _get_owned_course(db: AsyncSession, user: User, course_id: uuid.UUID) -> Course:
    """Charge le cours du prof ; 404 s'il n'existe pas ou appartient à autrui.

    L'instance ORM chargée sert aussi au bump d'``updated_at`` des mutations.
    """
    course = (
        (
            await db.execute(
                select(Course).where(Course.id == course_id, Course.owner_id == user.id)
            )
        )
        .scalars()
        .one_or_none()
    )
    if course is None:
        raise _introuvable("Cours introuvable")
    return course


async def list_courses(db: AsyncSession, user: User) -> list[CourseRead]:
    """Cours du prof, du plus récemment modifié au plus ancien.

    Ordre des execute : 1) cours ; puis, s'il y en a : 2) matières,
    3) niveaux, 4) comptes de blocs. Sans cours, court-circuit après 1).
    """
    courses = (
        (
            await db.execute(
                select(Course)
                .where(Course.owner_id == user.id)
                .order_by(Course.updated_at.desc(), Course.id)
            )
        )
        .scalars()
        .all()
    )
    if not courses:
        return []
    course_ids = [c.id for c in courses]

    matieres: dict[uuid.UUID, list[uuid.UUID]] = {c.id: [] for c in courses}
    lignes_matieres = (
        await db.execute(
            select(course_subjects.c.course_id, course_subjects.c.subject_id)
            .where(course_subjects.c.course_id.in_(course_ids))
            .order_by(course_subjects.c.course_id, course_subjects.c.subject_id)
        )
    ).all()
    for course_id, subject_id in lignes_matieres:
        matieres[course_id].append(subject_id)

    niveaux: dict[uuid.UUID, list[uuid.UUID]] = {c.id: [] for c in courses}
    lignes_niveaux = (
        await db.execute(
            select(
                course_education_levels.c.course_id,
                course_education_levels.c.education_level_id,
            )
            .where(course_education_levels.c.course_id.in_(course_ids))
            .order_by(
                course_education_levels.c.course_id,
                course_education_levels.c.education_level_id,
            )
        )
    ).all()
    for course_id, level_id in lignes_niveaux:
        niveaux[course_id].append(level_id)

    comptes = dict(
        (
            await db.execute(
                select(Block.course_id, func.count())
                .where(Block.course_id.in_(course_ids))
                .group_by(Block.course_id)
            )
        ).all()
    )

    return [_course_read(c, matieres[c.id], niveaux[c.id], comptes.get(c.id, 0)) for c in courses]


async def create_course(db: AsyncSession, user: User, payload: CourseCreate) -> CourseRead:
    """Crée un cours et son classement matières/niveaux.

    Ordre des execute : 1) lookup matières, 2) lookup niveaux (toujours
    exécutés, même sur listes vides, pour garder un ordre FIFO constant),
    3) insert cours (RETURNING des timestamps server_default), puis si non
    vides : 4) insert course_subjects, 5) insert course_education_levels.
    """
    subject_ids = _dedupe(payload.subject_ids)
    education_level_ids = _dedupe(payload.education_level_ids)

    matieres_connues = set(
        (await db.execute(select(Subject.id).where(Subject.id.in_(subject_ids)))).scalars().all()
    )
    inconnues = set(subject_ids) - matieres_connues
    if inconnues:
        raise _invalide(f"Matières inconnues : {sorted(map(str, inconnues))}")

    niveaux_connus = set(
        (
            await db.execute(
                select(EducationLevel.id).where(EducationLevel.id.in_(education_level_ids))
            )
        )
        .scalars()
        .all()
    )
    inconnus = set(education_level_ids) - niveaux_connus
    if inconnus:
        raise _invalide(f"Niveaux d'étude inconnus : {sorted(map(str, inconnus))}")

    course_id = uuid.uuid4()
    created_at, updated_at = (
        await db.execute(
            insert(Course)
            .values(
                id=course_id,
                owner_id=user.id,
                titre=payload.titre,
                description=payload.description,
            )
            .returning(Course.created_at, Course.updated_at)
        )
    ).one()
    if subject_ids:
        await db.execute(
            course_subjects.insert(),
            [{"course_id": course_id, "subject_id": subject_id} for subject_id in subject_ids],
        )
    if education_level_ids:
        await db.execute(
            course_education_levels.insert(),
            [
                {"course_id": course_id, "education_level_id": level_id}
                for level_id in education_level_ids
            ],
        )
    await db.commit()

    return CourseRead(
        id=course_id,
        titre=payload.titre,
        description=payload.description,
        subject_ids=subject_ids,
        education_level_ids=education_level_ids,
        block_count=0,
        created_at=created_at,
        updated_at=updated_at,
    )


async def get_course_detail(
    db: AsyncSession, user: User, course_id: uuid.UUID
) -> CourseDetailRead:
    """Détail d'un cours avec ses blocs ordonnés.

    Ordre des execute : 1) cours (contrôle de propriété), 2) matières,
    3) niveaux, 4) blocs (tri stable ``position, id``).
    """
    course = await _get_owned_course(db, user, course_id)
    subject_ids = list(
        (
            await db.execute(
                select(course_subjects.c.subject_id)
                .where(course_subjects.c.course_id == course.id)
                .order_by(course_subjects.c.subject_id)
            )
        )
        .scalars()
        .all()
    )
    education_level_ids = list(
        (
            await db.execute(
                select(course_education_levels.c.education_level_id)
                .where(course_education_levels.c.course_id == course.id)
                .order_by(course_education_levels.c.education_level_id)
            )
        )
        .scalars()
        .all()
    )
    blocks = (
        (
            await db.execute(
                select(Block).where(Block.course_id == course.id).order_by(Block.position, Block.id)
            )
        )
        .scalars()
        .all()
    )
    base = _course_read(course, subject_ids, education_level_ids, len(blocks))
    return CourseDetailRead(**base.model_dump(), blocks=[_block_read(b) for b in blocks])


async def add_block(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: BlockCreate
) -> BlockRead:
    """Ajoute un bloc au contenu par défaut en fin de cours.

    Ordre des execute : 1) cours (contrôle de propriété), 2) position
    suivante (max + 1, 0 si aucun bloc), 3) insert du bloc. Le cours est
    « touché » (updated_at) pour remonter dans la liste.
    """
    course = await _get_owned_course(db, user, course_id)
    position = (
        (
            await db.execute(
                select(func.coalesce(func.max(Block.position) + 1, 0)).where(
                    Block.course_id == course.id
                )
            )
        )
        .scalars()
        .one()
    )
    block_id = uuid.uuid4()
    content = _contenu_par_defaut(payload.type)
    await db.execute(
        insert(Block).values(
            id=block_id,
            course_id=course.id,
            position=position,
            type=payload.type,
            titre=payload.titre,
            description=payload.description,
            content=content,
            resource_id=None,
        )
    )
    course.updated_at = datetime.now(UTC)
    await db.commit()
    return BlockRead(
        id=block_id,
        position=position,
        type=payload.type,
        titre=payload.titre,
        description=payload.description,
        content=content,
        resource_id=None,
    )


async def delete_block(
    db: AsyncSession, user: User, course_id: uuid.UUID, block_id: uuid.UUID
) -> None:
    """Supprime un bloc du cours ; les positions restantes gardent leurs trous.

    Ordre des execute : 1) cours (contrôle de propriété), 2) existence du
    bloc dans ce cours (select puis delete : la fausse session des tests ne
    simule pas rowcount), 3) delete.
    """
    course = await _get_owned_course(db, user, course_id)
    existe = (
        (
            await db.execute(
                select(Block.id).where(Block.id == block_id, Block.course_id == course.id)
            )
        )
        .scalars()
        .one_or_none()
    )
    if existe is None:
        raise _introuvable("Bloc introuvable")
    await db.execute(delete(Block).where(Block.id == block_id, Block.course_id == course.id))
    course.updated_at = datetime.now(UTC)
    await db.commit()


async def update_block(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    block_id: uuid.UUID,
    payload: BlockUpdate,
) -> BlockRead:
    """Édite un bloc : titre/description (tous types) et/ou contenu (texte seulement).

    Ordre des execute : 1) cours (contrôle de propriété), 2) bloc complet
    (id + course_id) — 404 s'il n'existe pas dans ce cours, 422 si ``content``
    est fourni sur un bloc dont le type n'est pas « texte ». Seuls les champs
    présents dans le payload (``model_fields_set``) sont appliqués ; le
    contenu est remplacé par un NOUVEAU dict (une mutation in-place du JSONB
    ne serait pas détectée par l'ORM).
    """
    course = await _get_owned_course(db, user, course_id)
    block = (
        (
            await db.execute(
                select(Block).where(Block.id == block_id, Block.course_id == course.id)
            )
        )
        .scalars()
        .one_or_none()
    )
    if block is None:
        raise _introuvable("Bloc introuvable")
    if payload.content is not None and block.type != TYPE_TEXTE:
        raise _invalide(f"Seuls les blocs « {TYPE_TEXTE} » sont éditables pour l'instant")
    champs = payload.model_fields_set
    if "titre" in champs:
        block.titre = payload.titre
    if "description" in champs:
        block.description = payload.description
    if payload.content is not None:
        block.content = {"markdown": payload.content.markdown}
    course.updated_at = datetime.now(UTC)
    await db.commit()
    return _block_read(block)


async def reorder_blocks(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: BlockOrderUpdate
) -> None:
    """Réécrit les positions des blocs selon l'ordre fourni (0..n-1).

    Ordre des execute : 1) cours (contrôle de propriété), 2) ids des blocs
    du cours (la liste fournie doit les contenir exactement), 3) update
    executemany des positions (omis si le cours n'a pas de blocs).
    """
    course = await _get_owned_course(db, user, course_id)
    ids_du_cours = set(
        (await db.execute(select(Block.id).where(Block.course_id == course.id))).scalars().all()
    )
    if set(payload.block_ids) != ids_du_cours:
        raise _invalide("block_ids doit contenir exactement les blocs du cours")
    if payload.block_ids:
        # Update sur la Table (pas l'entité ORM) : executemany pur Core,
        # sans passer par le bulk-update-by-pk de l'ORM.
        await db.execute(
            update(Block.__table__)
            .where(Block.__table__.c.id == bindparam("b_id"))
            .values(position=bindparam("b_position")),
            [{"b_id": block_id, "b_position": i} for i, block_id in enumerate(payload.block_ids)],
        )
    course.updated_at = datetime.now(UTC)
    await db.commit()
