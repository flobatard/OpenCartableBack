from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """Declarative base shared by every model."""


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async DB session."""
    async with AsyncSessionLocal() as session:
        yield session


def touch(*rows: object) -> None:
    """Pose ``updated_at = maintenant (UTC)`` sur des instances ORM chargées.

    Bump côté Python, pas ``onupdate`` SQL : celui-ci ne tirerait qu'au flush,
    après la construction de la réponse, et un cours doit remonter dans la
    liste dès qu'un de ses contenus (bloc, ressource, module) change.
    """
    now = datetime.now(UTC)
    for row in rows:
        row.updated_at = now  # type: ignore[attr-defined]

