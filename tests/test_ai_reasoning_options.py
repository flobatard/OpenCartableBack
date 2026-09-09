"""Catalogue des options de raisonnement par couple (provider, modèle).

Les **règles par préfixe** sont la source de vérité du repo ; le profil
embarqué des paquets langchain ne fait que les raffiner. Son contenu est une
donnée tierce qui change d'une version à l'autre : un test qui l'interroge
casse à la première montée de version — panne constatée en préprod, où
``langchain-google-genai`` 4.4 ne déclare plus ``gemini-2.0-flash`` comme
non-raisonnant. La table ci-dessous s'exécute donc **profil neutralisé**
(fixture autouse), et la branche « profil » a ses propres cas à profils
explicites (motif de tests/test_ai_providers.py).
"""

from collections.abc import Mapping
from typing import Any

import pytest

from app.core.ai import AIProvider, ReasoningOptions, reasoning_options
from app.core.ai import reasoning as reasoning_module
from app.core.ai.reasoning import normalize_model_name

BOTH = ("on", "off")
ON = ("on",)
LMH = ("low", "medium", "high")
ALL5 = (*LMH, "xhigh", "max")
LMHX = (*LMH, "xhigh")


def _set_profile(monkeypatch: pytest.MonkeyPatch, profile: Mapping[str, Any] | None) -> None:
    """Profil embarqué scripté (le vrai registre n'est jamais interrogé)."""
    monkeypatch.setattr(reasoning_module, "model_profile", lambda provider, model: profile)


