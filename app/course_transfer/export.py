"""Export d'un cours en archive ``.zip`` (manifest + binaires S3).

Seule exception à la règle « les binaires ne transitent jamais par le
backend » (contrainte Pi, cf. docs/decisions.md) : l'archive est assemblée
par l'API, dans un fichier temporaire spoolé (jamais entière en RAM). Non
exportés : propriétaire, visibilité, liens de partage, statut et ``s3_key``
des ressources, ressources ``pending``. Matières et niveaux voyagent par
leur ``code`` (portable entre instances).
"""

import uuid
from datetime import UTC, datetime
from tempfile import SpooledTemporaryFile

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import Storage
from app.course_transfer.archive import build_zip_sync
from app.course_transfer.schemas import (
    FORMAT,
    FORMAT_VERSION,
    CourseManifest,
    ManifestBlock,
    ManifestCourse,
    ManifestModule,
    ManifestResource,
)
from app.courses.queries import get_owned_course
from app.models.block import Block
from app.models.course import course_education_levels, course_subjects
from app.models.education_level import EducationLevel
from app.models.module import Module
from app.models.resource import STATUS_AVAILABLE, Resource
from app.models.subject import Subject
from app.models.user import User
from app.resources.service import _sanitize_name


async def export_course(
    db: AsyncSession, user: User, course_id: uuid.UUID, storage: Storage
) -> tuple[str, SpooledTemporaryFile]:
    """Archive d'export d'un cours : ``(nom de fichier, fichier temporaire)``.

    Ordre des execute : 1) cours (contrôle de propriété), 2) codes matières,
    3) codes niveaux, 4) blocs (tri ``position, id``), 5) ressources
    ``available`` uniquement (tri ``created_at desc, id`` — les
    ``pending`` n'ont pas de binaire sûr : exclues, un bloc ``document``
    qui en pointerait une sort détaché), 6) modules (tri ``created_at desc,
    id``). Lecture seule : pas de commit. L'assemblage du zip (lectures S3
    comprises) est déporté en UN ``run_in_threadpool``.
    """
    course = await get_owned_course(db, user, course_id)
    subject_codes = list(
        (
            await db.execute(
                select(Subject.code)
                .select_from(
                    course_subjects.join(Subject, course_subjects.c.subject_id == Subject.id)
                )
                .where(course_subjects.c.course_id == course.id)
                .order_by(Subject.code)
            )
        )
        .scalars()
        .all()
    )
    education_level_codes = list(
        (
            await db.execute(
                select(EducationLevel.code)
                .select_from(
                    course_education_levels.join(
                        EducationLevel,
                        course_education_levels.c.education_level_id == EducationLevel.id,
                    )
                )
                .where(course_education_levels.c.course_id == course.id)
                .order_by(EducationLevel.code)
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
    resources = (
        (
            await db.execute(
                select(Resource)
                .where(
                    Resource.course_id == course.id,
                    Resource.status == STATUS_AVAILABLE,
                )
                .order_by(Resource.created_at.desc(), Resource.id)
            )
        )
        .scalars()
        .all()
    )
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

    available = {r.id for r in resources}
    manifest = CourseManifest(
        format=FORMAT,
        format_version=FORMAT_VERSION,
        exported_at=datetime.now(UTC),
        course=ManifestCourse(
            title=course.title,
            description=course.description,
            preview_settings=course.preview_settings,
            subject_codes=subject_codes,
            education_level_codes=education_level_codes,
        ),
        blocks=[
            ManifestBlock(
                position=block.position,
                type=block.type,
                title=block.title,
                description=block.description,
                content=block.content,
                # Garde défensive : un bloc ne peut pointer qu'une ressource
                # « available » (update_block), mais le manifest exige que
                # toute ref soit déclarée — on détache plutôt que d'échouer.
                resource_ref=(
                    block.resource_id if block.resource_id in available else None
                ),
                module_ref=block.module_id,
            )
            for block in blocks
        ],
        resources=[
            ManifestResource(
                id=resource.id,
                type=resource.type,
                original_name=resource.original_name,
                size=resource.size,
                mime=resource.mime,
            )
            for resource in resources
        ],
        modules=[
            ManifestModule(
                id=module.id,
                title=module.title,
                html=module.html,
                css=module.css,
                js=module.js,
            )
            for module in modules
        ],
    )
    manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
    entries = [(str(resource.id), resource.s3_key) for resource in resources]
    tmp = await run_in_threadpool(build_zip_sync, manifest_bytes, entries, storage)
    date = datetime.now(UTC).strftime("%Y%m%d")
    return f"course-{_sanitize_name(course.title)}-{date}.zip", tmp
