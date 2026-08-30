"""Schémas HTTP du credential IA de l'utilisateur.

Règle d'or (motif avatar_s3_key) : la clé API — chiffrée ou en clair — ne
figure dans AUCUN schéma de réponse ; seule sort la projection
``api_key_definie: bool``. Pas de masque type ``sk-…abc`` : il faudrait
persister un fragment de clé en clair, affaiblissement refusé.
"""

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.core.ai import AIProvider

# Providers dont la clé API est facultative : ollama n'en a pas ;
# openai_compatible reçoit un placeholder côté client si absente.
PROVIDERS_CLE_OPTIONNELLE = frozenset({AIProvider.OLLAMA, AIProvider.OPENAI_COMPATIBLE})
# Providers acceptant une base_url ; openai_compatible l'exige.
PROVIDERS_AVEC_BASE_URL = frozenset({AIProvider.OLLAMA, AIProvider.OPENAI_COMPATIBLE})


class AICredentialsRead(BaseModel):
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_definie: bool = False


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
    def _cle_non_blanche(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and not v.get_secret_value().strip():
            raise ValueError(
                "api_key ne peut pas être vide ; omettre le champ pour conserver la clé enregistrée"
            )
        return v

    @model_validator(mode="after")
    def _base_url_selon_provider(self) -> "AICredentialsUpdate":
        if self.provider == AIProvider.OPENAI_COMPATIBLE and not self.base_url:
            raise ValueError("base_url est requise pour le provider openai_compatible")
        if self.base_url and self.provider not in PROVIDERS_AVEC_BASE_URL:
            raise ValueError("base_url ne s'applique qu'aux providers ollama et openai_compatible")
        return self
