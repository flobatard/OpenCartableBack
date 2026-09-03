from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.ai.router import router as ai_router
from app.ai_credentials.router import router as ai_credentials_router
from app.auth.router import router as auth_router
from app.core.ai import shutdown_langfuse
from app.core.config import settings
from app.core.database import engine
from app.course_assistant.router import router as course_assistant_router
from app.course_transfer.router import router as course_transfer_router
from app.courses.router import router as courses_router
from app.education_levels.router import router as education_levels_router
from app.health.router import router as health_router
from app.modules.router import router as modules_router
from app.public.router import router as public_router
from app.resources.router import router as resources_router
from app.search.router import router as search_router
from app.share_links.router import router as share_links_router
from app.student_exercises.router import router as student_exercises_router
from app.student_exercises.router import teacher_router as student_exercises_teacher_router
from app.subjects.router import router as subjects_router
from app.users.router import router as users_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic goes here (warm caches, check connections, ...)
    yield
    # Shutdown: flush pending Langfuse traces (no-op unless configured),
    # then release the DB connection pool cleanly
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
            allow_credentials=False,  # auth is Bearer-based, no cookies
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type"],
        )

    app.include_router(health_router, prefix=settings.API_V1_PREFIX)
    app.include_router(auth_router, prefix=settings.API_V1_PREFIX)
    app.include_router(subjects_router, prefix=settings.API_V1_PREFIX)
    app.include_router(education_levels_router, prefix=settings.API_V1_PREFIX)
    app.include_router(users_router, prefix=settings.API_V1_PREFIX)
    # Credential IA chiffré de l'utilisateur (/users/me/ai-credentials).
    app.include_router(ai_credentials_router, prefix=settings.API_V1_PREFIX)
    # Avant courses_router : POST /courses/import doit matcher le segment
    # littéral avant un futur POST /courses/{course_id}.
    app.include_router(course_transfer_router, prefix=settings.API_V1_PREFIX)
    app.include_router(courses_router, prefix=settings.API_V1_PREFIX)
    app.include_router(resources_router, prefix=settings.API_V1_PREFIX)
    app.include_router(modules_router, prefix=settings.API_V1_PREFIX)
    # Assistant IA du cours (J5) : conversations persistées + SSE agent.
    app.include_router(course_assistant_router, prefix=settings.API_V1_PREFIX)
    # Tuteur IA d'exercice élève (J5) : JWT de l'élève + accès public au cours.
    app.include_router(student_exercises_router, prefix=settings.API_V1_PREFIX)
    # Régime professeur du même package : résumé/effacement des tentatives.
    app.include_router(student_exercises_teacher_router, prefix=settings.API_V1_PREFIX)
    app.include_router(share_links_router, prefix=settings.API_V1_PREFIX)
    # Régime élève (J2) : routes publiques par visibilité/token de partage,
    # sans JWT — l'autorisation vit dans app/public/service.py.
    app.include_router(public_router, prefix=settings.API_V1_PREFIX)
    # Recherche publique (J3) : même régime sans JWT (préfixe /public/search).
    app.include_router(search_router, prefix=settings.API_V1_PREFIX)
    # Smoke-test du client IA générique (BYO token) — référence SSE, supprimable.
    app.include_router(ai_router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