@pytest.fixture(autouse=True)
def no_embedded_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aucun profil : les règles du catalogue doivent se suffire."""
    _set_profile(monkeypatch, None)


# ---------------------------------------------------------------- règles seules


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        # Anthropic : famille 5 à niveaux natifs, 4.x et 3-7 à budget (presets),
        # générations antérieures sans réflexion.
        (AIProvider.ANTHROPIC, "claude-opus-5", ReasoningOptions(BOTH, ALL5, True)),
        (AIProvider.ANTHROPIC, "claude-sonnet-5", ReasoningOptions(BOTH, ALL5, True)),
        (AIProvider.ANTHROPIC, "claude-opus-4-5", ReasoningOptions(BOTH, LMH, True)),
        (AIProvider.ANTHROPIC, "claude-opus-4-6-2026", ReasoningOptions(BOTH, (*LMH, "max"), True)),
        (AIProvider.ANTHROPIC, "claude-sonnet-4-5", ReasoningOptions(BOTH, LMH, True)),  # presets
        (AIProvider.ANTHROPIC, "claude-3-7-sonnet-latest", ReasoningOptions(BOTH, LMH, True)),
        (AIProvider.ANTHROPIC, "claude-3-5-sonnet-latest", ReasoningOptions((), (), True)),
        (AIProvider.ANTHROPIC, "claude-3-haiku-20240307", ReasoningOptions((), (), True)),
        (AIProvider.ANTHROPIC, "claude-opus-6", ReasoningOptions(BOTH, ALL5, False)),
        # OpenAI : « none » = bascule (gpt-5.1+), minimal sur gpt-5, xhigh sur 5.2+ ;
        # la famille gpt-4 ne raisonne pas.
        (AIProvider.OPENAI, "gpt-4o", ReasoningOptions((), (), True)),
        (AIProvider.OPENAI, "gpt-4.1-mini", ReasoningOptions((), (), True)),
        (AIProvider.OPENAI, "gpt-5", ReasoningOptions((), ("minimal", *LMH), True)),
        (AIProvider.OPENAI, "gpt-5-mini", ReasoningOptions((), ("minimal", *LMH), True)),
        (AIProvider.OPENAI, "gpt-5-pro", ReasoningOptions((), ("high",), True)),
        (AIProvider.OPENAI, "gpt-5.1", ReasoningOptions(BOTH, LMH, True)),
        (AIProvider.OPENAI, "gpt-5.1-codex-max", ReasoningOptions((), LMHX, True)),
        (AIProvider.OPENAI, "gpt-5.2", ReasoningOptions(BOTH, LMHX, True)),
        (AIProvider.OPENAI, "gpt-5.4-mini", ReasoningOptions(BOTH, LMHX, True)),
        (AIProvider.OPENAI, "o3", ReasoningOptions((), LMH, True)),
        (AIProvider.OPENAI, "gpt-6", ReasoningOptions((), LMH, False)),
        (AIProvider.OPENAI_COMPATIBLE, "llama-3.3-70b-versatile", ReasoningOptions((), LMH, False)),
        (AIProvider.OPENAI_COMPATIBLE, "gpt-5.2", ReasoningOptions(BOTH, LMHX, True)),
        # Google : 1.x et 2.0 ne raisonnent pas, 2.5 à budget (presets), 3.x à
        # niveau natif et jamais désactivable.
        (AIProvider.GOOGLE, "gemini-2.0-flash", ReasoningOptions((), (), True)),
        (AIProvider.GOOGLE, "gemini-1.5-pro", ReasoningOptions((), (), True)),
        (AIProvider.GOOGLE, "gemini-2.5-flash", ReasoningOptions(BOTH, LMH, True)),
        (AIProvider.GOOGLE, "gemini-2.5-pro", ReasoningOptions(ON, LMH, True)),
        (AIProvider.GOOGLE, "gemini-3-pro-preview", ReasoningOptions(ON, ("low", "high"), True)),
        (AIProvider.GOOGLE, "models/Gemini-3.1-Pro-Preview", ReasoningOptions(ON, LMH, True)),
        (AIProvider.GOOGLE, "gemini-3-flash", ReasoningOptions(ON, ("minimal", *LMH), True)),
        (AIProvider.GOOGLE, "gemini-4-pro", ReasoningOptions(ON, LMH, False)),
        # Ollama : bascule think partout, niveaux pour gpt-oss seulement.
        (AIProvider.OLLAMA, "gpt-oss:20b", ReasoningOptions(BOTH, LMH, True)),
        (AIProvider.OLLAMA, "qwen3", ReasoningOptions(BOTH, (), False)),
        # Sans capacité.
        (AIProvider.MISTRAL, "magistral-medium-latest", ReasoningOptions((), (), True)),
        (AIProvider.HUGGINGFACE, "meta-llama/Llama-3-8B", ReasoningOptions((), (), True)),
    ],
)
def test_reasoning_options(provider: AIProvider, model: str, expected: ReasoningOptions) -> None:
    assert reasoning_options(provider, model) == expected


def test_empty_model_has_no_options() -> None:
    assert reasoning_options(AIProvider.ANTHROPIC, "  ") == ReasoningOptions((), (), False)


def test_normalize_model_name() -> None:
    assert normalize_model_name(" models/Gemini-2.5-Flash ") == "gemini-2.5-flash"


# ---------------------------------------------------------------- branche profil


def test_profile_without_reasoning_wins_over_the_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reasoning_output: False`` coupe tout, même sur un modèle réputé raisonner."""
    _set_profile(monkeypatch, {"reasoning_output": False})
    assert reasoning_options(AIProvider.GOOGLE, "gemini-3-pro-preview") == ReasoningOptions(
        (), (), True
    )
    assert reasoning_options(AIProvider.OPENAI, "gpt-5.2") == ReasoningOptions((), (), True)


def test_anthropic_profile_levels_are_taken_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """Les niveaux déclarés priment sur les règles (modèle inconnu du catalogue)."""
    _set_profile(
        monkeypatch, {"reasoning_output": True, "reasoning_effort_levels": ["low", "high"]}
    )
    assert reasoning_options(AIProvider.ANTHROPIC, "claude-opus-9") == ReasoningOptions(
        BOTH, ("low", "high"), True
    )


def test_anthropic_known_profile_without_levels_uses_budget_presets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Modèle connu du profil mais sans effort natif (Sonnet/Haiku 4.5) : presets."""
    _set_profile(monkeypatch, {"reasoning_output": True})
    assert reasoning_options(AIProvider.ANTHROPIC, "claude-opus-9") == ReasoningOptions(
        BOTH, LMH, True
    )


def test_profile_that_reasons_leaves_the_rules_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hors Anthropic, le profil ne sert qu'à couper : les niveaux viennent des règles."""
    _set_profile(monkeypatch, {"reasoning_output": True})
    assert reasoning_options(AIProvider.GOOGLE, "gemini-2.5-pro") == ReasoningOptions(ON, LMH, True)
    assert reasoning_options(AIProvider.OPENAI, "gpt-5.1") == ReasoningOptions(BOTH, LMH, True)
