"""Credential IA de l'utilisateur : lecture, écriture chiffrée, cascade.

Seul consommateur de :mod:`app.core.crypto` (confinement). L'ordre des
``execute`` de chaque fonction est stable et documenté : les tests le
rejouent avec une fausse session FIFO (voir tests/test_ai_credentials_api.py).

Cascade de résolution des appels IA (``config_effective``) :
config explicite de la requête > credential utilisateur déchiffré > ``None``
(le ``resolve_config`` d'AIClient applique alors le fallback serveur AI_*).
"""

from fastapi import HTTPException, status
from pydantic import SecretStr
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


def _read(user: User) -> AICredentialsRead:
    return AICredentialsRead(
        provider=user.ai_provider,
        model=user.ai_model,
        base_url=user.ai_base_url,
        api_key_definie=user.ai_api_key_chiffree is not None,
    )


def read_credentials(user: User) -> AICredentialsRead:
    """Projection sûre du credential — 200 même sans config (tout null)."""
    return _read(user)


async def update_credentials(
    db: AsyncSession, user: User, payload: AICredentialsUpdate
) -> AICredentialsRead:
    """Enregistre le credential (remplacement provider/model/base_url).

    ``api_key`` absente = conserver le blob+sel existants ; fournie =
    re-chiffrement avec un NOUVEAU sel. Ni fournie ni existante alors que le
    provider l'exige → 422. Ordre des execute : aucun (mutation d'attributs
    de l'instance chargée par ``get_or_create_by_sub``), un commit.
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
    reponse = _read(user)
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


async def config_effective(
    db: AsyncSession, auth: AuthenticatedUser, explicite: AIRequestConfig | None
) -> AIRequestConfig | None:
    """Cascade : config explicite > credential utilisateur > None (fallback AI_*).

    Une config explicite court-circuite toute lecture DB. Un credential
    illisible (clé maître changée) → 422 explicite, JAMAIS un repli
    silencieux sur le fallback serveur : l'utilisateur croirait sa clé
    utilisée. Ordre des execute : ceux de ``get_or_create_by_sub``
    (1 insert, 2 select) quand la config n'est pas explicite.
    """
    if explicite is not None:
        return explicite
    user = await users_service.get_or_create_by_sub(db, auth)
    if user.ai_provider is None:
        return None
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
    )
