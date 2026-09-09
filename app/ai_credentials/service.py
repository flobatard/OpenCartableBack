"""Configurations IA nommées de l'utilisateur : CRUD chiffré, bascule, cascade.

Seul consommateur de :mod:`app.core.crypto` (confinement). L'ordre des
``execute`` de chaque fonction est stable et documenté : les tests le
rejouent avec une fausse session FIFO (voir tests/test_ai_credentials_api.py).

Plusieurs configurations par utilisateur (table ``ai_configurations``,
plafond ``MAX_CONFIGURATIONS``), au plus une **active** (index partiel unique
en base) ; aucune active = IA par défaut du serveur. Créer une configuration
l'active ; supprimer l'active ramène à l'IA par défaut.

Cascade de résolution des appels IA (``effective_config``) :
config explicite de la requête > configuration ACTIVE déchiffrée > ``None``
(le ``resolve_config`` d'AIClient applique alors le fallback serveur AI_*).
La configuration porte aussi les préférences de raisonnement de
l'utilisateur (``reasoning`` / ``reasoning_effort``, règles par provider en
422 dans les schémas) : elles voyagent dans l'``AIRequestConfig`` qu'elle
produit ; le fallback serveur porte les siennes (settings ``AI_REASONING*``,
posés par l'opérateur, résolus par ``AIClient.resolve_config``).
Le repli sur le fallback serveur est le SEUL cas soumis au quota QUOTIDIEN
d'appels (``AI_DEFAULT_DAILY_QUOTA`` / ``users.ai_daily_call_quota``, comptage
par jour UTC dans la table ``ai_daily_usage``) : les appels BYO token
consomment la clé de l'utilisateur, jamais celle du serveur. Sémantique
**réservation + remboursement** : le quota est consommé atomiquement AVANT
l'appel provider (plafond dur, 429 avant le 200 du flux SSE) et le
consommateur rembourse via le :class:`QuotaTicket` si l'appel échoue
(``refund_default_quota``) — un échec provider est net-zéro.
"""

import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime

from fastapi import HTTPException, status
from pydantic import SecretStr
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_credentials.schemas import (
    PROVIDERS_WITH_OPTIONAL_KEY,
    AIConfigurationIn,
    AIConfigurationRead,
    AIConnectionTestIn,
    AIConnectionTestRead,
    AICredentialsRead,
    AIModelListIn,
    AIModelListRead,
    ReasoningOptionsIn,
    ReasoningOptionsRead,
    options_for,
)
from app.core import crypto
from app.core.ai import (
    AIClient,
    AIProvider,
    AIRequestConfig,
    ChatMessage,
    list_models,
    reasoning_options,
)
from app.core.auth import AuthenticatedUser
from app.core.config import settings
from app.core.database import touch
from app.core.http import invalid, not_found, unavailable
from app.models.ai_configuration import AIConfiguration
from app.models.ai_daily_usage import AIDailyUsage
from app.models.user import User
from app.users import service as users_service

# Plafond de configurations par utilisateur (miroir front AI_CONFIGURATIONS_MAX).
MAX_CONFIGURATIONS = 10
NOT_FOUND_DETAIL = "Configuration introuvable"


def _master_key() -> bytes:
    """Clé maître décodée ; 503 si absente/invalide (misconfiguration serveur)."""
    try:
        return crypto.decode_master_key(settings.AI_CREDENTIALS_MASTER_KEY)
    except crypto.MasterKeyMissing:
        raise unavailable(
            "Chiffrement des credentials IA non configuré sur ce serveur"
        ) from None


def _effective_quota(user: User) -> int:
    """Plafond quotidien résolu : ``ai_daily_call_quota`` sinon le défaut config."""
    return (
        user.ai_daily_call_quota
        if user.ai_daily_call_quota is not None
        else settings.AI_DEFAULT_DAILY_QUOTA
    )


async def _usage_for_day(db: AsyncSession, user: User) -> int:
    """Appels déjà servis par l'IA par défaut aujourd'hui (jour UTC), 0 sans ligne."""
    result = await db.execute(
        select(AIDailyUsage.calls).where(
            AIDailyUsage.user_id == user.id,
            AIDailyUsage.day == datetime.now(UTC).date(),
        )
    )
    return result.scalars().one_or_none() or 0


