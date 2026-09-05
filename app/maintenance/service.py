"""Les tâches de purge — une fonction par jeu de données.

Règles communes à toutes :

- **Rétention en jours, ``0`` (ou moins) = tâche désactivée** : elle sort avant
  le moindre ``execute``. C'est le défaut de deux d'entre elles.
- **La borne est calculée en Python** (``datetime.now(UTC)``), jamais avec le
  ``now()`` de Postgres : celui-ci suit le fuseau du serveur, alors que tous les
  timestamps du projet sont en UTC (``ai_daily_usage.day`` est même un ``Date``
  UTC — un ``WHERE day < now() - interval 'N days'`` dériverait d'un jour).
- **Une transaction par tâche**, commitée par la tâche : l'échec de l'une ne
  doit ni annuler ni empêcher les autres (l'orchestrateur les isole aussi).
- Chaque tâche renvoie un **compte** de lignes touchées, pour le journal.

Les tâches qui touchent S3 suivent le motif de ``delete_course``
(:mod:`app.courses.service`) : DELETE en base → ``commit`` → **puis** purge du
bucket. Un échec S3 après commit laisse un orphelin (que la réconciliation
rattrape) ; l'inverse laisserait une référence DB pointant un objet absent.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.storage import Storage
from app.models.ai_conversation import AIConversation
from app.models.ai_daily_usage import AIDailyUsage
from app.models.ai_message import ROLE_TOOL, AIMessage
from app.models.exercise_submission import ExerciseSubmission
from app.models.resource import STATUS_PENDING, Resource
from app.models.share_link import ShareLink
from app.models.user import User

logger = logging.getLogger(__name__)

# Rétention plancher des compteurs de quota, quel que soit le réglage : le jour
# UTC courant porte le quota vivant, et `refund_default_quota` peut viser LA
# VEILLE (un QuotaTicket capturé avant minuit UTC, remboursé après).
MIN_USAGE_RETENTION_DAYS = 2

# Ce qu'on garde d'un résultat d'outil allégé : exactement l'extrait que le
# front affichait déjà pendant le flux (TOOL_RESULT_EXCERPT_CHARS de
# app/course_assistant/turn_encoder.py). L'affichage déplié ne change donc pas.
TOOL_CONTENT_KEEP_CHARS = 400
TOOL_CONTENT_MARKER = (
    "\n\n[Résultat d'outil allégé par la purge : seul le début est conservé. "
    "Relancez l'outil si son contenu complet est nécessaire.]"
)

# Les deux seuls préfixes de clés que l'application écrit. Tout ce qui vit
# ailleurs dans le bucket ne nous appartient pas : jamais touché.
S3_PREFIXES = ("courses/", "users/")


def _cutoff(days: int) -> datetime:
    """Borne d'ancienneté en UTC (les timestamps du projet sont tz-aware)."""
    return datetime.now(UTC) - timedelta(days=days)


async def purge_ai_daily_usage(db: AsyncSession, days: int) -> int:
    """Supprime les compteurs de quota antérieurs à la rétention.

    ``day`` est un ``Date`` **UTC** : la borne est un ``date``, calculée comme
    le fait ``_consume_default_quota`` (``datetime.now(UTC).date()``).
    La rétention est plancherée à :data:`MIN_USAGE_RETENTION_DAYS` — supprimer
    la ligne du jour rouvrirait un quota épuisé, et supprimer celle de la veille
    ferait perdre un remboursement à cheval sur minuit.
    """
    if days <= 0:
        return 0
    days = max(days, MIN_USAGE_RETENTION_DAYS)
    cutoff = datetime.now(UTC).date() - timedelta(days=days)
    result = await db.execute(delete(AIDailyUsage).where(AIDailyUsage.day < cutoff))
    await db.commit()
    return result.rowcount


async def purge_tool_message_content(db: AsyncSession, days: int) -> int:
    """Allège le contenu des tours ``tool`` anciens — le plus gros gain disque.

    Un tour ``tool`` persiste le résultat **complet** de l'outil (jusqu'à
    ``PDF_MAX_CHARS`` = 40 000 caractères par lecture de PDF) : une conversation
    qui enchaîne les lectures pèse plusieurs Mo. On garde les
    :data:`TOOL_CONTENT_KEEP_CHARS` premiers caractères + un marqueur, et on
    jette le reste.

    **La ligne n'est jamais supprimée** : un tour ``tool`` doit rester apparié
    au ``tool_calls`` du segment assistant qui le précède (CHECK
    ``ck_ai_messages_tool_call_id``), et ``replay_messages`` replie les rounds
    incomplets. Le marqueur est rédigé pour être compris aussi bien du prof qui
    déplie l'appel que du modèle si le round est rejoué.

    Idempotente par arithmétique : une ligne déjà allégée mesure exactement
    ``keep + len(marqueur)`` et ne repasse pas le prédicat de longueur.
    """
    if days <= 0:
        return 0
    threshold = TOOL_CONTENT_KEEP_CHARS + len(TOOL_CONTENT_MARKER)
    result = await db.execute(
        update(AIMessage)
        .where(
            AIMessage.role == ROLE_TOOL,
            AIMessage.created_at < _cutoff(days),
            func.length(AIMessage.content) > threshold,
        )
        .values(
            content=func.left(AIMessage.content, TOOL_CONTENT_KEEP_CHARS)
            + TOOL_CONTENT_MARKER
        )
    )
    await db.commit()
    return result.rowcount


