"""Contrôles périodiques — **ils observent, ils ne corrigent rien**.

Aucun ``DELETE``, aucun ``UPDATE``, aucun ``commit``, aucune suppression S3 :
c'est la différence de nature avec :mod:`app.maintenance.service`, dont chaque
tâche commite sa transaction. Leur résultat vit dans les logs et dans
``maintenance_job_state.last_detail``, et l'action qui en découle est **humaine**.

Les deux contrôles respectent la contrainte Pi de bout en bout : le bucket n'est
jamais tenu en mémoire (inventaire page par page) et la vérification inverse est
**bornée par passe**, avec un curseur qui tourne d'une passe à l'autre.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import Storage
from app.maintenance.results import JobOutcome
from app.maintenance.service import S3_PREFIXES
from app.models.ai_conversation import AIConversation
from app.models.ai_daily_usage import AIDailyUsage
from app.models.ai_message import AIMessage
from app.models.exercise_submission import ExerciseSubmission
from app.models.resource import STATUS_AVAILABLE, STATUS_PENDING, Resource
from app.models.share_link import ShareLink
from app.models.user import User

logger = logging.getLogger(__name__)

# Les tables dont la volumétrie est suivie : celles que la purge fait décroître,
# plus celles qui les portent. L'ordre fixe celui des colonnes du SELECT — un
# contrat rejoué par les tests.
INVENTORIED_TABLES: tuple[tuple[str, type], ...] = (
    ("ai_daily_usage", AIDailyUsage),
    ("ai_conversations", AIConversation),
    ("ai_messages", AIMessage),
    ("exercise_submissions", ExerciseSubmission),
    ("share_links", ShareLink),
    ("resources", Resource),
    ("users", User),
)

# Taille RÉELLE sur disque (données + index + TOAST) des tables du schéma
# courant. `pg_total_relation_size` est la métrique qui compte pour une carte
# SD ; sur `ai_messages`, le TOAST est l'essentiel du poids.
TABLE_SIZES_SQL = text(
    "SELECT c.relname AS name, pg_total_relation_size(c.oid) AS bytes "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = current_schema() AND c.relkind = 'r'"
)


def _human_bytes(size: int) -> str:
    """Taille lisible d'un coup d'œil dans un ``docker logs``."""
    value = float(size)
    for unit in ("o", "Kio", "Mio", "Gio"):
        if value < 1024 or unit == "Gio":
            return f"{value:.0f} {unit}" if unit == "o" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} Gio"  # pragma: no cover — la boucle sort toujours avant


async def storage_inventory(db: AsyncSession, storage: Storage) -> JobOutcome:
    """Rapport de volumétrie : lignes par table, octets sur disque, objets S3.

    Ce que ça sert : régler les rétentions sur des chiffres plutôt qu'au doigt
    mouillé, et **voir venir** la saturation du Pi au lieu de la constater.

    ``count(*)`` exact, et non les estimations ``pg_class.reltuples`` : celles-ci
    sont gratuites mais fausses (``-1`` tant qu'autovacuum n'est pas passé, puis
    dérivantes), et un inventaire faux ne sert à rien. Huit seq scans une fois
    par semaine, la nuit, derrière le verrou de maintenance : c'est payable.

    Ordre des execute (contrat) : 1) les comptes, en **une seule** requête de
    sous-requêtes scalaires, 2) les tailles sur disque. Puis le bucket, **page
    par page** — on n'accumule que deux entiers par préfixe, jamais les clés.
    """
    columns = [
        select(func.count()).select_from(model).scalar_subquery().label(name)
        for name, model in INVENTORIED_TABLES
    ]
    columns.append(
        select(func.count())
        .select_from(Resource)
        .where(Resource.status == STATUS_PENDING)
        .scalar_subquery()
        .label("resources_pending")
    )
    rows = dict((await db.execute(select(*columns))).mappings().one())

    sizes = {row.name: row.bytes for row in (await db.execute(TABLE_SIZES_SQL))}
    tracked = {name for name, _ in INVENTORIED_TABLES}
    table_bytes = {name: size for name, size in sizes.items() if name in tracked}

    buckets: dict[str, dict[str, int]] = {}
    for prefix in S3_PREFIXES:
        objects = total_bytes = 0
        async for page in storage.iter_objects(prefix):
            objects += len(page)
            total_bytes += sum(obj.size for obj in page)
        buckets[prefix] = {"objects": objects, "bytes": total_bytes}

    detail = {
        "rows": rows,
        "table_bytes": table_bytes,
        "schema_bytes": sum(sizes.values()),
        "s3": buckets,
    }
    logger.info(
        "inventaire — base %s sur disque (%s), %s",
        _human_bytes(detail["schema_bytes"]),
        ", ".join(
            f"{name} {rows[name]} lignes / {_human_bytes(table_bytes.get(name, 0))}"
            for name in sorted(table_bytes, key=lambda n: -table_bytes[n])[:3]
        ),
        ", ".join(
            f"{prefix} {stats['objects']} objets / {_human_bytes(stats['bytes'])}"
            for prefix, stats in buckets.items()
        ),
    )
    return JobOutcome(
        count=sum(stats["objects"] for stats in buckets.values()), detail=detail
    )


async def missing_s3_objects(
    db: AsyncSession,
    storage: Storage,
    *,
    grace_days: int,
    max_checks: int,
    concurrency: int,
    cursor: str | None = None,
) -> JobOutcome:
    """Lignes ``available`` qui pointent un objet S3 **absent**.

    C'est la cohérence **inverse** de ``reconcile_s3_orphans`` : celle-ci part du
    bucket pour trouver ce que la base ne référence pas, celle-là part de la base
    pour trouver ce que le bucket n'a plus. Une ligne signalée ici sert une URL
    présignée qui rendra 404 au prof ou à l'élève.

    **Stratégie : curseur tournant + HEAD à concurrence bornée.** Les avatars
    d'abord, tous (quelques dizaines de lignes) ; puis une tranche de
    ``resources`` ordonnée par ``s3_key``, reprise là où la passe précédente
    s'était arrêtée. L'index unique ``uq_resources_s3_key`` en fait un parcours
    d'index, pas un tri. Une tranche plus courte que le budget = table épuisée,
    le curseur s'enroule.

    La direction inverse **n'est pas décomposable par page** comme l'est la
    réconciliation : la seule alternative (lister le bucket et compléter) exige
    de tenir toutes les clés de la base en mémoire, des dizaines de Mo sur un
    Pi. D'où le choix d'un coût en requêtes (``max_checks`` HEAD) contre une
    mémoire strictement bornée — et une couverture qui **n'est jamais complète
    en une passe**, ce qui justifie la cadence quotidienne.

    La grâce ne couvre pas une fenêtre d'upload (la confirmation a déjà fait un
    HEAD sur l'objet) : elle **stabilise le rapport**, en laissant hors champ ce
    qui vient d'être créé pendant une restauration ou un dérèglement d'horloge.

    Ordre des execute (contrat) : 1) les avatars, 2) la tranche de ressources.
    Aucune écriture, aucun commit, **aucune suppression** — jamais.
    """
    if max_checks <= 0:
        return JobOutcome(count=0, detail={"checked": 0, "missing": 0, "cursor": cursor})

    cutoff = datetime.now(UTC) - timedelta(days=grace_days)
    avatars = list(
        (
            await db.execute(
                select(User.avatar_s3_key).where(
                    User.avatar_s3_key.is_not(None),
                    User.avatar_status == STATUS_AVAILABLE,
                )
            )
        )
        .scalars()
        .all()
    )[:max_checks]

    budget = max_checks - len(avatars)
    resources: list[str] = []
    wrapped = False
    if budget > 0:
        condition = (Resource.status == STATUS_AVAILABLE) & (Resource.created_at < cutoff)
        if cursor:
            condition &= Resource.s3_key > cursor
        resources = list(
            (
                await db.execute(
                    select(Resource.s3_key)
                    .where(condition)
                    .order_by(Resource.s3_key)
                    .limit(budget)
                )
            )
            .scalars()
            .all()
        )
        # Tranche incomplète : on a touché le bout de la table, la prochaine
        # passe repart du début. Sinon on mémorise la dernière clé vue.
        wrapped = len(resources) < budget

    keys = avatars + resources
    missing = await _absent_keys(storage, keys, concurrency)
    for key in missing:
        logger.warning("objet S3 absent pour une ligne `available` : %s", key)

    next_cursor = None if wrapped or not resources else resources[-1]
    detail = {
        "checked": len(keys),
        "missing": len(missing),
        "missing_keys": missing,
        "cursor": next_cursor,
        "wrapped": wrapped,
    }
    logger.info(
        "objets S3 manquants — %d clé(s) vérifiée(s), %d absente(s)%s",
        len(keys),
        len(missing),
        " (table balayée en entier, le curseur repart du début)" if wrapped else "",
    )
    return JobOutcome(count=len(missing), detail=detail)


async def _absent_keys(storage: Storage, keys: list[str], concurrency: int) -> list[str]:
    """Les clés dont le HEAD ne trouve rien, à concurrence bornée.

    ``storage.head`` ne rend ``None`` que sur 404/NoSuchKey/NotFound : toute
    autre :class:`ClientError` **remonte** et fait échouer le job. Un bucket
    momentanément indisponible ne doit jamais se déguiser en « 2 000 objets
    manquants » — un faux rapport serait pire que pas de rapport.
    """
    if not keys:
        return []
    semaphore = asyncio.Semaphore(max(concurrency, 1))

    async def exists(key: str) -> tuple[str, bool]:
        async with semaphore:
            return key, await storage.head(key) is not None

    try:
        checked = await asyncio.gather(*(exists(key) for key in keys))
    except ClientError:
        logger.exception("HEAD S3 en échec — contrôle abandonné, aucun rapport écrit")
        raise
    return [key for key, found in checked if not found]
