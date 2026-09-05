"""Cours du prof : création/liste et structure des blocs (sans leur contenu).

L'ordre des ``execute`` de chaque fonction est stable et documenté : les
tests le rejouent avec une fausse session FIFO (voir tests/test_courses_api.py).
Toute lecture/écriture est scopée au propriétaire (``owner_id``) : un cours
d'autrui est introuvable (404), jamais interdit (403) — on ne divulgue pas
son existence.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import bindparam, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import touch
from app.core.http import invalid, not_found
from app.core.storage import Storage
from app.courses.queries import get_owned_course
from app.courses.schemas import (
    BlockCreate,
    BlockOrderUpdate,
    BlockRead,
    BlockUpdate,
    CourseCreate,
    CourseDetailRead,
    CourseRead,
    DocumentContent,
    ExerciseContent,
    PreviewSettings,
    TextContent,
    VisibilityUpdate,
)
from app.models.block import TYPE_DOCUMENT, TYPE_EXERCISE, TYPE_MODULE, TYPE_TEXT, Block
from app.models.course import (
    VISIBILITY_DRAFT,
    Course,
    course_education_levels,
    course_subjects,
)
from app.models.education_level import EducationLevel
from app.models.module import Module
from app.models.resource import STATUS_AVAILABLE, Resource
from app.models.subject import Subject
from app.models.user import User


def _dedupe(ids: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    """Dédoublonne en préservant l'ordre de première apparition."""
    return list(dict.fromkeys(ids))


def _default_content(type_: str) -> dict:
    """Contenu JSONB initial d'un bloc, conforme au contrat de block.py.

    Les éditeurs dédiés rempliront ces gabarits ; les ``questions[].id``
    des exercices sont générés à l'update (voir ``_exercise_content``).
    """
    return {
        TYPE_TEXT: lambda: {"markdown": ""},
        TYPE_EXERCISE: lambda: {"statement": "", "questions": []},
        TYPE_DOCUMENT: lambda: {"caption": None, "display": "inline"},
        TYPE_MODULE: dict,
    }[type_]()


# Forme de content admise par type de bloc (garde-fou d'update_block).
# Le content d'un bloc « module » n'est pas éditable : son contenu, c'est le
# module pointé par ``module_id``.
_TYPE_BY_CONTENT = {
    TextContent: TYPE_TEXT,
    ExerciseContent: TYPE_EXERCISE,
    DocumentContent: TYPE_DOCUMENT,
}


def _exercise_content(block: Block, content: ExerciseContent) -> dict:
    """Nouveau dict JSONB d'un bloc exercice.

    Les ids fournis sont préservés et doivent exister dans le bloc édité
    (422 sinon — ids stables à vie, cf. block.py) ; les questions sans id
    en reçoivent un frais. Ids sérialisés en ``str`` (un ``uuid.UUID``
    n'est pas JSON-sérialisable par asyncpg). Sémantique remplacement :
    question absente du payload = supprimée.
    """
    existing = {
        q.get("id") for q in block.content.get("questions", []) if isinstance(q, dict)
    }
    unknown = {str(q.id) for q in content.questions if q.id is not None} - existing
    if unknown:
        raise invalid(f"Questions inconnues : {sorted(unknown)}")
    return {
        "statement": content.statement,
        "questions": [
            {
                "id": str(q.id) if q.id is not None else str(uuid.uuid4()),
                "statement": q.statement,
                "type": q.type,
                "expected_answer": q.expected_answer,
            }
            for q in content.questions
        ],
    }


def _course_read(
    course: Course,
    subject_ids: list[uuid.UUID],
    education_level_ids: list[uuid.UUID],
    block_count: int,
) -> CourseRead:
    return CourseRead(
        id=course.id,
        title=course.title,
        description=course.description,
        subject_ids=subject_ids,
        education_level_ids=education_level_ids,
        block_count=block_count,
        preview_settings=course.preview_settings,
        visibility=course.visibility,
        created_at=course.created_at,
        updated_at=course.updated_at,
    )


