"""Credential IA de l'utilisateur : lecture, écriture chiffrée, cascade.

Seul consommateur de :mod:`app.core.crypto` (confinement). L'ordre des
``execute`` de chaque fonction est stable et documenté : les tests le
rejouent avec une fausse session FIFO (voir tests/test_ai_credentials_api.py).

Cascade de résolution des appels IA (``effective_config``) :
config explicite de la requête > credential utilisateur déchiffré > ``None``
(le ``resolve_config`` d'AIClient applique alors le fallback serveur AI_*).
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
from dataclasses import dataclass
from datetime import UTC, date, datetime

from fastapi import HTTPException, status
from pydantic import SecretStr
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_credentials.schemas import (
    PROVIDERS_WITH_OPTIONAL_KEY,
    AIConnectionTestIn,
    AIConnectionTestRead,
    AICredentialsRead,
    AICredentialsUpdate,
    AIModelListIn,
    AIModelListRead,
)
from app.core import crypto
from app.core.ai import AIClient, AIProvider, AIRequestConfig, ChatMessage, list_models
from app.core.auth import AuthenticatedUser
from app.core.config import settings
from app.core.http import invalid, unavailable
from app.models.ai_daily_usage import AIDailyUsage
from app.models.user import User
from app.users import service as users_service


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


def _read(user: User, calls_today: int) -> AICredentialsRead:
    return AICredentialsRead(
        provider=user.ai_provider,
        model=user.ai_model,
        base_url=user.ai_base_url,
        api_key_set=user.ai_api_key_encrypted is not None,
        default_ai_available=bool(settings.AI_PROVIDER),
        daily_quota=_effective_quota(user),
        calls_today=calls_today,
        default_provider=settings.AI_PROVIDER or None,
        default_model=(settings.AI_MODEL or None) if settings.AI_PROVIDER else None,
    )


async def read_credentials(db: AsyncSession, user: User) -> AICredentialsRead:
    """Projection sûre du credential — 200 même sans config (tout null).

    Porte aussi l'état de l'IA par défaut (disponibilité + quota du jour),
    affiché par l'écran de réglages IA du front. Ordre des execute :
    1 select (usage du jour).
    """
    return _read(user, await _usage_for_day(db, user))


async def update_credentials(
    db: AsyncSession, user: User, payload: AICredentialsUpdate
) -> AICredentialsRead:
    """Enregistre le credential (remplacement provider/model/base_url).

    ``api_key`` absente = conserver le blob+sel existants ; fournie =
    re-chiffrement avec un NOUVEAU sel. Ni fournie ni existante alors que le
    provider l'exige → 422. Ordre des execute : mutation d'attributs de
    l'instance chargée par ``get_or_create_by_sub``, puis 1 select (usage du
    jour, pour la réponse), un commit.
    """
    if payload.api_key is not None:
        master_key = _master_key()
        salt = crypto.new_salt()
        user.ai_api_key_encrypted = crypto.encrypt_secret(
            payload.api_key.get_secret_value(), master_key, salt
        )
        user.ai_encryption_salt = salt
    elif user.ai_api_key_encrypted is None and payload.provider not in PROVIDERS_WITH_OPTIONAL_KEY:
        raise invalid(f"Clé API requise pour le provider {payload.provider.value}")

    user.ai_provider = payload.provider.value
    user.ai_model = payload.model
    user.ai_base_url = payload.base_url
    response = _read(user, await _usage_for_day(db, user))
    await db.commit()
    return response


def _decrypt_stored_key(user: User) -> SecretStr | None:
    """Clé enregistrée déchiffrée (``None`` sans clé) ; credential illisible
    (clé maître changée) → 422 « ré-enregistrez », JAMAIS un repli silencieux."""
    if user.ai_api_key_encrypted is None:
        return None
    try:
        return SecretStr(
            crypto.decrypt_secret(user.ai_api_key_encrypted, _master_key(), user.ai_encryption_salt)
        )
    except crypto.DecryptionError:
        raise invalid(
            "Identifiants IA illisibles — ré-enregistrez votre clé API dans les paramètres"
        ) from None


def _probe_api_key(
    user: User, provider: AIProvider, provided: SecretStr | None
) -> SecretStr | None:
    """Clé effective d'un test/listing : fournie, sinon clé enregistrée (même
    sémantique que le PUT), sinon 422 pour les providers qui l'exigent."""
    api_key = provided if provided is not None else _decrypt_stored_key(user)
    if api_key is None and provider not in PROVIDERS_WITH_OPTIONAL_KEY:
        raise invalid(f"Clé API requise pour le provider {provider.value}")
    return api_key


