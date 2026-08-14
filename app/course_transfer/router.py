"""Routes d'export/import de cours (archive ``.zip``).

Montées AVANT ``app/courses/router.py`` dans ``create_app()`` : le littéral
``POST /courses/import`` doit primer sur un futur ``POST
/courses/{course_id}``. Auth par paramètre (motif ``courses``) : chaque
handler résout la ligne ``users`` du prof pour scoper au propriétaire.
"""

import uuid

from fastapi import APIRouter, Depends, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.database import get_db
from app.core.storage import Storage, get_storage
from app.course_transfer import service
from app.courses.schemas import CourseRead
from app.users import service as users_service

router = APIRouter(tags=["course-transfer"])

_STREAM_CHUNK = 64 * 1024


@router.get("/courses/{course_id}/export")
async def export_course(
    course_id: uuid.UUID,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> StreamingResponse:
    """Archive ``.zip`` du cours : ``manifest.json`` + binaires des ressources."""
    user = await users_service.get_or_create_by_sub(db, auth)
    filename, tmp = await service.export_course(db, user, course_id, storage)
    tmp.seek(0, 2)
    length = tmp.tell()
    tmp.seek(0)

    def _chunks():
        while chunk := tmp.read(_STREAM_CHUNK):
            yield chunk

    # Générateur sync : Starlette l'itère dans un thread. Le fichier
    # temporaire disparaît à sa fermeture, planifiée après l'envoi.
    return StreamingResponse(
        _chunks(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(length),
        },
        background=BackgroundTask(tmp.close),
    )


@router.post(
    "/courses/import",
    response_model=CourseRead,
    status_code=status.HTTP_201_CREATED,
)
async def import_course(
    file: UploadFile,
    auth: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> CourseRead:
    """Recrée un cours complet depuis une archive d'export (toujours un nouveau cours)."""
    user = await users_service.get_or_create_by_sub(db, auth)
    return await service.import_course(db, user, file, storage)