async def purge_ai_conversations(db: AsyncSession, days: int) -> int:
    """Supprime les conversations sans activité depuis la rétention.

    ``updated_at`` est bumpé **côté Python** à chaque message persisté : c'est
    une vraie date de dernière activité, pas un artefact de flush. Les
    ``ai_messages`` partent par la FK ``CASCADE``.

    Désactivée par défaut (``PURGE_AI_CONVERSATIONS_DAYS = 0``) : c'est du
    travail de prof, et l'effacement manuel existe déjà.
    """
    if days <= 0:
        return 0
    result = await db.execute(
        delete(AIConversation).where(AIConversation.updated_at < _cutoff(days))
    )
    await db.commit()
    return result.rowcount


async def purge_exercise_submissions(db: AsyncSession, days: int) -> int:
    """Supprime les tentatives d'élèves antérieures à la rétention.

    Désactivée par défaut (``PURGE_EXERCISE_SUBMISSIONS_DAYS = 0``) : ce sont
    des données personnelles d'élèves, et l'effacement manuel existe des deux
    côtés (l'élève ses tours, le prof ceux de tous ses élèves).

    Pas d'index sur ``created_at`` seul (celui du fil est
    ``(user_id, block_id, question_id, created_at)``) : seq scan assumé tant
    que la tâche reste désactivée et la table petite.
    """
    if days <= 0:
        return 0
    result = await db.execute(
        delete(ExerciseSubmission).where(ExerciseSubmission.created_at < _cutoff(days))
    )
    await db.commit()
    return result.rowcount


async def purge_share_links(db: AsyncSession, days: int) -> int:
    """Supprime les liens de partage expirés depuis la rétention.

    Un lien expiré résout **déjà** en 404 uniforme (``_link_valid``,
    :mod:`app.public.service`) : la suppression n'a aucun effet observable côté
    élève. Prédicat sur ``expires_at`` **seul** — un lien révoqué mais non
    encore expiré est délibérément conservé : le modèle documente la révocation
    soft comme une trace d'audit qui reste listée au prof.
    """
    if days <= 0:
        return 0
    result = await db.execute(delete(ShareLink).where(ShareLink.expires_at < _cutoff(days)))
    await db.commit()
    return result.rowcount


async def purge_pending_resources(db: AsyncSession, storage: Storage, days: int) -> int:
    """Supprime les ressources restées ``pending`` et leurs objets S3.

    Une ressource est créée AVANT l'upload direct navigateur→S3 ; si le PUT ou
    la confirmation n'arrivent jamais, la ligne reste ``pending`` à vie (le
    front l'affiche atténuée, mais rien ne la balaye). L'objet S3 peut exister
    ou non — ``delete_many`` est indifférent aux clés absentes.

    Aucun bloc n'est perdu au passage : le ``PATCH`` d'un bloc ``document``
    exige une ressource ``available``, une ``pending`` n'est donc jamais pointée.

    Ordre des execute : 1) clés S3 des ressources concernées, 2) delete ;
    purge du bucket **après** le commit (motif ``delete_course``).
    """
    if days <= 0:
        return 0
    cutoff = _cutoff(days)
    condition = (Resource.status == STATUS_PENDING) & (Resource.created_at < cutoff)
    s3_keys = list(
        (await db.execute(select(Resource.s3_key).where(condition))).scalars().all()
    )
    if not s3_keys:
        return 0
    result = await db.execute(delete(Resource).where(condition))
    await db.commit()
    await storage.delete_many(s3_keys)
    return result.rowcount


