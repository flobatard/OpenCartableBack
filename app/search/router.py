"""Routes de recherche publique — AUCUN ``Depends(get_current_user)`` ici.

C'est voulu et c'est le contrat du régime public (motif ``app/public/``) :
la recherche est anonyme par construction — seuls les cours ``public`` et les
profs opt-in remontent, l'autorisation vit dans ``app/search/service.py``.
Ne jamais ajouter de dépendance JWT sur ces routes, ni y exposer une donnée
réservée au prof. Le préfixe ``/public/`` fait aussi foi côté front : la
``customUrlValidation`` de la SPA n'attache pas de Bearer sous ``/v1/public/``.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.storage import Storage, get_storage
from app.search import service
from app.search.schemas import SearchCoursesPage, SearchTeachersPage

router = APIRouter(prefix="/public/search", tags=["search"])


@router.get("/courses", response_model=SearchCoursesPage)
async def search_courses(
    q: str | None = Query(default=None, max_length=200),
    subject_id: uuid.UUID | None = None,
    education_level_id: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: AsyncSession = Depends(get_db),
) -> SearchCoursesPage:
    """Recherche de cours publics — texte libre et/ou facettes (sans ``q``,
    la page est un catalogue trié du plus récent au plus ancien)."""
    return await service.search_courses(
        db,
        q=q,
        subject_id=subject_id,
        education_level_id=education_level_id,
        limit=limit,
        offset=offset,
    )


@router.get("/teachers", response_model=SearchTeachersPage)
async def search_teachers(
    q: str | None = Query(default=None, max_length=200),
    subject_id: uuid.UUID | None = None,
    education_level_id: uuid.UUID | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: AsyncSession = Depends(get_db),
    storage: Storage = Depends(get_storage),
) -> SearchTeachersPage:
    """Recherche de professeurs cherchables (opt-in + nom public + au moins
    un cours public)."""
    return await service.search_teachers(
        db,
        q=q,
        subject_id=subject_id,
        education_level_id=education_level_id,
        limit=limit,
        offset=offset,
        storage=storage,
    )