def _block_read(block: Block) -> BlockRead:
    return BlockRead(
        id=block.id,
        position=block.position,
        type=block.type,
        title=block.title,
        description=block.description,
        content=block.content,
        resource_id=block.resource_id,
        module_id=block.module_id,
    )


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

    subjects: dict[uuid.UUID, list[uuid.UUID]] = {c.id: [] for c in courses}
    subject_rows = (
        await db.execute(
            select(course_subjects.c.course_id, course_subjects.c.subject_id)
            .where(course_subjects.c.course_id.in_(course_ids))
            .order_by(course_subjects.c.course_id, course_subjects.c.subject_id)
        )
    ).all()
    for course_id, subject_id in subject_rows:
        subjects[course_id].append(subject_id)

    levels: dict[uuid.UUID, list[uuid.UUID]] = {c.id: [] for c in courses}
    level_rows = (
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
    for course_id, level_id in level_rows:
        levels[course_id].append(level_id)

    counts = dict(
        (
            await db.execute(
                select(Block.course_id, func.count())
                .where(Block.course_id.in_(course_ids))
                .group_by(Block.course_id)
            )
        ).all()
    )

    return [_course_read(c, subjects[c.id], levels[c.id], counts.get(c.id, 0)) for c in courses]


async def create_course(db: AsyncSession, user: User, payload: CourseCreate) -> CourseRead:
    """Crée un cours et son classement matières/niveaux.

    Ordre des execute : 1) lookup matières, 2) lookup niveaux (toujours
    exécutés, même sur listes vides, pour garder un ordre FIFO constant),
    3) insert cours (RETURNING des timestamps server_default), puis si non
    vides : 4) insert course_subjects, 5) insert course_education_levels.
    """
    subject_ids = _dedupe(payload.subject_ids)
    education_level_ids = _dedupe(payload.education_level_ids)

    known_subjects = set(
        (await db.execute(select(Subject.id).where(Subject.id.in_(subject_ids)))).scalars().all()
    )
    unknown_subjects = set(subject_ids) - known_subjects
    if unknown_subjects:
        raise invalid(f"Matières inconnues : {sorted(map(str, unknown_subjects))}")

    known_levels = set(
        (
            await db.execute(
                select(EducationLevel.id).where(EducationLevel.id.in_(education_level_ids))
            )
        )
        .scalars()
        .all()
    )
    unknown_levels = set(education_level_ids) - known_levels
    if unknown_levels:
        raise invalid(f"Niveaux d'étude inconnus : {sorted(map(str, unknown_levels))}")

    course_id = uuid.uuid4()
    created_at, updated_at = (
        await db.execute(
            insert(Course)
            .values(
                id=course_id,
                owner_id=user.id,
                title=payload.title,
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
        title=payload.title,
        description=payload.description,
        subject_ids=subject_ids,
        education_level_ids=education_level_ids,
        block_count=0,
        preview_settings={},
        visibility=VISIBILITY_DRAFT,
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
    course = await get_owned_course(db, user, course_id)
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


async def delete_course(
    db: AsyncSession, user: User, course_id: uuid.UUID, storage: Storage
) -> None:
    """Supprime un cours du prof ; 404 s'il n'existe pas ou appartient à autrui.

    Ordre des execute : 1) cours (contrôle de propriété), 2) clés S3 des
    ressources du cours, 3) delete. Les blocs, ressources et lignes de classement
    (course_subjects/course_education_levels) partent en cascade via les FK
    ``ondelete=CASCADE`` ; les objets S3 (hors cascade DB) sont supprimés après
    le commit, pour ne pas laisser d'orphelins dans le bucket.
    """
    course = await get_owned_course(db, user, course_id)
    s3_keys = list(
        (await db.execute(select(Resource.s3_key).where(Resource.course_id == course.id)))
        .scalars()
        .all()
    )
    await db.execute(delete(Course).where(Course.id == course.id))
    await db.commit()
    await storage.delete_many(s3_keys)


async def update_preview_settings(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: PreviewSettings
) -> PreviewSettings:
    """Remplace les réglages de preview d'un cours du prof (404 si autrui).

    Ordre des execute : 1) cours (contrôle de propriété). Le JSONB est remplacé
    par un NOUVEAU dict (mutation d'attribut ORM ; une mutation in-place ne
    serait pas détectée). Le cours est « touché » (updated_at) pour remonter
    dans la liste.
    """
    course = await get_owned_course(db, user, course_id)
    course.preview_settings = payload.model_dump(by_alias=True)  # clés camelCase
    touch(course)
    await db.commit()
    return payload


async def update_visibility(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: VisibilityUpdate
) -> VisibilityUpdate:
    """Change le régime d'accès élève d'un cours du prof (404 si autrui).

    Ordre des execute : 1) cours (contrôle de propriété). Le cours est
    « touché » (updated_at) pour remonter dans la liste. Passer en
    ``draft`` suspend les liens de partage sans les toucher : la règle
    vit dans app/public/service.py, vérifiée à chaque accès.
    """
    course = await get_owned_course(db, user, course_id)
    course.visibility = payload.visibility
    touch(course)
    await db.commit()
    return payload


async def add_block(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: BlockCreate
) -> BlockRead:
    """Ajoute un bloc au contenu par défaut en fin de cours.

    Ordre des execute : 1) cours (contrôle de propriété), 2) position
    suivante (max + 1, 0 si aucun bloc), 3) insert du bloc. Le cours est
    « touché » (updated_at) pour remonter dans la liste.
    """
    course = await get_owned_course(db, user, course_id)
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
    content = _default_content(payload.type)
    await db.execute(
        insert(Block).values(
            id=block_id,
            course_id=course.id,
            position=position,
            type=payload.type,
            title=payload.title,
            description=payload.description,
            content=content,
            resource_id=None,
            module_id=None,
        )
    )
    touch(course)
    await db.commit()
    return BlockRead(
        id=block_id,
        position=position,
        type=payload.type,
        title=payload.title,
        description=payload.description,
        content=content,
        resource_id=None,
        module_id=None,
    )


async def delete_block(
    db: AsyncSession, user: User, course_id: uuid.UUID, block_id: uuid.UUID
) -> None:
    """Supprime un bloc du cours ; les positions restantes gardent leurs trous.

    Ordre des execute : 1) cours (contrôle de propriété), 2) bloc dans ce cours
    (select puis delete : la fausse session des tests ne simule pas rowcount),
    3) delete du bloc. Supprimer un bloc ne touche jamais ni ``resources`` ni
    S3 : la ressource éventuellement pointée par un bloc ``document`` reste
    dans la bibliothèque du cours (suppression via ``app/resources/``).
    """
    course = await get_owned_course(db, user, course_id)
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
        raise not_found("Bloc introuvable")
    await db.execute(delete(Block).where(Block.id == block_id, Block.course_id == course.id))
    touch(course)
    await db.commit()


async def update_block(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    block_id: uuid.UUID,
    payload: BlockUpdate,
) -> BlockRead:
    """Édite un bloc : titre/description (tous types), contenu (texte,
    exercice, document), ressource pointée (document) et/ou module pointé
    (module).

    Ordre des execute : 1) cours (contrôle de propriété), 2) bloc complet
    (id + course_id) — 404 s'il n'existe pas dans ce cours —, puis 3)
    UNIQUEMENT si un ``resource_id`` non nul est fourni : la ressource
    (id + course_id du cours), puis 4) UNIQUEMENT si un ``module_id`` non
    nul est fourni : le module (id + course_id du cours) — les deux ne
    coexistent jamais, chacun étant gardé par le type du bloc. 422 si la
    forme du ``content`` fourni ne correspond pas au type du bloc, si une
    question porte un id inconnu du bloc, si ``resource_id`` est fourni sur
    un bloc non-document, si la ressource est inconnue du cours / pas encore
    ``available``, si ``module_id`` est fourni sur un bloc non-module, ou
    si le module est inconnu du cours (pas de statut à vérifier : le code
    vit en base). Toute 422 est levée AVANT la moindre écriture d'attribut
    (pas de mutation partielle). Seuls les champs présents dans le payload
    (``model_fields_set``) sont appliqués ; le contenu est remplacé par un
    NOUVEAU dict (une mutation in-place du JSONB ne serait pas détectée
    par l'ORM).
    """
    course = await get_owned_course(db, user, course_id)
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
        raise not_found("Bloc introuvable")
    fields = payload.model_fields_set
    if "resource_id" in fields:
        if block.type != TYPE_DOCUMENT:
            raise invalid("resource_id ne s'applique qu'aux blocs « document »")
        if payload.resource_id is not None:
            # 422 et non 404 : le bloc ciblé, lui, existe (motif « Matières
            # inconnues ») ; le filtre course_id scelle l'appartenance.
            resource = (
                (
                    await db.execute(
                        select(Resource).where(
                            Resource.id == payload.resource_id,
                            Resource.course_id == course.id,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if resource is None:
                raise invalid("Ressource inconnue")
            if resource.status != STATUS_AVAILABLE:
                raise invalid("Ressource non disponible")
    if "module_id" in fields:
        if block.type != TYPE_MODULE:
            raise invalid("module_id ne s'applique qu'aux blocs « module »")
        if payload.module_id is not None:
            # 422 et non 404 : le bloc ciblé, lui, existe (motif « Matières
            # inconnues ») ; le filtre course_id scelle l'appartenance.
            module = (
                (
                    await db.execute(
                        select(Module).where(
                            Module.id == payload.module_id,
                            Module.course_id == course.id,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if module is None:
                raise invalid("Module inconnu")
    new_content: dict | None = None
    if payload.content is not None:
        expected_type = _TYPE_BY_CONTENT[type(payload.content)]
        if block.type != expected_type:
            raise invalid(
                f"Le contenu fourni correspond à un bloc « {expected_type} », "
                f"pas « {block.type} »"
            )
        if isinstance(payload.content, TextContent):
            new_content = {"markdown": payload.content.markdown}
        elif isinstance(payload.content, ExerciseContent):
            new_content = _exercise_content(block, payload.content)
        else:
            new_content = {
                "caption": payload.content.caption,
                "display": payload.content.display,
            }
    if "title" in fields:
        block.title = payload.title
    if "description" in fields:
        block.description = payload.description
    if "resource_id" in fields:
        block.resource_id = payload.resource_id
    if "module_id" in fields:
        block.module_id = payload.module_id
    if new_content is not None:
        block.content = new_content
    touch(course)
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
    course = await get_owned_course(db, user, course_id)
    course_block_ids = set(
        (await db.execute(select(Block.id).where(Block.course_id == course.id))).scalars().all()
    )
    if set(payload.block_ids) != course_block_ids:
        raise invalid("block_ids doit contenir exactement les blocs du cours")
    if payload.block_ids:
        # Update sur la Table (pas l'entité ORM) : executemany pur Core,
        # sans passer par le bulk-update-by-pk de l'ORM.
        await db.execute(
            update(Block.__table__)
            .where(Block.__table__.c.id == bindparam("b_id"))
            .values(position=bindparam("b_position")),
            [{"b_id": block_id, "b_position": i} for i, block_id in enumerate(payload.block_ids)],
        )
    touch(course)
    await db.commit()
