from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.database import engine
from app.health.router import router as health_router
from app.users.router import router as users_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic goes here (warm caches, check connections, ...)
    yield
    # Shutdown: release the DB connection pool cleanly
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        debug=settings.DEBUG,
        lifespan=lifespan,
    )

    app.include_router(health_router, prefix=settings.API_V1_PREFIX)
    app.include_router(users_router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