async def _list_configurations(db: AsyncSession, user: User) -> list[AIConfiguration]:
    """Toutes les configurations de l'utilisateur, de la plus ancienne à la
    plus récente (tri stable ``created_at, id``). 1 select."""
    result = await db.execute(
        select(AIConfiguration)
        .where(AIConfiguration.user_id == user.id)
        .order_by(AIConfiguration.created_at, AIConfiguration.id)
    )
    return list(result.scalars().all())


def _configuration_read(config: AIConfiguration) -> AIConfigurationRead:
    return AIConfigurationRead(
        id=config.id,
        name=config.name,
        provider=config.provider,
        model=config.model,
        base_url=config.base_url,
        api_key_set=config.api_key_encrypted is not None,
        reasoning=config.reasoning,
        reasoning_effort=config.reasoning_effort,
        reasoning_options=options_for(config.provider, config.model),
    )


def _active_id(configurations: list[AIConfiguration]) -> uuid.UUID | None:
    return next((c.id for c in configurations if c.is_active), None)


def _read(
    user: User,
    configurations: list[AIConfiguration],
    calls_today: int,
    active_id: uuid.UUID | None,
) -> AICredentialsRead:
    """Enveloppe. ``active_id`` est passé explicitement : après une bascule ou
    une création, les instances chargées ne reflètent pas encore l'UPDATE de
    désactivation exécuté en Core."""
    return AICredentialsRead(
        configurations=[_configuration_read(c) for c in configurations],
        active_id=active_id,
        default_ai_available=bool(settings.AI_PROVIDER),
        daily_quota=_effective_quota(user),
        calls_today=calls_today,
        default_provider=settings.AI_PROVIDER or None,
        default_model=(settings.AI_MODEL or None) if settings.AI_PROVIDER else None,
    )


async def read_credentials(db: AsyncSession, user: User) -> AICredentialsRead:
    """Enveloppe des configurations — 200 même sans aucune (liste vide).

    Porte aussi l'état de l'IA par défaut (disponibilité + quota du jour),
    affiché par l'écran de réglages IA du front. Ordre des execute :
    1) configurations, 2) usage du jour.
    """
    configurations = await _list_configurations(db, user)
    calls_today = await _usage_for_day(db, user)
    return _read(user, configurations, calls_today, _active_id(configurations))


def _find(configurations: list[AIConfiguration], config_id: uuid.UUID) -> AIConfiguration:
    """La configuration d'id donné parmi celles de l'utilisateur — 404 sinon
    (une configuration d'autrui est introuvable, jamais interdite)."""
    for config in configurations:
        if config.id == config_id:
            return config
    raise not_found(NOT_FOUND_DETAIL)


def _apply_key(config: AIConfiguration, payload: AIConfigurationIn) -> None:
    """Applique la sémantique de la clé : fournie = re-chiffrement avec un
    NOUVEAU sel ; absente = conserver blob+sel ; ni fournie ni existante alors
    que le provider l'exige → 422."""
    if payload.api_key is not None:
        master_key = _master_key()
        salt = crypto.new_salt()
        config.api_key_encrypted = crypto.encrypt_secret(
            payload.api_key.get_secret_value(), master_key, salt
        )
        config.encryption_salt = salt
    elif config.api_key_encrypted is None and payload.provider not in PROVIDERS_WITH_OPTIONAL_KEY:
        raise invalid(f"Clé API requise pour le provider {payload.provider.value}")


def _apply_fields(config: AIConfiguration, payload: AIConfigurationIn) -> None:
    config.name = payload.name
    config.provider = payload.provider.value
    config.model = payload.model
    config.base_url = payload.base_url
    config.reasoning = payload.reasoning
    config.reasoning_effort = payload.reasoning_effort


async def _deactivate_all(db: AsyncSession, user: User) -> None:
    """UPDATE Core immédiat : l'index partiel unique n'est pas différable, la
    désactivation doit précéder toute activation dans la même transaction."""
    await db.execute(
        update(AIConfiguration)
        .where(AIConfiguration.user_id == user.id, AIConfiguration.is_active.is_(True))
        .values(is_active=False)
    )


