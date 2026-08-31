"""Schémas HTTP du credential IA de l'utilisateur.

Règle d'or (motif avatar_s3_key) : la clé API — chiffrée ou en clair — ne
figure dans AUCUN schéma de réponse ; seule sort la projection
``api_key_set: bool``. Pas de masque type ``sk-…abc`` : il faudrait
persister un fragment de clé en clair, affaiblissement refusé.
"""

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.core.ai import AIProvider

# Providers dont la clé API est facultative : ollama n'en a pas ;
# openai_compatible reçoit un placeholder côté client si absente.
PROVIDERS_WITH_OPTIONAL_KEY = frozenset({AIProvider.OLLAMA, AIProvider.OPENAI_COMPATIBLE})
# Providers acceptant une base_url ; openai_compatible l'exige.
PROVIDERS_WITH_BASE_URL = frozenset({AIProvider.OLLAMA, AIProvider.OPENAI_COMPATIBLE})


class AICredentialsRead(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_set: bool = False
    # IA par défaut (fallback serveur AI_*) : proposée ou non par ce serveur,
    # et où en est l'utilisateur dans son quota QUOTIDIEN (jour UTC).
    # ``daily_quota`` = plafond effectif résolu (users.ai_daily_call_quota
    # sinon AI_DEFAULT_DAILY_QUOTA), 0 = illimité.
    default_ai_available: bool = False
    daily_quota: int = 0
    calls_today: int = 0


def _check_key_not_blank(v: SecretStr | None) -> SecretStr | None:
    if v is not None and not v.get_secret_value().strip():
        raise ValueError(
            "api_key ne peut pas être vide ; omettre le champ pour conserver la clé enregistrée"
        )
    return v


def _check_base_url_per_provider(provider: AIProvider, base_url: str | None) -> None:
    if provider == AIProvider.OPENAI_COMPATIBLE and not base_url:
        raise ValueError("base_url est requise pour le provider openai_compatible")
    if base_url and provider not in PROVIDERS_WITH_BASE_URL:
        raise ValueError("base_url ne s'applique qu'aux providers ollama et openai_compatible")


class AICredentialsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: AIProvider
    model: str = Field(min_length=1, max_length=200)
    # None/absent = CONSERVER la clé déjà enregistrée (changer de modèle sans
    # re-saisie) ; fournie = re-chiffrement avec un nouveau sel.
    api_key: SecretStr | None = None
    base_url: str | None = Field(None, max_length=2000)

    @field_validator("api_key")
    @classmethod
    def _key_not_blank(cls, v: SecretStr | None) -> SecretStr | None:
        return _check_key_not_blank(v)

    @model_validator(mode="after")
    def _base_url_per_provider(self) -> "AICredentialsUpdate":
        _check_base_url_per_provider(self.provider, self.base_url)
        return self


class AIConnectionTestIn(AICredentialsUpdate):
    """Payload du test de connexion — mêmes champs et règles que le PUT.

    Le test valide exactement ce que le PUT enregistrerait, sémantique de la
    clé comprise : ``api_key`` omise = tester avec la clé déjà enregistrée.
    """


class AIConnectionTestRead(BaseModel):
    ok: bool = True


class AIModelListIn(BaseModel):
    """Payload du listing des modèles d'un provider — pas de ``model``, même
    sémantique de clé que le PUT (omise = clé déjà enregistrée)."""

    model_config = ConfigDict(extra="forbid")

    provider: AIProvider
    api_key: SecretStr | None = None
    base_url: str | None = Field(None, max_length=2000)

    @field_validator("api_key")
    @classmethod
    def _key_not_blank(cls, v: SecretStr | None) -> SecretStr | None:
        return _check_key_not_blank(v)

    @model_validator(mode="after")
    def _base_url_per_provider(self) -> "AIModelListIn":
        _check_base_url_per_provider(self.provider, self.base_url)
        return self


class AIModelListRead(BaseModel):
    models: list[str]
