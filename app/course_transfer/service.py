"""Export/import d'un cours en archive ``.zip`` (manifest + binaires S3).

Seule exception à la règle « les binaires ne transitent jamais par le
backend » (contrainte Pi, cf. docs/decisions.md) : l'archive est assemblée et
parsée par l'API, volumes bornés (``TRANSFER_MAX_ZIP_BYTES`` global,
``S3_MAX_UPLOAD_BYTES`` par fichier). L'ordre des ``execute`` de chaque
fonction est un contrat des tests (fausse session FIFO) ; tout est scopé au
propriétaire (introuvable → 404, jamais 403).

L'import crée TOUJOURS un nouveau cours : uuid régénérés partout (cours,
blocs, ressources, modules), références remappées — colonnes
``resource_id``/``module_id`` ET références ``oc-resource:``/``oc-module:``
des chaînes markdown —, ``questions[].id`` conservés verbatim (stables à
vie), ``visibility`` par défaut (``draft``), classement remappé par les
``code`` des matières/niveaux (codes inconnus ignorés). Les put S3 précèdent
le commit : au pire des orphelins bucket, jamais une réf DB pointant un objet
absent (miroir de ``delete_course``, qui purge S3 APRÈS commit pour la même
raison).
"""

import uuid
from datetime import UTC, datetime
from tempfile import SpooledTemporaryFile

from fastapi import HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.http import invalid, unavailable
from app.core.storage import Storage
from app.course_transfer.archive import (
    InvalidArchive,
    build_zip_sync,
    extract_entry_sync,
    parse_zip_sync,
    rewrite_block_content,
)
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
from app.courses.schemas import CourseRead
from app.models.block import TYPE_EXERCISE, Block
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


async def _cleanup(storage: Storage, s3_keys: list[str]) -> None:
    """Purge best-effort des objets déjà poussés lors d'un import échoué."""
    try:
        await storage.delete_many(s3_keys)
    except Exception:  # best effort : l'erreur d'origine prime
        pass


