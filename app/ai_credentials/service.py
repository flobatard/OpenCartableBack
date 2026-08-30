"""Credential IA de l'utilisateur : lecture, écriture chiffrée, cascade.

Seul consommateur de :mod:`app.core.crypto` (confinement). L'ordre des
``execute`` de chaque fonction est stable et documenté : les tests le
rejouent avec une fausse session FIFO (voir tests/test_ai_credentials_api.py).

Cascade de résolution des appels IA (``config_effective``) :
config explicite de la requête > credential utilisateur déchiffré > ``None``
(le ``resolve_config`` d'AIClient applique alors le fallback serveur AI_*).
Le repli sur le fallback serveur est le SEUL cas soumis au quota QUOTIDIEN
d'appels (``AI_DEFAULT_DAILY_QUOTA`` / ``users.ai_quota_appels``, comptage
par jour UTC dans la table ``ai_daily_usage``) : les appels BYO token
consomment la clé de l'utilisateur, jamais celle du serveur. Sémantique
**réservation + remboursement** : le quota est consommé atomiquement AVANT
l'appel provider (plafond dur, 429 avant le 200 du flux SSE) et le
consommateur rembourse via le :class:`QuotaTicket` si l'appel échoue
(``rembourser_quota_defaut``) — un échec provider est net-zéro.
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
    PROVIDERS_CLE_OPTIONNELLE,
    AICredentialsRead,
    AICredentialsUpdate,
)
from app.core import crypto
from app.core.ai import AIProvider, AIRequestConfig
from app.core.auth import AuthenticatedUser
from app.core.config import settings
from app.models.ai_daily_usage import AIDailyUsage
from app.models.user import User
from app.users import service as users_service


def _invalide(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _cle_maitre() -> bytes:
    """Clé maître décodée ; 503 si absente/invalide (misconfiguration serveur)."""
    try:
        return crypto.decoder_cle_maitre(settings.AI_CREDENTIALS_MASTER_KEY)
    except crypto.CleMaitreAbsente:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chiffrement des credentials IA non configuré sur ce serveur",
        ) from None


def _quota_effectif(user: User) -> int:
    """Plafond quotidien résolu : ``ai_quota_appels`` sinon le défaut config."""
    return (
        user.ai_quota_appels
        if user.ai_quota_appels is not None
        else settings.AI_DEFAULT_DAILY_QUOTA
    )


async def _usage_du_jour(db: AsyncSession, user: User) -> int:
    """Appels déjà servis par l'IA par défaut aujourd'hui (jour UTC), 0 sans ligne."""
    result = await db.execute(
        select(AIDailyUsage.appels).where(
            AIDailyUsage.user_id == user.id,
            AIDailyUsage.jour == datetime.now(UTC).date(),
        )
    )
    return result.scalars().one_or_none() or 0


def _read(user: User, appels_aujourdhui: int) -> AICredentialsRead:
    return AICredentialsRead(
        provider=user.ai_provider,
        model=user.ai_model,
        base_url=user.ai_base_url,
        api_key_definie=user.ai_api_key_chiffree is not None,
        ia_defaut_disponible=bool(settings.AI_PROVIDER),
        quota_quotidien=_quota_effectif(user),
        appels_aujourdhui=appels_aujourdhui,
    )


async def read_credentials(db: AsyncSession, user: User) -> AICredentialsRead:
    """Projection sûre du credential — 200 même sans config (tout null).

    Porte aussi l'état de l'IA par défaut (disponibilité + quota du jour),
    affiché par l'écran de réglages IA du front. Ordre des execute :
    1 select (usage du jour).
    """
    return _read(user, await _usage_du_jour(db, user))


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
        cle_maitre = _cle_maitre()
        sel = crypto.nouveau_sel()
        user.ai_api_key_chiffree = crypto.chiffrer_secret(
            payload.api_key.get_secret_value(), cle_maitre, sel
        )
        user.ai_chiffrement_sel = sel
    elif user.ai_api_key_chiffree is None and payload.provider not in PROVIDERS_CLE_OPTIONNELLE:
        raise _invalide(f"Clé API requise pour le provider {payload.provider.value}")

    user.ai_provider = payload.provider.value
    user.ai_model = payload.model
    user.ai_base_url = payload.base_url
    reponse = _read(user, await _usage_du_jour(db, user))
    await db.commit()
    return reponse


async def delete_credentials(db: AsyncSession, user: User) -> None:
    """Efface tout le credential (les 5 colonnes à NULL) — idempotent."""
    user.ai_provider = None
    user.ai_model = None
    user.ai_base_url = None
    user.ai_api_key_chiffree = None
    user.ai_chiffrement_sel = None
    await db.commit()


@dataclass(frozen=True)
class QuotaTicket:
    """Réservation du quota d'IA par défaut, à rembourser si l'appel échoue.

    Capture la ligne ``(user_id, jour)`` réellement consommée : un échec qui
    traverse minuit UTC rembourse le bon jour.
    """

    user_id: uuid.UUID
    jour: date


async def _consommer_quota_defaut(db: AsyncSession, user: User) -> QuotaTicket:
    """Consomme un appel du quota QUOTIDIEN de l'IA par défaut — 429 si épuisé.

    Quota par jour (UTC) : ``ai_quota_appels`` si renseigné, sinon
    ``AI_DEFAULT_DAILY_QUOTA`` ; 0 = illimité (l'appel est quand même compté,
    à des fins de statistiques). Upsert **atomique** sur ``ai_daily_usage``
    (PK ``user_id, jour``) : le plafond est dans le WHERE du DO UPDATE, deux
    appels concurrents ne peuvent donc pas le dépasser — rowcount 0 = quota
    du jour épuisé, rien n'a été écrit. Sémantique réservation : consommé
    AVANT l'appel provider, remboursé par le consommateur via le ticket
    retourné si l'appel échoue. Ordre des execute : 1 insert (upsert), un
    commit.
    """
    quota = _quota_effectif(user)
    jour = datetime.now(UTC).date()
    stmt = pg_insert(AIDailyUsage).values(user_id=user.id, jour=jour, appels=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "jour"],
        set_={"appels": AIDailyUsage.appels + 1},
        where=(AIDailyUsage.appels < quota) if quota > 0 else None,
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
    return QuotaTicket(user_id=user.id, jour=jour)


async def rembourser_quota_defaut(db: AsyncSession, ticket: QuotaTicket) -> None:
    """Rembourse une réservation dont l'appel provider a échoué — best-effort.

    UPDATE décrémental sur la ligne du ticket, garde ``appels > 0`` (jamais
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
                AIDailyUsage.jour == ticket.jour,
                AIDailyUsage.appels > 0,
            )
            .values(appels=AIDailyUsage.appels - 1)
        )
        await db.commit()
    except Exception:  # pragma: no cover - filet best-effort
        with contextlib.suppress(Exception):
            await db.rollback()


async def config_effective(
    db: AsyncSession, auth: AuthenticatedUser, explicite: AIRequestConfig | None
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
    pas explicite, + l'upsert de ``_consommer_quota_defaut`` en cas de repli.
    """
    if explicite is not None:
        return explicite, None
    user = await users_service.get_or_create_by_sub(db, auth)
    if user.ai_provider is None:
        if settings.AI_PROVIDER:
            return None, await _consommer_quota_defaut(db, user)
        return None, None
    api_key: SecretStr | None = None
    if user.ai_api_key_chiffree is not None:
        try:
            api_key = SecretStr(
                crypto.dechiffrer_secret(
                    user.ai_api_key_chiffree, _cle_maitre(), user.ai_chiffrement_sel
                )
            )
        except crypto.ErreurDechiffrement:
            raise _invalide(
                "Identifiants IA illisibles — ré-enregistrez votre clé API dans les paramètres"
            ) from None
    return AIRequestConfig(
        provider=AIProvider(user.ai_provider),
        model=user.ai_model,
        api_key=api_key,
        base_url=user.ai_base_url,
    ), None
