"""Fabrique de l'application : CORS, lifespan et montage des routeurs."""

from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ai.router import router as ai_router
from app.ai_credentials.router import router as ai_credentials_router
from app.core.ai import shutdown_langfuse
from app.core.config import settings
from app.core.database import engine
from app.course_assistant.router import router as course_assistant_router
from app.course_transfer.router import router as course_transfer_router
from app.courses.router import router as courses_router
from app.education_levels.router import router as education_levels_router
from app.modules.router import router as modules_router
from app.public.router import router as public_router
from app.resources.router import router as resources_router
from app.search.router import router as search_router
from app.share_links.router import router as share_links_router
from app.student_exercises.router import router as student_exercises_router
from app.student_exercises.router import teacher_router as student_exercises_teacher_router
from app.subjects.router import router as subjects_router
from app.system.router import router as system_router
from app.users.router import router as users_router

# Ordre de montage = ordre de matching. Une seule contrainte load-bearing :
# course_transfer AVANT courses — le littéral POST /courses/import doit primer
# sur un futur POST /courses/{course_id}. Tous montés sous API_V1_PREFIX.
ROUTERS: tuple[APIRouter, ...] = (
    system_router,  # /health (public), /me
    subjects_router,
    education_levels_router,
    users_router,
    ai_credentials_router,  # /users/me/ai-credentials
    course_transfer_router,  # /courses/import, /courses/{id}/export
    courses_router,
    resources_router,
    modules_router,
    course_assistant_router,
    student_exercises_router,  # élève : JWT + accès au cours par le régime public
    student_exercises_teacher_router,  # prof : résumé / effacement des tentatives
    share_links_router,
    public_router,  # sans JWT : visibilité + token de partage
    search_router,  # sans JWT : /public/search
    ai_router,  # smoke-test du client IA, supprimable
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Shutdown : flush des traces Langfuse (no-op sans config), puis le pool.
    shutdown_langfuse()
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        debug=settings.DEBUG,
        lifespan=lifespan,
    )

    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=False,  # auth par Bearer, aucun cookie
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type"],
        )

    for router in ROUTERS:
        app.include_router(router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
