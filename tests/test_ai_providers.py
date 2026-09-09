"""Encodage des préférences de raisonnement par provider — aucun réseau.

Les helpers ``_*_reasoning_kwargs`` sont purs (profil injecté) ; les tests
``build_chat_model`` construisent les vraies classes ``Chat*`` en local
(construction pydantic, aucun client réseau créé) et lisent les attributs
qu'elles enverraient — modèle de ``test_openai_family_forces_stream_usage``.
"""

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.core.ai import AIProvider, AIRequestConfig
from app.core.ai.providers import (
    _anthropic_reasoning_kwargs,
    _anthropic_tier,
    _google_reasoning_kwargs,
    build_chat_model,
)

KEY = SecretStr("sk-test")
ADAPTIVE_PROFILE = {"reasoning_effort_levels": ["low", "medium", "high", "xhigh", "max"]}
EFFORT_PROFILE = {"reasoning_effort_levels": ["low", "medium", "high"]}
BUDGET_PROFILE = {"reasoning_output": True}  # connu du profil, sans niveau d'effort


def _anthropic(model: str = "claude-sonnet-5", **prefs) -> AIRequestConfig:
    return AIRequestConfig(provider=AIProvider.ANTHROPIC, model=model, api_key=KEY, **prefs)


def _cfg(provider: AIProvider, model: str, **prefs) -> AIRequestConfig:
    api_key = None if provider is AIProvider.OLLAMA else KEY
    base_url = "http://localhost:8000/v1" if provider is AIProvider.OPENAI_COMPATIBLE else None
    return AIRequestConfig(
        provider=provider, model=model, api_key=api_key, base_url=base_url, **prefs
    )


# ---------------------------------------------------------------- anthropic : paliers


@pytest.mark.parametrize(
    ("model", "profile", "expected"),
    [
        ("claude-opus-5", ADAPTIVE_PROFILE, "adaptive"),
        ("claude-opus-4-5", EFFORT_PROFILE, "effort_budget"),
        ("claude-sonnet-4-5", BUDGET_PROFILE, "budget"),
        # Alias inconnus du profil : préfixes, puis « inconnu = adaptive ».
        ("claude-opus-5-20260301", None, "adaptive"),
        ("claude-opus-4-7-20260101", {}, "adaptive"),
        ("claude-sonnet-4-6-20260101", None, "effort_budget"),
        ("claude-opus-4-6-20260101", None, "effort_budget"),
        ("claude-sonnet-4-5-20250929", None, "budget"),
        ("claude-sonnet-4-20250514", None, "budget"),
        ("claude-opus-4-20250514", None, "budget"),
        ("claude-opus-4-1-20250805", None, "budget"),
        ("claude-haiku-4-5-20251001", None, "budget"),
        ("claude-3-7-sonnet-latest", None, "budget"),
    ],
)
def test_anthropic_tier(model: str, profile: dict | None, expected: str) -> None:
    assert _anthropic_tier(model, profile) == expected


# ---------------------------------------------------------------- anthropic : kwargs purs


def test_anthropic_no_preference_is_noop() -> None:
    assert _anthropic_reasoning_kwargs(_anthropic(), ADAPTIVE_PROFILE, 64000) == {}


@pytest.mark.parametrize("profile", [ADAPTIVE_PROFILE, EFFORT_PROFILE, BUDGET_PROFILE])
def test_anthropic_off_wins_over_effort(profile: dict) -> None:
    cfg = _anthropic(reasoning=False, reasoning_effort="high")
    assert _anthropic_reasoning_kwargs(cfg, profile, 64000) == {
        "thinking": {"type": "disabled"}
    }


def test_anthropic_adaptive_on() -> None:
    assert _anthropic_reasoning_kwargs(_anthropic(reasoning=True), ADAPTIVE_PROFILE, 64000) == {
        "thinking": {"type": "adaptive", "display": "summarized"}
    }


def test_anthropic_adaptive_effort_only_keeps_model_default_thinking() -> None:
    """Un effort seul ne force pas l'affichage du raisonnement (défaut du modèle)."""
    cfg = _anthropic(reasoning_effort="low")
    assert _anthropic_reasoning_kwargs(cfg, ADAPTIVE_PROFILE, 64000) == {
        "output_config": {"effort": "low"}
    }


def test_anthropic_adaptive_on_with_effort() -> None:
    cfg = _anthropic(reasoning=True, reasoning_effort="high")
    assert _anthropic_reasoning_kwargs(cfg, ADAPTIVE_PROFILE, 64000) == {
        "output_config": {"effort": "high"},
        "thinking": {"type": "adaptive", "display": "summarized"},
    }


