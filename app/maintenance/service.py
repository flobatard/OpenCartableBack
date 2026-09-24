"""Les tâches de purge — une fonction par jeu de données.

Règles communes à toutes :

- **Rétention en jours, ``0`` (ou moins) = tâche désactivée** : elle sort avant
  le moindre ``execute``. C'est le défaut de deux d'entre elles.
- **La borne est calculée en Python** (``datetime.now(UTC)``), jamais avec le
  ``now()`` de Postgres : celui-ci suit le fuseau du serveur, alors que tous les
  timestamps du projet sont en UTC (``ai_daily_usage.day`` est même un ``Date``
  UTC — un ``WHERE day < now() - interval 'N days'`` dériverait d'un jour).
- **Une transaction par tâche**, commitée par la tâche : l'échec de l'une ne
  doit ni annuler ni empêcher les autres (:mod:`app.maintenance.runner` les
  isole aussi).
- Chaque tâche renvoie un **compte** de lignes touchées, pour le journal.

Les tâches qui touchent S3 suivent le motif de ``delete_course``
(:mod:`app.courses.service`) : DELETE en base → ``commit`` → **puis** purge du
bucket. Un échec S3 après commit laisse un orphelin (que la réconciliation
rattrape) ; l'inverse laisserait une référence DB pointant un objet absent.

Ces fonctions ne se connaissent pas entre elles : la liste des tâches, leur
cadence et leur enchaînement vivent dans :mod:`app.maintenance.registry` et
:mod:`app.maintenance.runner`. Les contrôles en lecture seule, qui ne suivent
aucune des règles ci-dessus, vivent dans :mod:`app.maintenance.checks`.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import Storage
from app.models.ai_attachment import AIAttachment
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


async def purge_ai_conversations(db: AsyncSession, storage: Storage, days: int) -> int:
    """Supprime les conversations sans activité depuis la rétention.

    ``updated_at`` est bumpé **côté Python** à chaque message persisté : c'est
    une vraie date de dernière activité, pas un artefact de flush. Les
    ``ai_messages`` et les ``ai_attachments`` partent par la FK ``CASCADE`` ;
    les objets S3 des pièces jointes, hors cascade DB, sont purgés **après** le
    commit (motif ``delete_course``).

    Ordre des execute : 1) clés S3 des pièces jointes des conversations
    concernées, 2) delete.

    Désactivée par défaut (``PURGE_AI_CONVERSATIONS_DAYS = 0``) : c'est du
    travail de prof, et l'effacement manuel existe déjà.
    """
    if days <= 0:
        return 0
    condition = AIConversation.updated_at < _cutoff(days)
    s3_keys = list(
        (
            await db.execute(
                select(AIAttachment.s3_key).where(
                    AIAttachment.conversation_id.in_(
                        select(AIConversation.id).where(condition)
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    result = await db.execute(delete(AIConversation).where(condition))
    await db.commit()
    if s3_keys:
        await storage.delete_many(s3_keys)
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


async def purge_unsent_attachments(db: AsyncSession, storage: Storage, days: int) -> int:
    """Supprime les pièces jointes jamais envoyées et leurs objets S3.

    Un seul prédicat, ``message_id IS NULL``, couvre les deux abandons : le
    presign dont le PUT ou la confirmation ne sont jamais venus (la ligne reste
    ``pending``), et la pièce confirmée puis jamais jointe à un message (le prof
    a changé d'avis, ou fermé l'onglet avant d'envoyer). Une pièce rattachée à
    un message, elle, vit et meurt avec sa conversation (FK ``CASCADE``).

    Rétention **courte** par défaut (``PURGE_AI_ATTACHMENTS_DAYS = 7``) : c'est
    du déchet qui occupe le bucket, pas du travail de prof.

    Pas d'index sur ``created_at`` : seq scan assumé sur une petite table
    (même doctrine que ``purge_exercise_submissions``).

    Ordre des execute : 1) clés S3 des pièces concernées, 2) delete ; purge du
    bucket **après** le commit (motif ``delete_course``).
    """
    if days <= 0:
        return 0
    condition = AIAttachment.message_id.is_(None) & (AIAttachment.created_at < _cutoff(days))
    s3_keys = list(
        (await db.execute(select(AIAttachment.s3_key).where(condition))).scalars().all()
    )
    if not s3_keys:
        return 0
    result = await db.execute(delete(AIAttachment).where(condition))
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
    plus récents que la grâce, puis une anti-jointure contre ``resources.s3_key``,
    ``users.avatar_s3_key`` et ``ai_attachments.s3_key`` désigne les orphelins.
    Le bucket n'est jamais tenu en mémoire.

    ⚠ **Toute table qui écrit une clé S3 doit figurer dans cette anti-jointure**,
    sans quoi ses objets sont déclarés orphelins et supprimés. Les trois qui
    existent sont ci-dessous, dans cet ordre (contrat FIFO des tests : trois
    execute par page).

    La **grâce est une sécurité, pas un confort** : l'import de cours pousse ses
    objets AVANT son commit (:mod:`app.course_transfer.importer`), et un upload
    de ressource/avatar/pièce jointe vit entre son presign et sa confirmation —
    pendant ces fenêtres un objet légitime n'a pas (encore) de ligne. Une grâce
    de plusieurs jours les couvre toutes largement.

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
            known.update(
                (
                    await db.execute(
                        select(AIAttachment.s3_key).where(
                            AIAttachment.s3_key.in_(candidates)
                        )
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


