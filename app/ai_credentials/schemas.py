"""Schémas HTTP des configurations IA nommées de l'utilisateur.

Règle d'or (motif avatar_s3_key) : la clé API — chiffrée ou en clair — ne
figure dans AUCUN schéma de réponse ; seule sort la projection
``api_key_set: bool``. Pas de masque type ``sk-…abc`` : il faudrait
persister un fragment de clé en clair, affaiblissement refusé.

La ressource est une collection : ``AICredentialsRead`` est l'**enveloppe**
(liste des configurations, id de l'active, état de l'IA par défaut) renvoyée
par le GET et par toute route mutante — le front remplace son état d'un bloc.
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.core.ai import (
    REASONING_EFFORT_MAX_LENGTH,
    AIProvider,
    ReasoningOptions,
    check_reasoning_support,
    reasoning_options,
)

# Providers dont la clé API est facultative : ollama n'en a pas ;
# openai_compatible reçoit un placeholder côté client si absente.
PROVIDERS_WITH_OPTIONAL_KEY = frozenset({AIProvider.OLLAMA, AIProvider.OPENAI_COMPATIBLE})
# Providers acceptant une base_url ; openai_compatible l'exige.
PROVIDERS_WITH_BASE_URL = frozenset({AIProvider.OLLAMA, AIProvider.OPENAI_COMPATIBLE})

NAME_MAX_LENGTH = 100


class ReasoningOptionsRead(BaseModel):
    """Options de raisonnement à proposer pour un couple (provider, modèle) —
    projection de :class:`app.core.ai.ReasoningOptions` (catalogue back)."""

    # Sous-ensemble de ["on", "off"] ; vide = pas de bascule.
    toggle: list[str] = Field(default_factory=list)
    # Niveaux natifs proposés (ordre croissant) ; vide = pas d'effort.
    efforts: list[str] = Field(default_factory=list)
    # Modèle reconnu par le catalogue ; sinon options génériques du provider.
    known: bool = False

    @classmethod
    def from_options(cls, options: ReasoningOptions) -> "ReasoningOptionsRead":
        return cls(toggle=list(options.toggle), efforts=list(options.efforts), known=options.known)


def options_for(provider: str | None, model: str | None) -> ReasoningOptionsRead:
    """Options du catalogue pour une configuration (rien sans provider/modèle)."""
    if not provider or not model:
        return ReasoningOptionsRead()
    try:
        return ReasoningOptionsRead.from_options(reasoning_options(AIProvider(provider), model))
    except ValueError:  # provider inconnu en base : jamais bloquant pour un GET
        return ReasoningOptionsRead()


class AIConfigurationRead(BaseModel):
    """Une configuration nommée, sans sa clé. ``is_active`` n'y figure pas :
    ``active_id`` de l'enveloppe est la seule source."""

    id: uuid.UUID
    name: str
    provider: str
    model: str
    base_url: str | None = None
    api_key_set: bool = False
    # Préférences de raisonnement enregistrées avec la configuration (null =
    # défaut du provider/modèle). ``reasoning_effort`` relu en ``str`` : une
    # valeur inattendue en base ne doit jamais faire échouer un GET.
    reasoning: bool | None = None
    reasoning_effort: str | None = None
    # Options à proposer pour le couple enregistré (pied du chat, état initial
    # du formulaire) ; le formulaire re-sonde POST /reasoning-options quand le
    # provider ou le modèle change.
    reasoning_options: ReasoningOptionsRead = Field(default_factory=ReasoningOptionsRead)


class AICredentialsRead(BaseModel):
    """Enveloppe : toutes les configurations de l'utilisateur, l'active, et
    l'état de l'IA par défaut."""

    # Ordre stable : de la plus ancienne à la plus récente.
    configurations: list[AIConfigurationRead] = Field(default_factory=list)
    # Configuration utilisée par les chats et le tuteur ; null = IA par défaut.
    active_id: uuid.UUID | None = None
    # IA par défaut (fallback serveur AI_*) : proposée ou non par ce serveur,
    # et où en est l'utilisateur dans son quota QUOTIDIEN (jour UTC).
    # ``daily_quota`` = plafond effectif résolu (users.ai_daily_call_quota
    # sinon AI_DEFAULT_DAILY_QUOTA), 0 = illimité.
    default_ai_available: bool = False
    daily_quota: int = 0
    calls_today: int = 0
    # Provider/modèle servis par l'IA par défaut (AI_PROVIDER/AI_MODEL) — le
    # front les affiche dans le panneau assistant ; null quand le serveur ne
    # propose pas de fallback. Jamais AI_API_KEY ni AI_BASE_URL (règle d'or).
    default_provider: str | None = None
    default_model: str | None = None


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