def test_anthropic_effort_budget_tier() -> None:
    cfg = _anthropic("claude-opus-4-5", reasoning=True, reasoning_effort="low")
    assert _anthropic_reasoning_kwargs(cfg, EFFORT_PROFILE, 64000) == {
        "output_config": {"effort": "low"},
        "thinking": {"type": "enabled", "budget_tokens": 2048},
    }
    effort_only = _anthropic("claude-opus-4-5", reasoning_effort="medium")
    assert _anthropic_reasoning_kwargs(effort_only, EFFORT_PROFILE, 64000) == {
        "output_config": {"effort": "medium"}
    }


def test_anthropic_budget_tier() -> None:
    on = _anthropic("claude-sonnet-4-5", reasoning=True)
    assert _anthropic_reasoning_kwargs(on, BUDGET_PROFILE, 64000) == {
        "thinking": {"type": "enabled", "budget_tokens": 8192}
    }
    # Seul encodage possible de l'effort : le budget (asymétrie assumée).
    effort_only = _anthropic("claude-sonnet-4-5", reasoning_effort="high")
    assert _anthropic_reasoning_kwargs(effort_only, BUDGET_PROFILE, 64000) == {
        "thinking": {"type": "enabled", "budget_tokens": 16384}
    }


def test_anthropic_native_levels_on_budget_tier_become_budgets() -> None:
    """xhigh/max n'existent pas sur un modèle à budget : presets bornés sous max_tokens."""
    cfg = _anthropic("claude-sonnet-4-5", reasoning_effort="max")
    kwargs = _anthropic_reasoning_kwargs(cfg, BUDGET_PROFILE, 64000)
    assert kwargs["thinking"]["budget_tokens"] == 64000 - 1024
    cfg = _anthropic("claude-sonnet-4-5", reasoning_effort="xhigh")
    kwargs = _anthropic_reasoning_kwargs(cfg, BUDGET_PROFILE, 64000)
    assert kwargs["thinking"]["budget_tokens"] == 32768


def test_anthropic_budget_clamped_under_max_tokens() -> None:
    cfg = _anthropic("claude-sonnet-4-5", reasoning=True, reasoning_effort="high")
    kwargs = _anthropic_reasoning_kwargs(cfg, BUDGET_PROFILE, 4096)
    assert kwargs["thinking"]["budget_tokens"] == 3072
    with pytest.raises(HTTPException) as exc:
        _anthropic_reasoning_kwargs(cfg, BUDGET_PROFILE, 1500)
    assert exc.value.status_code == 422


# ---------------------------------------------------------------- anthropic : construction


def test_anthropic_model_untouched_without_preference() -> None:
    model = build_chat_model(_anthropic())
    assert model.thinking is None and model.output_config is None


def test_anthropic_known_model_adaptive() -> None:
    model = build_chat_model(_anthropic("claude-sonnet-5", reasoning=True, reasoning_effort="high"))
    assert model.thinking == {"type": "adaptive", "display": "summarized"}
    assert model.output_config == {"effort": "high"}


def test_anthropic_unknown_dated_opus5_never_enabled() -> None:
    """« enabled » + budget sur un Opus 5 lèverait une ValueError EN PLEIN FLUX
    (validation de la lib à la requête, pas à la construction) : un alias
    inconnu du profil est traité comme récent."""
    model = build_chat_model(_anthropic("claude-opus-5-20260301", reasoning=True))
    assert model.thinking == {"type": "adaptive", "display": "summarized"}


def test_anthropic_legacy_alias_budget_from_fallback_max_tokens() -> None:
    """Modèle inconnu du profil → max_tokens 4096 de la lib → budget borné."""
    model = build_chat_model(_anthropic("claude-3-7-sonnet-latest", reasoning=True))
    assert model.max_tokens == 4096
    assert model.thinking == {"type": "enabled", "budget_tokens": 3072}


def test_anthropic_known_legacy_model_budget_from_profile() -> None:
    model = build_chat_model(_anthropic("claude-sonnet-4-5", reasoning_effort="high"))
    assert model.max_tokens == 64000
    assert model.thinking == {"type": "enabled", "budget_tokens": 16384}
    assert model.output_config is None


def test_anthropic_off() -> None:
    model = build_chat_model(_anthropic("claude-sonnet-4-5", reasoning=False))
    assert model.thinking == {"type": "disabled"}


# ---------------------------------------------------------------- openai (famille)