async def create_configuration(
    db: AsyncSession, user: User, payload: AIConfigurationIn
) -> AICredentialsRead:
    """Crée une configuration nommée et l'ACTIVE (ce qu'on vient d'enregistrer
    est utilisé tout de suite ; revenir à l'IA par défaut reste un clic).

    Clé requise à la création pour les providers qui l'exigent (422). Plafond
    ``MAX_CONFIGURATIONS`` (422). Ordre des execute : 1) configurations
    (plafond + liste de la réponse), 2) update de désactivation, 3) insert de
    la ligne (timestamps posés en Python, sans RETURNING), 4) usage du jour,
    un commit. Réponse construite avant le commit.
    """
    configurations = await _list_configurations(db, user)
    if len(configurations) >= MAX_CONFIGURATIONS:
        raise invalid(f"Au plus {MAX_CONFIGURATIONS} configurations IA par utilisateur")
    now = datetime.now(UTC)
    config = AIConfiguration(
        id=uuid.uuid4(),
        user_id=user.id,
        api_key_encrypted=None,
        encryption_salt=None,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    _apply_key(config, payload)
    _apply_fields(config, payload)
    await _deactivate_all(db, user)
    await db.execute(
        insert(AIConfiguration).values(
            id=config.id,
            user_id=config.user_id,
            name=config.name,
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            api_key_encrypted=config.api_key_encrypted,
            encryption_salt=config.encryption_salt,
            reasoning=config.reasoning,
            reasoning_effort=config.reasoning_effort,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
    )
    calls_today = await _usage_for_day(db, user)
    response = _read(user, [*configurations, config], calls_today, config.id)
    await db.commit()
    return response


async def update_configuration(
    db: AsyncSession, user: User, config_id: uuid.UUID, payload: AIConfigurationIn
) -> AICredentialsRead:
    """Remplace une configuration (nom, provider/modèle/base_url, préférences
    de raisonnement) sans toucher à son statut actif.

    ``api_key`` absente = conserver le blob+sel existants ; fournie =
    re-chiffrement avec un NOUVEAU sel ; ni fournie ni existante alors que le
    provider l'exige → 422. Ordre des execute : 1) configurations (404 si la
    cible n'en fait pas partie), mutation d'attributs de la cible chargée,
    2) usage du jour, un commit.
    """
    configurations = await _list_configurations(db, user)
    config = _find(configurations, config_id)
    _apply_key(config, payload)
    _apply_fields(config, payload)
    touch(config)
    calls_today = await _usage_for_day(db, user)
    response = _read(user, configurations, calls_today, _active_id(configurations))
    await db.commit()
    return response


async def delete_configuration(db: AsyncSession, user: User, config_id: uuid.UUID) -> None:
    """Supprime une configuration (seule façon d'effacer sa clé). Supprimer
    l'active ramène à l'IA par défaut. Ordre des execute : 1) configurations
    (404 si absente), 2) delete, un commit.
    """
    configurations = await _list_configurations(db, user)
    config = _find(configurations, config_id)
    await db.execute(
        delete(AIConfiguration).where(
            AIConfiguration.id == config.id, AIConfiguration.user_id == user.id
        )
    )
    await db.commit()


async def set_active_configuration(
    db: AsyncSession, user: User, config_id: uuid.UUID | None
) -> AICredentialsRead:
    """Bascule : ``config_id`` = la configuration à utiliser (404 si absente),
    ``None`` = revenir à l'IA par défaut.

    Deux instructions séparées (désactivation Core puis activation ORM de la
    cible) : l'index partiel unique n'est pas différable, un seul UPDATE
    ``is_active = (id = :x)`` pourrait le violer en cours de route. Ordre des
    execute : 1) configurations, 2) update de désactivation, 3) usage du
    jour, un commit (l'activation part au flush).
    """
    configurations = await _list_configurations(db, user)
    target = _find(configurations, config_id) if config_id is not None else None
    await _deactivate_all(db, user)
    if target is not None:
        target.is_active = True
        touch(target)
    calls_today = await _usage_for_day(db, user)
    response = _read(user, configurations, calls_today, target.id if target else None)
    await db.commit()
    return response


def _decrypt_stored_key(config: AIConfiguration) -> SecretStr | None:
    """Clé enregistrée déchiffrée (``None`` sans clé) ; configuration illisible
    (clé maître changée) → 422 « ré-enregistrez », JAMAIS un repli silencieux."""
    if config.api_key_encrypted is None:
        return None
    try:
        return SecretStr(
            crypto.decrypt_secret(
                config.api_key_encrypted, _master_key(), config.encryption_salt
            )
        )
    except crypto.DecryptionError:
        raise invalid(
            "Identifiants IA illisibles — ré-enregistrez votre clé API dans les paramètres"
        ) from None


async def _probe_api_key(
    db: AsyncSession,
    user: User,
    provider: AIProvider,
    provided: SecretStr | None,
    config_id: uuid.UUID | None,
) -> SecretStr | None:
    """Clé effective d'un test/listing : fournie, sinon clé enregistrée de la
    configuration ``config_id`` (1 select, 404 si absente), sinon aucune —
    422 pour les providers qui l'exigent."""
    api_key = provided
    if api_key is None and config_id is not None:
        result = await db.execute(
            select(AIConfiguration).where(
                AIConfiguration.id == config_id, AIConfiguration.user_id == user.id
            )
        )
        config = result.scalars().one_or_none()
        if config is None:
            raise not_found(NOT_FOUND_DETAIL)
        api_key = _decrypt_stored_key(config)
    if api_key is None and provider not in PROVIDERS_WITH_OPTIONAL_KEY:
        raise invalid(f"Clé API requise pour le provider {provider.value}")
    return api_key


# Prompt du test de connexion : réponse d'un mot demandée, coût négligeable.
# Volontairement SANS max_tokens : un plafond minuscule fait échouer certains
# modèles « thinking » (le budget de raisonnement compte dans la sortie).
_TEST_PROMPT = "Réponds uniquement « ok »."
_TEST_TRACE_NAME = "ai-credentials-test"


async def test_connection(
    db: AsyncSession, user: User, payload: AIConnectionTestIn, ai: AIClient
) -> AIConnectionTestRead:
    """Teste la config du formulaire par un mini-appel provider réel.

    Valide exactement ce que l'écriture enregistrerait (``api_key`` omise +
    ``config_id`` = clé déjà enregistrée de cette configuration). BYO token
    intégral : jamais le fallback serveur ``AI_*``, donc jamais de quota
    consommé — et aucune écriture DB (au plus 1 select, celui de la clé). Les
    échecs remontent en HTTPException déjà traduites par ``app/core/ai``
    (422 config, 400 clé refusée, 429, 503 injoignable).
    """
    config = AIRequestConfig(
        provider=payload.provider,
        model=payload.model,
        api_key=await _probe_api_key(
            db, user, payload.provider, payload.api_key, payload.config_id
        ),
        base_url=payload.base_url,
        reasoning=payload.reasoning,
        reasoning_effort=payload.reasoning_effort,
    )
    await ai.complete(
        [ChatMessage(role="user", content=_TEST_PROMPT)],
        config,
        trace_name=_TEST_TRACE_NAME,
        user_id=user.sub,
    )
    return AIConnectionTestRead()


async def list_provider_models(
    db: AsyncSession, user: User, payload: AIModelListIn
) -> AIModelListRead:
    """Modèles proposés par le provider (auto-complétion du champ modèle).

    Même sémantique de clé que le test ; délégation à
    :func:`app.core.ai.list_models` (REST direct du provider, erreurs
    traduites). Aucune écriture DB, jamais de quota.
    """
    api_key = await _probe_api_key(
        db, user, payload.provider, payload.api_key, payload.config_id
    )
    return AIModelListRead(models=await list_models(payload.provider, api_key, payload.base_url))


def read_reasoning_options(payload: ReasoningOptionsIn) -> ReasoningOptionsRead:
    """Options de raisonnement du catalogue pour le couple saisi — pur (aucune
    lecture DB, aucun appel provider) ; un modèle inconnu reçoit les options
    génériques du provider avec ``known=False``."""
    return ReasoningOptionsRead.from_options(reasoning_options(payload.provider, payload.model))


@dataclass(frozen=True)
class QuotaTicket:
    """Réservation du quota d'IA par défaut, à rembourser si l'appel échoue.

    Capture la ligne ``(user_id, day)`` réellement consommée : un échec qui
    traverse minuit UTC rembourse le bon jour.
    """

    user_id: uuid.UUID
    day: date


async def _consume_default_quota(db: AsyncSession, user: User) -> QuotaTicket:
    """Consomme un appel du quota QUOTIDIEN de l'IA par défaut — 429 si épuisé.

    Quota par jour (UTC) : ``ai_daily_call_quota`` si renseigné, sinon
    ``AI_DEFAULT_DAILY_QUOTA`` ; 0 = illimité (l'appel est quand même compté,
    à des fins de statistiques). Upsert **atomique** sur ``ai_daily_usage``
    (PK ``user_id, day``) : le plafond est dans le WHERE du DO UPDATE, deux
    appels concurrents ne peuvent donc pas le dépasser — rowcount 0 = quota
    du jour épuisé, rien n'a été écrit. Sémantique réservation : consommé
    AVANT l'appel provider, remboursé par le consommateur via le ticket
    retourné si l'appel échoue. Ordre des execute : 1 insert (upsert), un
    commit.
    """
    quota = _effective_quota(user)
    day = datetime.now(UTC).date()
    stmt = pg_insert(AIDailyUsage).values(user_id=user.id, day=day, calls=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "day"],
        set_={"calls": AIDailyUsage.calls + 1},
        where=(AIDailyUsage.calls < quota) if quota > 0 else None,
    )
    result = await db.execute(stmt)
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "Quota quotidien d'appels à l'IA par défaut atteint — réessayez "
                "demain ou enregistrez votre propre clé API dans les paramètres"
            ),
        )
    return QuotaTicket(user_id=user.id, day=day)


