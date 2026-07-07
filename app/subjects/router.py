from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.subjects import service
from app.subjects.schemas import SubjectRead

router = APIRouter(tags=["subjects"], dependencies=[Depends(get_current_user)])


@router.get("/subjects/tree", response_model=list[SubjectRead])
async def read_subject_tree(db: AsyncSession = Depends(get_db)) -> list[SubjectRead]:
    """Taxonomie complète des matières, en arbre (racines = disciplines)."""
    return await service.get_subject_tree(db)