async def reconcile_s3_orphans(
    db: AsyncSession, storage: Storage, days: int, dry_run: bool
) -> int:
    """Supprime du bucket les objets qu'aucune ligne de la base ne référence.

    Le filet promis par « Nettoyage S3 aux suppressions » : toute purge S3 de
    l'API a lieu **après** son commit, donc un échec réseau y laisse un
    orphelin — sans jamais l'inverse.

    Marche **page par page** (1 000 clés) : pour chacune, on écarte les objets
    plus récents que la grâce, puis une anti-jointure contre ``resources.s3_key``
    et ``users.avatar_s3_key`` désigne les orphelins. Le bucket n'est jamais
    tenu en mémoire.

    La **grâce est une sécurité, pas un confort** : l'import de cours pousse ses
    objets AVANT son commit (:mod:`app.course_transfer.importer`), et un upload
    de ressource/avatar vit entre son presign et sa confirmation — pendant ces
    fenêtres un objet légitime n'a pas (encore) de ligne. Une grâce de plusieurs
    jours les couvre toutes largement.

    Seuls les préfixes de :data:`S3_PREFIXES` sont balayés : le bucket peut
    contenir autre chose, qui ne nous appartient pas.

    ``dry_run`` (défaut) : les candidats sont journalisés, rien n'est supprimé.
    Renvoie le nombre d'orphelins **trouvés** (supprimés si ``dry_run`` est
    faux) — la base n'est jamais modifiée.
    """
    if days <= 0:
        return 0
    cutoff = _cutoff(days)
    found = 0
    for prefix in S3_PREFIXES:
        async for page in storage.iter_objects(prefix):
            candidates = [obj.key for obj in page if obj.last_modified < cutoff]
            if not candidates:
                continue
            known = set(
                (
                    await db.execute(
                        select(Resource.s3_key).where(Resource.s3_key.in_(candidates))
                    )
                )
                .scalars()
                .all()
            )
            known.update(
                (
                    await db.execute(
                        select(User.avatar_s3_key).where(User.avatar_s3_key.in_(candidates))
                    )
                )
                .scalars()
                .all()
            )
            orphans = [key for key in candidates if key not in known]
            if not orphans:
                continue
            found += len(orphans)
            if dry_run:
                for key in orphans:
                    logger.info("orphelin S3 (dry-run, non supprimé) : %s", key)
            else:
                await storage.delete_many(orphans)
    return found


@dataclass(frozen=True)
class TaskResult:
    """Issue d'une tâche : son libellé, ce qu'elle a touché, si elle a échoué."""

    name: str
    count: int
    failed: bool = False


@dataclass(frozen=True)
class PurgeReport:
    """Synthèse d'une passe complète."""

    tasks: list[TaskResult]

    @property
    def failed(self) -> bool:
        return any(task.failed for task in self.tasks)

    def summary(self) -> str:
        return ", ".join(
            f"{task.name}={'échec' if task.failed else task.count}" for task in self.tasks
        )


async def run_purge(db: AsyncSession, storage: Storage) -> PurgeReport:
    """Exécute toutes les tâches selon les rétentions configurées.

    Chaque tâche est **isolée** : son échec est journalisé, la session est
    rollbackée et la passe continue — une erreur sur un jeu de données ne doit
    pas priver les autres de leur purge. Les rétentions sont lues dans les
    settings (surchargeables par variable d'env, sans reconstruire l'image).
    """
    # Des *callables*, pas des coroutines : rien ne démarre avant son tour, et
    # un échec ne laisse pas derrière lui des coroutines jamais attendues.
    plan: list[tuple[str, Callable[[], Awaitable[int]]]] = [
        ("compteurs_quota", partial(purge_ai_daily_usage, db, settings.PURGE_AI_USAGE_DAYS)),
        (
            "contenu_tours_tool",
            partial(purge_tool_message_content, db, settings.PURGE_AI_TOOL_CONTENT_DAYS),
        ),
        (
            "conversations_ia",
            partial(purge_ai_conversations, db, settings.PURGE_AI_CONVERSATIONS_DAYS),
        ),
        (
            "tentatives_eleves",
            partial(
                purge_exercise_submissions, db, settings.PURGE_EXERCISE_SUBMISSIONS_DAYS
            ),
        ),
        ("liens_partage", partial(purge_share_links, db, settings.PURGE_SHARE_LINKS_DAYS)),
        (
            "ressources_pending",
            partial(
                purge_pending_resources, db, storage, settings.PURGE_PENDING_RESOURCES_DAYS
            ),
        ),
        (
            "orphelins_s3",
            partial(
                reconcile_s3_orphans,
                db,
                storage,
                settings.PURGE_S3_ORPHANS_DAYS,
                settings.PURGE_S3_ORPHANS_DRY_RUN,
            ),
        ),
    ]
    results: list[TaskResult] = []
    for name, task in plan:
        try:
            count = await task()
        except Exception:  # noqa: BLE001 — une tâche qui tombe n'arrête pas la passe
            logger.exception("purge %s : échec", name)
            await db.rollback()
            results.append(TaskResult(name=name, count=0, failed=True))
        else:
            logger.info("purge %s : %d", name, count)
            results.append(TaskResult(name=name, count=count))
    return PurgeReport(tasks=results)