async def refund_default_quota(db: AsyncSession, ticket: QuotaTicket) -> None:
    """Rembourse une réservation dont l'appel provider a échoué — best-effort.

    UPDATE décrémental sur la ligne du ticket, garde ``calls > 0`` (jamais
    négatif). Toute erreur DB est avalée (rollback silencieux) : le
    remboursement ne doit JAMAIS masquer l'erreur d'origine du provider — au
    pire un appel échoué reste compté. Ordre des execute : 1 update, un
    commit.
    """
    try:
        await db.execute(
            update(AIDailyUsage)
            .where(
                AIDailyUsage.user_id == ticket.user_id,
                AIDailyUsage.day == ticket.day,
                AIDailyUsage.calls > 0,
            )
            .values(calls=AIDailyUsage.calls - 1)
        )
        await db.commit()
    except Exception:  # pragma: no cover - filet best-effort
        with contextlib.suppress(Exception):
            await db.rollback()


@contextlib.asynccontextmanager
async def refund_on_error(
    db: AsyncSession, ticket: QuotaTicket | None
) -> AsyncIterator[None]:
    """Rembourse le ticket si le bloc lève, puis re-lève.

    Pour les échecs EAGER (cascade, validation du client IA, appel classique) :
    un flux déjà entamé applique sa propre règle — remboursé seulement
    avant le premier token, dans son encodeur.
    """
    try:
        yield
    except Exception:
        if ticket is not None:
            await refund_default_quota(db, ticket)
        raise