def _check_reasoning_per_provider(
    provider: AIProvider, reasoning: bool | None, reasoning_effort: str | None
) -> None:
    """Gating dur par provider (miroir des contrôles masqués côté front), règle
    partagée avec le fallback serveur (:func:`check_reasoning_support`) ;
    ``null`` explicite est accepté partout — le front envoie toujours les deux."""
    check_reasoning_support(provider, reasoning, reasoning_effort)


class AIConfigurationFields(BaseModel):
    """Champs d'une configuration tels que saisis (base commune du POST, du
    PUT et du test de connexion — le test ne porte pas de ``name``)."""

    model_config = ConfigDict(extra="forbid")

    provider: AIProvider
    model: str = Field(min_length=1, max_length=200)
    # None/absent = CONSERVER la clé déjà enregistrée (changer de modèle sans
    # re-saisie) ; fournie = re-chiffrement avec un nouveau sel.
    api_key: SecretStr | None = None
    base_url: str | None = Field(None, max_length=2000)
    # Préférences de raisonnement (voir AIRequestConfig) : remplacées à
    # chaque écriture, null = défaut du provider/modèle ; le niveau doit être
    # un niveau natif du provider (check_reasoning_support), le catalogue par
    # modèle ne fait que proposer.
    reasoning: bool | None = None
    reasoning_effort: str | None = Field(None, min_length=1, max_length=REASONING_EFFORT_MAX_LENGTH)

    @field_validator("api_key")
    @classmethod
    def _key_not_blank(cls, v: SecretStr | None) -> SecretStr | None:
        return _check_key_not_blank(v)

    @model_validator(mode="after")
    def _rules_per_provider(self) -> "AIConfigurationFields":
        _check_base_url_per_provider(self.provider, self.base_url)
        _check_reasoning_per_provider(self.provider, self.reasoning, self.reasoning_effort)
        return self


class AIConfigurationIn(AIConfigurationFields):
    """Corps du POST (création) et du PUT /{id} (remplacement) d'une
    configuration nommée."""

    name: str = Field(min_length=1, max_length=NAME_MAX_LENGTH)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name ne peut pas être vide")
        return stripped


class ActiveConfigurationIn(BaseModel):
    """Corps du PUT /active : ``id`` null = revenir à l'IA par défaut."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID | None = None


class AIConnectionTestIn(AIConfigurationFields):
    """Payload du test de connexion — mêmes champs et règles que l'écriture,
    sans le nom.

    Le test valide exactement ce que l'écriture enregistrerait, sémantique de
    la clé comprise : ``api_key`` omise + ``config_id`` = tester avec la clé
    enregistrée de CETTE configuration ; omise sans ``config_id`` = aucune clé.
    """

    config_id: uuid.UUID | None = None


class AIConnectionTestRead(BaseModel):
    ok: bool = True


class AIModelListIn(BaseModel):
    """Payload du listing des modèles d'un provider — pas de ``model``, même
    sémantique de clé que le test (omise + ``config_id`` = clé enregistrée)."""

    model_config = ConfigDict(extra="forbid")

    provider: AIProvider
    api_key: SecretStr | None = None
    base_url: str | None = Field(None, max_length=2000)
    config_id: uuid.UUID | None = None

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


class ReasoningOptionsIn(BaseModel):
    """Sonde pure du catalogue pour un couple (provider, modèle) saisi dans le
    formulaire — ni clé, ni base_url, ni DB, ni réseau."""

    model_config = ConfigDict(extra="forbid")

    provider: AIProvider
    model: str = Field(min_length=1, max_length=200)
