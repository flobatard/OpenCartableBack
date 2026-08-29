"""Schémas HTTP de la route de smoke-test IA.

Miroir HTTP des types de :mod:`app.core.ai` — on ne réutilise pas directement
:class:`AIRequestConfig` en entrée pour garder des bornes propres au transport
(tailles max) et un découplage schéma HTTP ↔ types internes (motif du repo).
``api_key`` est un ``SecretStr`` : jamais ré-émis dans une réponse ni une repr.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.core.ai import AIProvider


class ChatMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class AIConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: AIProvider
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = None
    base_url: str | None = Field(None, max_length=2000)
    temperature: float | None = Field(None, ge=0, le=2)
    max_tokens: int | None = Field(None, ge=1, le=128_000)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessageIn] = Field(min_length=1, max_length=100)
    # None → fallback serveur AI_* (résolu par AIClient.resolve_config).
    config: AIConfigIn | None = None


class ChatUsageRead(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None


class ChatResponse(BaseModel):
    content: str
    provider: str
    model: str
    usage: ChatUsageRead | None = None
