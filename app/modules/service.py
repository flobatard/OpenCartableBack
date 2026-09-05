"""Bibliothèque de modules interactifs d'un cours : CRUD pur BDD.

Le code HTML/CSS/JS vit en base (pas de bundle S3) : aucune dépendance
storage ici, une suppression n'a rien à purger. Les modules sont
**indépendants des blocs** : supprimer un module supprime les blocs
``module`` qui le pointent (FK ``CASCADE`` — un bloc pointeur sans son module
n'a pas de sens, comme pour les documents). Comme
:mod:`app.courses.service`, l'ordre des ``execute`` de chaque fonction est
stable et rejoué par une fausse session FIFO (tests/test_modules_api.py), et
tout est scopé au propriétaire du cours (introuvable → 404, jamais 403).
"""

import uuid

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import touch
from app.core.http import not_found
from app.courses.queries import get_owned_course
from app.models.course import Course
from app.models.module import Module
from app.models.user import User
from app.modules.schemas import ModuleCreate, ModuleRead, ModuleSummary, ModuleUpdate


def _module_summary(module: Module) -> ModuleSummary:
    return ModuleSummary(
        id=module.id,
        title=module.title,
        created_at=module.created_at,
        updated_at=module.updated_at,
    )


def _module_read(module: Module) -> ModuleRead:
    return ModuleRead(
        id=module.id,
        title=module.title,
        html=module.html,
        css=module.css,
        js=module.js,
        created_at=module.created_at,
        updated_at=module.updated_at,
    )


async def _get_module(db: AsyncSession, course: Course, module_id: uuid.UUID) -> Module:
    """Charge un module scopé à ce cours ; 404 sinon."""
    module = (
        (
            await db.execute(
                select(Module).where(
                    Module.id == module_id, Module.course_id == course.id
                )
            )
        )
        .scalars()
        .one_or_none()
    )
    if module is None:
        raise not_found("Module introuvable")
    return module


async def list_modules(
    db: AsyncSession, user: User, course_id: uuid.UUID
) -> list[ModuleSummary]:
    """Modules du cours, du plus récent au plus ancien, sans leur code.

    Ordre des execute : 1) cours (contrôle de propriété), 2) modules
    (tri stable ``created_at desc, id``). Lecture seule : pas de commit.
    """
    course = await get_owned_course(db, user, course_id)
    modules = (
        (
            await db.execute(
                select(Module)
                .where(Module.course_id == course.id)
                .order_by(Module.created_at.desc(), Module.id)
            )
        )
        .scalars()
        .all()
    )
    return [_module_summary(m) for m in modules]


async def create_module(
    db: AsyncSession, user: User, course_id: uuid.UUID, payload: ModuleCreate
) -> ModuleRead:
    """Crée un module (code éventuellement vide, rempli dans l'éditeur).

    Ordre des execute : 1) cours (contrôle de propriété), 2) insert module
    (RETURNING les timestamps générés par Postgres — motif ``create_course``).
    """
    course = await get_owned_course(db, user, course_id)
    module_id = uuid.uuid4()
    created_at, updated_at = (
        await db.execute(
            insert(Module)
            .values(
                id=module_id,
                course_id=course.id,
                title=payload.title,
                html=payload.html,
                css=payload.css,
                js=payload.js,
            )
            .returning(Module.created_at, Module.updated_at)
        )
    ).one()
    touch(course)
    await db.commit()
    return ModuleRead(
        id=module_id,
        title=payload.title,
        html=payload.html,
        css=payload.css,
        js=payload.js,
        created_at=created_at,
        updated_at=updated_at,
    )


async def get_module(
    db: AsyncSession, user: User, course_id: uuid.UUID, module_id: uuid.UUID
) -> ModuleRead:
    """Détail d'un module, code inclus (éditeur + exécution sandbox).

    Ordre des execute : 1) cours (contrôle de propriété), 2) module (scopé
    cours). Lecture seule : pas de commit.
    """
    course = await get_owned_course(db, user, course_id)
    module = await _get_module(db, course, module_id)
    return _module_read(module)


async def update_module(
    db: AsyncSession,
    user: User,
    course_id: uuid.UUID,
    module_id: uuid.UUID,
    payload: ModuleUpdate,
) -> ModuleRead:
    """Édition partielle (renommage et/ou code) par mutation d'attributs.

    Ordre des execute : 1) cours (contrôle de propriété), 2) module (scopé
    cours). Seuls les champs de ``model_fields_set`` sont appliqués (les
    ``null`` explicites sont déjà rejetés en 422 par le schéma ; colonnes
    texte simples — pas de JSONB, la mutation d'attribut suffit).
    ``updated_at`` est posé côté Python (comme ``course.updated_at``) : le
    ``onupdate`` SQL ne tirerait qu'au flush, APRÈS la construction du
    ``ModuleRead`` — la réponse renverrait le timestamp de la sauvegarde
    précédente. Le ``ModuleRead`` reste construit AVANT le commit (piège
    ``MissingGreenlet``).
    """
    course = await get_owned_course(db, user, course_id)
    module = await _get_module(db, course, module_id)
    fields = payload.model_fields_set
    if "title" in fields:
        module.title = payload.title
    if "html" in fields:
        module.html = payload.html
    if "css" in fields:
        module.css = payload.css
    if "js" in fields:
        module.js = payload.js
    touch(module, course)
    read = _module_read(module)
    await db.commit()
    return read


async def delete_module(
    db: AsyncSession, user: User, course_id: uuid.UUID, module_id: uuid.UUID
) -> None:
    """Supprime un module ; ses blocs pointeurs partent par FK ``CASCADE``.

    Ordre des execute : 1) cours (contrôle de propriété), 2) module (scopé
    cours), 3) delete. Rien à purger côté storage : le code vit en base.
    """
    course = await get_owned_course(db, user, course_id)
    module = await _get_module(db, course, module_id)
    await db.execute(delete(Module).where(Module.id == module.id))
    touch(course)
    await db.commit()