async def import_course(
    db: AsyncSession, user: User, upload: UploadFile, storage: Storage
) -> CourseRead:
    """Recrée un cours complet depuis une archive d'export (nouveau cours).

    Phase 0 (sans DB) : taille du corps ≤ ``TRANSFER_MAX_ZIP_BYTES`` sinon
    413 ; ouverture + validation du manifest (``parse_zip_sync``, déporté en
    thread — les archives v1 y sont traduites en v2) sinon 422.

    Ordre des execute : 1) lookup matières par ``code``, 2) lookup niveaux
    (toujours exécutés, même sur listes vides — FIFO constant, motif
    ``create_course`` ; codes inconnus ignorés), 3) insert cours (RETURNING
    des timestamps ; ``visibility`` par défaut ``draft``), puis si non
    vides : 4) insert course_subjects, 5) insert course_education_levels,
    6) insert modules (executemany), 7) insert resources (executemany,
    ``status='available'`` — le commit n'a lieu qu'après les put S3),
    8) insert blocks (executemany, positions réécrites 0..n-1, contenus
    passés par ``rewrite_block_content``, colonnes remappées).

    PUIS les binaires : par ressource, extraction contrôlée (thread) →
    ``storage.put_object``. Un échec S3 → rollback + purge best-effort des
    clés déjà poussées → 503 ; une entrée incohérente → même nettoyage → 422.
    ENFIN le commit — le ``CourseRead`` est construit avant.
    """
    upload.file.seek(0, 2)
    size = upload.file.tell()
    upload.file.seek(0)
    if size > settings.TRANSFER_MAX_ZIP_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Archive au-dessus du plafond global",
        )
    try:
        zf, manifest = await run_in_threadpool(parse_zip_sync, upload.file)
    except InvalidArchive as exc:
        raise invalid(str(exc)) from exc

    try:
        subject_ids = list(
            (
                await db.execute(
                    select(Subject.id)
                    .where(Subject.code.in_(manifest.course.subject_codes))
                    .order_by(Subject.id)
                )
            )
            .scalars()
            .all()
        )
        education_level_ids = list(
            (
                await db.execute(
                    select(EducationLevel.id)
                    .where(EducationLevel.code.in_(manifest.course.education_level_codes))
                    .order_by(EducationLevel.id)
                )
            )
            .scalars()
            .all()
        )

        course_id = uuid.uuid4()
        created_at, updated_at = (
            await db.execute(
                insert(Course)
                .values(
                    id=course_id,
                    owner_id=user.id,
                    title=manifest.course.title,
                    description=manifest.course.description,
                    preview_settings=dict(manifest.course.preview_settings),
                )
                .returning(Course.created_at, Course.updated_at)
            )
        ).one()
        if subject_ids:
            await db.execute(
                course_subjects.insert(),
                [{"course_id": course_id, "subject_id": sid} for sid in subject_ids],
            )
        if education_level_ids:
            await db.execute(
                course_education_levels.insert(),
                [
                    {"course_id": course_id, "education_level_id": lid}
                    for lid in education_level_ids
                ],
            )

        module_map = {str(m.id): uuid.uuid4() for m in manifest.modules}
        if manifest.modules:
            await db.execute(
                insert(Module),
                [
                    {
                        "id": module_map[str(m.id)],
                        "course_id": course_id,
                        "title": m.title,
                        "html": m.html,
                        "css": m.css,
                        "js": m.js,
                    }
                    for m in manifest.modules
                ],
            )

        resource_map = {str(r.id): uuid.uuid4() for r in manifest.resources}
        # (entrée zip, s3_key cible, métadonnées) — pour la phase binaire.
        uploads: list[tuple[str, str, ManifestResource]] = []
        if manifest.resources:
            rows = []
            for r in manifest.resources:
                new_id = resource_map[str(r.id)]
                s3_key = (
                    f"courses/{course_id}/resources/{new_id}/"
                    f"{_sanitize_name(r.original_name)}"
                )
                rows.append(
                    {
                        "id": new_id,
                        "course_id": course_id,
                        "type": r.type,
                        "s3_key": s3_key,
                        "original_name": r.original_name,
                        "size": r.size,
                        "mime": r.mime,
                        "status": STATUS_AVAILABLE,
                    }
                )
                uploads.append((f"resources/{r.id}", s3_key, r))
            await db.execute(insert(Resource), rows)

        resource_refs = {old: str(new) for old, new in resource_map.items()}
        module_refs = {old: str(new) for old, new in module_map.items()}
        if manifest.blocks:
            rows = []
            for position, block in enumerate(manifest.blocks):
                content = rewrite_block_content(
                    block.type, block.content, resource_refs, module_refs
                )
                if block.type == TYPE_EXERCISE:
                    # Ids présents conservés verbatim (stables à vie) ; une
                    # question sans id (manifest écrit à la main) en reçoit
                    # un frais — jamais de question sans id en base.
                    content = {
                        **content,
                        "questions": [
                            {**q, "id": q.get("id") or str(uuid.uuid4())}
                            for q in content["questions"]
                        ],
                    }
                rows.append(
                    {
                        "id": uuid.uuid4(),
                        "course_id": course_id,
                        "position": position,
                        "type": block.type,
                        "title": block.title,
                        "description": block.description,
                        "content": content,
                        "resource_id": (
                            resource_map[str(block.resource_ref)]
                            if block.resource_ref
                            else None
                        ),
                        "module_id": (
                            module_map[str(block.module_ref)] if block.module_ref else None
                        ),
                    }
                )
            await db.execute(insert(Block), rows)

        read = CourseRead(
            id=course_id,
            title=manifest.course.title,
            description=manifest.course.description,
            subject_ids=subject_ids,
            education_level_ids=education_level_ids,
            block_count=len(manifest.blocks),
            preview_settings=manifest.course.preview_settings,
            visibility=VISIBILITY_DRAFT,
            created_at=created_at,
            updated_at=updated_at,
        )

        pushed: list[str] = []
        try:
            for entry_name, s3_key, meta in uploads:
                tmp = await run_in_threadpool(extract_entry_sync, zf, entry_name, meta.size)
                try:
                    await storage.put_object(s3_key, tmp, meta.mime)
                finally:
                    tmp.close()
                pushed.append(s3_key)
        except InvalidArchive as exc:
            await db.rollback()
            await _cleanup(storage, pushed)
            raise invalid(str(exc)) from exc
        except Exception as exc:
            await db.rollback()
            await _cleanup(storage, pushed)
            raise unavailable("Stockage S3 indisponible") from exc

        await db.commit()
        return read
    finally:
        zf.close()