async def effective_config(
    db: AsyncSession, auth: AuthenticatedUser, explicit: AIRequestConfig | None
) -> tuple[AIRequestConfig | None, QuotaTicket | None]:
    """Cascade : config explicite > configuration active > None (fallback AI_*).

    Une config explicite court-circuite toute lecture DB. Une configuration
    illisible (clé maître changée) → 422 explicite, JAMAIS un repli
    silencieux sur le fallback serveur : l'utilisateur croirait sa clé
    utilisée. Le repli sur le fallback serveur (retour ``None`` avec un
    ``AI_PROVIDER`` configuré) consomme le quota QUOTIDIEN d'IA par défaut
    (429 si épuisé) et retourne alors le :class:`QuotaTicket` à rembourser
    par l'appelant si l'appel provider échoue (``None`` dans tous les autres
    cas : rien n'a été consommé) ; sans fallback configuré, rien n'est
    consommé (le 422 de ``resolve_config`` suivra). Ordre des execute : ceux
    de ``get_or_create_by_sub`` (1 insert, 1 select) quand la config n'est
    pas explicite, + 1 select (configuration active, ou aucune), + l'upsert
    de ``_consume_default_quota`` en cas de repli.
    """
    if explicit is not None:
        return explicit, None
    user = await users_service.get_or_create_by_sub(db, auth)
    result = await db.execute(
        select(AIConfiguration).where(
            AIConfiguration.user_id == user.id, AIConfiguration.is_active.is_(True)
        )
    )
    active = result.scalars().one_or_none()
    if active is None:
        if settings.AI_PROVIDER:
            return None, await _consume_default_quota(db, user)
        return None, None
    api_key = _decrypt_stored_key(active)
    return AIRequestConfig(
        provider=AIProvider(active.provider),
        model=active.model,
        api_key=api_key,
        base_url=active.base_url,
        reasoning=active.reasoning,
        reasoning_effort=active.reasoning_effort,
    ), None