@pytest.mark.parametrize("provider", [AIProvider.OPENAI, AIProvider.OPENAI_COMPATIBLE])
def test_openai_family_effort_without_transport_change(provider: AIProvider) -> None:
    model = build_chat_model(_cfg(provider, "gpt-5", reasoning_effort="medium"))
    assert model.reasoning_effort == "medium"
    assert model.reasoning is None  # jamais la Responses API par ce chemin
    assert model.use_responses_api is None
    assert model.stream_usage is True


def test_openai_no_preference_is_noop() -> None:
    assert build_chat_model(_cfg(AIProvider.OPENAI, "gpt-5")).reasoning_effort is None


def test_openai_toggle_maps_to_none_and_medium() -> None:
    """Coupé = « none » (gpt-5.1+) ; demandé sans niveau = « medium » (gpt-5.1+
    raisonne à « none » par défaut) ; coupé l'emporte sur le niveau."""
    off = _cfg(AIProvider.OPENAI, "gpt-5.2", reasoning=False)
    assert build_chat_model(off).reasoning_effort == "none"
    off_with_effort = _cfg(AIProvider.OPENAI, "gpt-5.2", reasoning=False, reasoning_effort="xhigh")
    assert build_chat_model(off_with_effort).reasoning_effort == "none"
    on = _cfg(AIProvider.OPENAI, "gpt-5.2", reasoning=True)
    assert build_chat_model(on).reasoning_effort == "medium"
    native = _cfg(AIProvider.OPENAI, "gpt-5", reasoning_effort="minimal")
    assert build_chat_model(native).reasoning_effort == "minimal"


# ---------------------------------------------------------------- google


def test_google_budget_family() -> None:
    on = build_chat_model(_cfg(AIProvider.GOOGLE, "gemini-2.5-flash", reasoning=True))
    assert on.include_thoughts is True and on.thinking_budget == -1 and on.thinking_level is None
    off = build_chat_model(_cfg(AIProvider.GOOGLE, "gemini-2.5-flash", reasoning=False))
    assert off.thinking_budget == 0 and off.include_thoughts is None
    effort = build_chat_model(_cfg(AIProvider.GOOGLE, "gemini-2.5-flash", reasoning_effort="high"))
    assert effort.thinking_budget == 24576 and effort.include_thoughts is None


def test_google_level_family() -> None:
    cfg = _cfg(AIProvider.GOOGLE, "gemini-3-pro-preview", reasoning=True, reasoning_effort="medium")
    model = build_chat_model(cfg)
    assert model.thinking_level == "medium"
    assert model.thinking_budget is None  # jamais les deux clés
    assert model.include_thoughts is True
    off = build_chat_model(_cfg(AIProvider.GOOGLE, "gemini-3-pro-preview", reasoning=False))
    assert off.thinking_level == "low"


def test_google_native_minimal_level() -> None:
    flash3 = _cfg(AIProvider.GOOGLE, "gemini-3-flash-preview", reasoning_effort="minimal")
    assert build_chat_model(flash3).thinking_level == "minimal"
    # Famille budget : « minimal » devient un petit budget (jamais 0 = coupé).
    assert _google_reasoning_kwargs(
        _cfg(AIProvider.GOOGLE, "gemini-2.5-flash", reasoning_effort="minimal")
    ) == {"thinking_budget": 512}


def test_google_family_by_normalised_name() -> None:
    cfg = _cfg(AIProvider.GOOGLE, "models/Gemini-2.5-pro", reasoning_effort="low")
    assert _google_reasoning_kwargs(cfg) == {"thinking_budget": 1024}


# ---------------------------------------------------------------- ollama


def test_ollama_reasoning_maps_to_think() -> None:
    assert build_chat_model(_cfg(AIProvider.OLLAMA, "qwen3", reasoning=True)).reasoning is True
    assert (
        build_chat_model(
            _cfg(AIProvider.OLLAMA, "qwen3", reasoning=False, reasoning_effort="high")
        ).reasoning
        is False
    )
    assert (
        build_chat_model(_cfg(AIProvider.OLLAMA, "gpt-oss", reasoning_effort="high")).reasoning
        == "high"
    )
    assert build_chat_model(_cfg(AIProvider.OLLAMA, "qwen3")).reasoning is None


# ---------------------------------------------------------------- mistral


def test_mistral_ignores_preferences() -> None:
    """Le schéma du credential les refuse pour mistral ; la factory les ignore."""
    cfg = _cfg(AIProvider.MISTRAL, "magistral-medium-latest", reasoning=True)
    assert build_chat_model(cfg).model == "magistral-medium-latest"
