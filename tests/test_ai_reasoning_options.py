"""Catalogue des options de raisonnement par couple (provider, modèle) —
règles par préfixe adossées au profil embarqué des paquets langchain (lecture
locale, aucun réseau)."""

import pytest

from app.core.ai import AIProvider, ReasoningOptions, reasoning_options
from app.core.ai.reasoning import normalize_model_name

BOTH = ("on", "off")
ON = ("on",)
LMH = ("low", "medium", "high")
ALL5 = (*LMH, "xhigh", "max")
LMHX = (*LMH, "xhigh")


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        # Anthropic : niveaux déclarés par le profil embarqué, sinon préfixes.
        (AIProvider.ANTHROPIC, "claude-opus-5", ReasoningOptions(BOTH, ALL5, True)),
        (AIProvider.ANTHROPIC, "claude-opus-4-5", ReasoningOptions(BOTH, LMH, True)),
        (AIProvider.ANTHROPIC, "claude-opus-4-6-2026", ReasoningOptions(BOTH, (*LMH, "max"), True)),
        (AIProvider.ANTHROPIC, "claude-sonnet-4-5", ReasoningOptions(BOTH, LMH, True)),  # presets
        (AIProvider.ANTHROPIC, "claude-3-7-sonnet-latest", ReasoningOptions(BOTH, LMH, True)),
        (AIProvider.ANTHROPIC, "claude-opus-6", ReasoningOptions(BOTH, ALL5, False)),
        # OpenAI : « none » = bascule (gpt-5.1+), minimal sur gpt-5, xhigh sur 5.2+.
        (AIProvider.OPENAI, "gpt-4o", ReasoningOptions((), (), True)),
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
        # Google : 2.x à budget (presets), 3.x à niveau natif, jamais désactivable.
        (AIProvider.GOOGLE, "gemini-2.0-flash", ReasoningOptions((), (), True)),
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
