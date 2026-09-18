"""Routes du backoffice, montées sous ``/admin``.

La garde est portée par le router : **toute** route déclarée ici exige le rôle
de plateforme ``super_admin`` (403 sinon, 401 sans token valide). Une route
qui a besoin du compte le redemande en paramètre — le cache de dépendances de
FastAPI n'exécute la garde qu'une fois par requête.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import service
from app.admin.schemas import MaintenanceOverviewRead
from app.core.database import get_db
from app.core.kv import KeyValueStore, get_kv
from app.models.user import User
from app.users.dependencies import require_super_admin

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_super_admin)])


@router.get("/maintenance/jobs", response_model=MaintenanceOverviewRead)
async def read_maintenance_jobs(
    db: AsyncSession = Depends(get_db),
    kv: KeyValueStore = Depends(get_kv),
) -> MaintenanceOverviewRead:
    """État du scheduler et de chaque job du registre : plan, demande, dernière passe."""
    return await service.read_overview(db, kv)


@router.post(
    "/maintenance/jobs/{job_name}/run",
    response_model=MaintenanceOverviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_maintenance_run(
    job_name: str,
    admin: User = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
    kv: KeyValueStore = Depends(get_kv),
) -> MaintenanceOverviewRead:
    """Demande une passe manuelle : 202, le scheduler la relève dès qu'aucune
    passe n'est en vol (au plus ``MAINTENANCE_REQUEST_POLL_SECONDS`` après)."""
    return await service.request_run(db, kv, admin, job_name)