# Prompt du test de connexion : réponse d'un mot demandée, coût négligeable.
# Volontairement SANS max_tokens : un plafond minuscule fait échouer certains
# modèles « thinking » (le budget de raisonnement compte dans la sortie).
_TEST_PROMPT = "Réponds uniquement « ok »."
_TEST_TRACE_NAME = "ai-credentials-test"


async def test_connection(
    user: User, payload: AIConnectionTestIn, ai: AIClient
) -> AIConnectionTestRead:
    """Teste la config du formulaire par un mini-appel provider réel.

    Valide exactement ce que le PUT enregistrerait (``api_key`` omise = clé
    déjà enregistrée). BYO token intégral : jamais le fallback serveur
    ``AI_*``, donc jamais de quota consommé — et aucune écriture DB. Les
    échecs remontent en HTTPException déjà traduites par ``app/core/ai``
    (422 config, 400 clé refusée, 429, 503 injoignable).
    """
    config = AIRequestConfig(
        provider=payload.provider,
        model=payload.model,
        api_key=_probe_api_key(user, payload.provider, payload.api_key),
        base_url=payload.base_url,
    )
    await ai.complete(
        [ChatMessage(role="user", content=_TEST_PROMPT)],
        config,
        trace_name=_TEST_TRACE_NAME,
        user_id=user.sub,
    )
    return AIConnectionTestRead()


async def list_provider_models(user: User, payload: AIModelListIn) -> AIModelListRead:
    """Modèles proposés par le provider (auto-complétion du champ modèle).

    Même sémantique de clé que le test ; délégation à
    :func:`app.core.ai.list_models` (REST direct du provider, erreurs
    traduites). Aucune écriture DB, jamais de quota.
    """
    api_key = _probe_api_key(user, payload.provider, payload.api_key)
    return AIModelListRead(models=await list_models(payload.provider, api_key, payload.base_url))


async def delete_credentials(db: AsyncSession, user: User) -> None:
    """Efface tout le credential (les 5 colonnes à NULL) — idempotent."""
    user.ai_provider = None
    user.ai_model = None
    user.ai_base_url = None
    user.ai_api_key_encrypted = None
    user.ai_encryption_salt = None
    await db.commit()


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


async def effective_config(
    db: AsyncSession, auth: AuthenticatedUser, explicit: AIRequestConfig | None
) -> tuple[AIRequestConfig | None, QuotaTicket | None]:
    """Cascade : config explicite > credential utilisateur > None (fallback AI_*).

    Une config explicite court-circuite toute lecture DB. Un credential
    illisible (clé maître changée) → 422 explicite, JAMAIS un repli
    silencieux sur le fallback serveur : l'utilisateur croirait sa clé
    utilisée. Le repli sur le fallback serveur (retour ``None`` avec un
    ``AI_PROVIDER`` configuré) consomme le quota QUOTIDIEN d'IA par défaut
    (429 si épuisé) et retourne alors le :class:`QuotaTicket` à rembourser
    par l'appelant si l'appel provider échoue (``None`` dans tous les autres
    cas : rien n'a été consommé) ; sans fallback configuré, rien n'est
    consommé (le 422 de ``resolve_config`` suivra). Ordre des execute : ceux
    de ``get_or_create_by_sub`` (1 insert, 1 select) quand la config n'est
    pas explicite, + l'upsert de ``_consume_default_quota`` en cas de repli.
    """
    if explicit is not None:
        return explicit, None
    user = await users_service.get_or_create_by_sub(db, auth)
    if user.ai_provider is None:
        if settings.AI_PROVIDER:
            return None, await _consume_default_quota(db, user)
        return None, None
    api_key = _decrypt_stored_key(user)
    return AIRequestConfig(
        provider=AIProvider(user.ai_provider),
        model=user.ai_model,
        api_key=api_key,
        base_url=user.ai_base_url,
    ), None
