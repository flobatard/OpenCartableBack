"""Fabrique de l'application : logs, CORS, lifespan et montage des routeurs."""

import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.admin.router import router as admin_router
from app.ai.router import router as ai_router
from app.ai_credentials.router import router as ai_credentials_router
from app.core.ai import shutdown_langfuse
from app.core.config import settings
from app.core.database import engine
from app.core.kv import close_kv
from app.core.logging import REQUEST_ID_HEADER, AccessLogMiddleware, configure_logging
from app.course_assistant.router import router as course_assistant_router
from app.course_transfer.router import router as course_transfer_router
from app.courses.router import router as courses_router
from app.education_levels.router import router as education_levels_router
from app.modules.router import router as modules_router
from app.public.router import router as public_router
from app.resources.router import router as resources_router
from app.search.router import router as search_router
from app.share_links.router import router as share_links_router
from app.starter_course.router import router as starter_course_router
from app.student_exercises.router import router as student_exercises_router
from app.student_exercises.router import teacher_router as student_exercises_teacher_router
from app.subjects.router import router as subjects_router
from app.system.router import router as system_router
from app.users.router import router as users_router

# Effet de bord à l'import, comme le `settings = get_settings()` de
# core/config.py : c'est le seul emplacement qui marche partout à la fois —
# uvicorn (sa propre config de log a lieu AVANT l'import de l'app, on reprend
# donc la main), `--reload`, et les tests (l'appel a lieu pendant la collecte,
# avant que pytest ne pose le handler de caplog sur la racine). Jamais dans
# create_app() : dictConfig y arracherait ce handler à chaque test.
configure_logging()
logger = logging.getLogger(__name__)

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
    starter_course_router,  # /courses/starter
    courses_router,
    resources_router,
    modules_router,
    course_assistant_router,
    student_exercises_router,  # élève : JWT + accès au cours par le régime public
    student_exercises_teacher_router,  # prof : résumé / effacement des tentatives
    share_links_router,
    public_router,  # sans JWT : visibilité + token de partage
    search_router,  # sans JWT : /public/search
    admin_router,  # /admin/* : backoffice, rôle super_admin (403 sinon)
    ai_router,  # smoke-test du client IA, supprimable
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Preuve observable que la configuration a bien été appliquée, dans
    # l'esprit du log_plan() du scheduler (qui imprime le fuseau qu'il a
    # résolu). N'apparaît pas dans les tests : ils n'exécutent pas le lifespan.
    logger.info(
        "démarrage — APP_ENV=%s, DEBUG=%s, LOG_LEVEL=%s",
        settings.APP_ENV,
        settings.DEBUG,
        settings.LOG_LEVEL,
    )
    yield
    # Shutdown : flush des traces Langfuse (no-op sans config), le client
    # Redis s'il a servi (backoffice), puis le pool.
    shutdown_langfuse()
    await close_kv()
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
            # Une permission, pas un contrat : le front l'ignore aujourd'hui,
            # mais sans elle il ne pourra jamais lire l'id de corrélation d'une
            # réponse en erreur. Pas dans allow_headers pour autant — laisser
            # le front IMPOSER l'id est une autre décision.
            expose_headers=[REQUEST_ID_HEADER],
        )

    # Ajouté en DERNIER, donc middleware le PLUS EXTERNE (add_middleware
    # insère en tête de pile) : la durée mesurée couvre le CORS, et les
    # préflights sont journalisés. Inconditionnel, contrairement au CORS.
    app.add_middleware(
        AccessLogMiddleware,
        quiet_paths=frozenset({f"{settings.API_V1_PREFIX}/health"}),
    )

    for router in ROUTERS:
        app.include_router(router, prefix=settings.API_V1_PREFIX)

    return app


app = create_app()
