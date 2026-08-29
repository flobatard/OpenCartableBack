"""Types publics de la brique IA — zéro import langchain.

Ces modèles sont l'interface que les futures features (J5 : RAG, résumés,
quiz, review de copies) importeront via :mod:`app.core.ai`. Ils ne dépendent
d'aucun SDK provider : remplacer LangChain ne les toucherait pas.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class AIProvider(StrEnum):
    """Fournisseurs supportés par le client générique (BYO token)."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"  # Gemini via AI Studio (google_genai)
    MISTRAL = "mistral"
    OLLAMA = "ollama"  # base_url optionnelle : local (défaut) OU distant
    OPENAI_COMPATIBLE = "openai_compatible"  # Groq, Together, vLLM, LM Studio…
    HUGGINGFACE = "huggingface"  # Inference Endpoints/Providers (jamais pipeline local)


class AIRequestConfig(BaseModel):
    """Config d'appel BYO token : voyage à chaque appel, jamais persistée ici.

    ``api_key`` est un :class:`SecretStr` : sa repr est masquée, une fuite dans
    un log ou une exception n'expose jamais la clé.
    """

    model_config = ConfigDict(extra="forbid")

    provider: AIProvider
    model: str = Field(min_length=1)
    api_key: SecretStr | None = None
    base_url: str | None = None
    temperature: float | None = Field(None, ge=0, le=2)
    max_tokens: int | None = Field(None, ge=1)


class ChatMessage(BaseModel):
    """Message de conversation (nom distinct de l'AIMessage de langchain)."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class AIUsage(BaseModel):
    """Consommation de tokens relayée par le provider (souvent partielle)."""

    input_tokens: int | None = None
    output_tokens: int | None = None


class AICompletion(BaseModel):
    """Réponse complète d'un appel classique (:meth:`AIClient.complete`)."""

    content: str
    provider: str
    model: str
    usage: AIUsage | None = None


class AIStreamEvent(BaseModel):
    """Événement du flux de :meth:`AIClient.stream`.

    ``token`` porte un ``delta`` de texte ; ``done`` clôt le flux avec l'usage
    agrégé quand le provider le fournit (sinon ``None``).
    """

    type: Literal["token", "done"]
    delta: str = ""
    usage: AIUsage | None = None
