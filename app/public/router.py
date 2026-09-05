"""Routes publiques élèves — AUCUN ``Depends(get_current_user)`` ici.

C'est voulu et c'est le contrat de ce package (comme ``/health``) :
l'autorisation est portée par la visibilité du cours et le token de partage
(query param ``?token=``), vérifiés par :mod:`app.public.access` à chaque
requête. Ne jamais ajouter de dépendance JWT sur ces routes — et ne jamais
y exposer de donnée réservée au prof (voir schemas.py).

Le token voyage en query param : zéro en-tête custom (CORS inchangé), même
canal que l'URL front qui le porte déjà. Contrepartie assumée : il apparaît
dans les access logs du serveur — acceptable pour une capability URL
volontairement diffusable ; alternative durcie possible : ``X-Share-Token``.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.storage import Storage, get_storage
from app.education_levels import service as education_levels_service
from app.education_levels.schemas import EducationLevelRead
from app.public import service
from app.public.access import get_course_for_token, get_public_course
from app.public.schemas import (
    PublicCourseDetailRead,
    PublicDownloadRead,
    PublicModuleRead,
    PublicProfessorRead,
)
from app.subjects import service as subjects_service
from app.subjects.schemas import SubjectRead

router = APIRouter(prefix="/public", tags=["public"])


@router.get("/shared/{token}", response_model=PublicCourseDetailRead)
async def read_shared_course(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> PublicCourseDetailRead:
    """Détail complet filtré du cours pointé par un lien de partage."""
    course = await get_course_for_token(db, token)
    return await service.course_detail_public(db, course)


@router.get("/courses/{course_id}", response_model=PublicCourseDetailRead)
async def read_public_course(
    course_id: uuid.UUID,
    token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> PublicCourseDetailRead:
    """Détail complet filtré d'un cours public (ou privé avec ``?token=``)."""
    course = await get_public_course(db, course_id, token)
    return await service.course_detail_public(db, course)


@router.get(
    "/courses/{course_id}/resources/{resource_id}/download",
    response_model=PublicDownloadRead,
)
async def download_public_resource(
    course_id: uuid.UUID,
    resource_id: uuid.UUID,
    token: str | None = Query(default=None),
    disposition: Literal["attachment", "inline"] = Query(default="attachment"),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> PublicDownloadRead:
    """URL présignée de lecture d'une ressource du cours (bucket jamais public)."""
    course = await get_public_course(db, course_id, token)
    return await service.presign_download_public(
        db, course, resource_id, storage, inline=disposition == "inline"
    )


@router.get("/courses/{course_id}/modules/{module_id}", response_model=PublicModuleRead)
async def read_public_module(
    course_id: uuid.UUID,
    module_id: uuid.UUID,
    token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> PublicModuleRead:
    """Code d'un module interactif du cours (exécuté en iframe sandbox)."""
    course = await get_public_course(db, course_id, token)
    return await service.get_module_public(db, course, module_id)


@router.get("/professors/{user_id}/courses", response_model=PublicProfessorRead)
async def list_professor_public_courses(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> PublicProfessorRead:
    """Catalogue public d'un prof : ses cours ``public`` uniquement."""
    return await service.list_public_courses_by_professor(db, user_id, storage)


@router.get("/subjects/tree", response_model=list[SubjectRead])
async def read_public_subject_tree(
    db: AsyncSession = Depends(get_db),
) -> list[SubjectRead]:
    """Arbre des matières, lecture publique (facettes de recherche) —
    délégation pure, réponse strictement identique à ``GET /subjects/tree``."""
    return await subjects_service.get_subject_tree(db)


@router.get("/education-levels/tree", response_model=list[EducationLevelRead])
async def read_public_education_level_tree(
    db: AsyncSession = Depends(get_db),
) -> list[EducationLevelRead]:
    """Arbre des niveaux d'étude, lecture publique (facettes de
    recherche) — délégation pure, identique à ``GET /education-levels/tree``."""
    return await education_levels_service.get_education_level_tree(db)
