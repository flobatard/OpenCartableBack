"""Profil embarqué d'un modèle (``model.profile`` de langchain-core, bêta).

Les paquets partenaires livrent un registre de capacités par modèle
(``reasoning_output``, ``reasoning_effort_levels`` chez Anthropic, plafonds de
tokens…), résolu par nom EXACT à la construction du chat model. Il sert
d'aide au catalogue de raisonnement (:mod:`app.core.ai.reasoning`) et au choix
du palier d'encodage Anthropic (:mod:`app.core.ai.providers`) — jamais de
vérité absolue : un modèle absent du registre est simplement « inconnu ».

Lecture par une **instance sonde** construite avec une clé factice :
construction pydantic pure, aucun client réseau ni appel. Imports langchain
paresseux par provider (confinement du paquet), résultat mémoïsé par couple
(provider, modèle).
"""

from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from app.core.ai.types import AIProvider

_PROBE_KEY = "profile-probe"


@lru_cache(maxsize=256)
def model_profile(provider: AIProvider, model: str) -> Mapping[str, Any] | None:
    """Profil déclaré par le paquet du provider ; ``None`` si inconnu ou sans
    registre (Ollama, Mistral, HuggingFace)."""
    try:
        if provider is AIProvider.ANTHROPIC:
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(model=model, api_key=_PROBE_KEY).profile or None
        if provider in (AIProvider.OPENAI, AIProvider.OPENAI_COMPATIBLE):
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(model=model, api_key=_PROBE_KEY).profile or None
        if provider is AIProvider.GOOGLE:
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(model=model, api_key=_PROBE_KEY).profile or None
    except Exception:  # noqa: BLE001 — un profil n'est qu'une aide, jamais bloquant
        return None
    return None


def reasoning_effort_levels(profile: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Niveaux d'effort déclarés par le profil (vide si absents)."""
    levels = (profile or {}).get("reasoning_effort_levels") or ()
    return tuple(str(level) for level in levels)


def declares_no_reasoning(profile: Mapping[str, Any] | None) -> bool:
    """Le profil affirme que le modèle ne raisonne pas (``reasoning_output`` False)."""
    return bool(profile) and profile.get("reasoning_output") is False
