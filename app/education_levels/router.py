from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.education_levels import service
from app.education_levels.schemas import EducationLevelRead

router = APIRouter(tags=["education-levels"], dependencies=[Depends(get_current_user)])


@router.get("/education-levels/tree", response_model=list[EducationLevelRead])
async def read_education_level_tree(
    db: AsyncSession = Depends(get_db),
) -> list[EducationLevelRead]:
    """Classification complète des niveaux d'étude, en arbre (racines = cycles)."""
    return await service.get_education_level_tree(db)
