"""Catalogue des options de raisonnement par couple (provider, modèle).

Aucun provider ne publie par API les niveaux d'effort de ses modèles : ce
catalogue est **maintenu à la main** (règles par préfixe de nom, ordonnées de
la plus précise à la plus générale), adossé au profil embarqué de langchain
(:mod:`app.core.ai.profiles`) — niveaux Anthropic déclarés par modèle, et
« ne raisonne pas » (``reasoning_output`` False) chez Anthropic/OpenAI/Google.

Sémantique de :class:`ReasoningOptions` :

- ``toggle`` : sous-ensemble de ``("on", "off")`` — « on » = raisonnement
  demandé et affiché, « off » = coupé ; absent quand le modèle ne l'accepte
  pas (Gemini 3 ne se désactive pas → ``("on",)`` ; o-series/gpt-5 sans
  « none » → ``()``).
- ``efforts`` : niveaux NATIFS proposés, dans l'ordre croissant. Pour les
  modèles sans effort natif mais à budget (Anthropic ancien, Gemini 2.5), ce
  sont des presets low/medium/high traduits en budget par providers.py.
- ``known`` : le modèle est reconnu (règle ou profil) ; sinon ce sont les
  options génériques du provider, à prendre avec précaution côté UI.

Le catalogue **propose**, il n'impose pas : la validation dure reste par
provider (``check_reasoning_support``) — un nouveau modèle inconnu ici reste
utilisable avec les niveaux du provider ; un refus du provider remonte en 422.
"""

from dataclasses import dataclass

from app.core.ai.profiles import declares_no_reasoning, model_profile, reasoning_effort_levels
from app.core.ai.types import AIProvider

ON = "on"
OFF = "off"
BOTH = (ON, OFF)


@dataclass(frozen=True)
class ReasoningOptions:
    toggle: tuple[str, ...]
    efforts: tuple[str, ...]
    known: bool


NOTHING = ReasoningOptions(toggle=(), efforts=(), known=True)

# ---------------------------------------------------------------- règles par provider
# (préfixes, options) — premier préfixe qui matche, donc du plus précis au
# plus général. Les noms sont normalisés (minuscules, sans « models/ »).

_LOW_MED_HIGH = ("low", "medium", "high")

_ANTHROPIC_RULES = (
    (("claude-opus-4-5",), ReasoningOptions(BOTH, _LOW_MED_HIGH, True)),
    (
        ("claude-opus-4-6", "claude-sonnet-4-6"),
        ReasoningOptions(BOTH, (*_LOW_MED_HIGH, "max"), True),
    ),
    # Modèles à budget de réflexion (pas d'effort natif) : presets → budget_tokens.
    (
        ("claude-3", "claude-opus-4-1", "claude-opus-4-2025", "claude-sonnet-4", "claude-haiku-4"),
        ReasoningOptions(BOTH, _LOW_MED_HIGH, True),
    ),
)
# Inconnu = récent (thinking adaptatif, tous les niveaux) — cohérent avec le
# palier « adaptive » de providers.py.
_ANTHROPIC_DEFAULT = ReasoningOptions(BOTH, (*_LOW_MED_HIGH, "xhigh", "max"), False)

_OPENAI_RULES = (
    (("gpt-5-pro",), ReasoningOptions((), ("high",), True)),
    (("gpt-5.2-pro", "gpt-5.4-pro", "gpt-5.5-pro"), ReasoningOptions((), ("medium", "high"), True)),
    (("gpt-5.1-codex-max",), ReasoningOptions((), (*_LOW_MED_HIGH, "xhigh"), True)),
    (("gpt-5.1-codex", "gpt-5-codex"), ReasoningOptions((), _LOW_MED_HIGH, True)),
    (("gpt-5.2-codex", "gpt-5.3-codex"), ReasoningOptions((), (*_LOW_MED_HIGH, "xhigh"), True)),
    # gpt-5.1 : « none » (défaut) → bascule ; pas de xhigh.
    (("gpt-5.1",), ReasoningOptions(BOTH, _LOW_MED_HIGH, True)),
    # gpt-5.2 et suivants : « none » et xhigh.
    (
        ("gpt-5.2", "gpt-5.3", "gpt-5.4", "gpt-5.5"),
        ReasoningOptions(BOTH, (*_LOW_MED_HIGH, "xhigh"), True),
    ),
    # gpt-5, gpt-5-mini, gpt-5-nano, gpt-5-chat-latest : minimal, pas de « none ».
    (("gpt-5",), ReasoningOptions((), ("minimal", *_LOW_MED_HIGH), True)),
    (("o1-pro",), ReasoningOptions((), ("high",), True)),
    (("o1", "o3", "o4"), ReasoningOptions((), _LOW_MED_HIGH, True)),
)
_OPENAI_DEFAULT = ReasoningOptions((), _LOW_MED_HIGH, False)

_GOOGLE_RULES = (
    # 2.5 Pro ne se coupe pas (thinking_budget 0 refusé) ; presets → budget.
    (("gemini-2.5-pro",), ReasoningOptions((ON,), _LOW_MED_HIGH, True)),
    (("gemini-2.5",), ReasoningOptions(BOTH, _LOW_MED_HIGH, True)),
    # Gemini 3 : thinking_level natif, jamais désactivable.
    (("gemini-3-pro",), ReasoningOptions((ON,), ("low", "high"), True)),
    (("gemini-3.1-pro",), ReasoningOptions((ON,), _LOW_MED_HIGH, True)),
    (
        ("gemini-3-flash", "gemini-3.1-flash", "gemini-3.5-flash"),
        ReasoningOptions((ON,), ("minimal", *_LOW_MED_HIGH), True),
    ),
)
_GOOGLE_DEFAULT = ReasoningOptions((ON,), _LOW_MED_HIGH, False)

_OLLAMA_RULES = (
    # Seule famille à niveaux (think: "low"/"medium"/"high").
    (("gpt-oss",), ReasoningOptions(BOTH, _LOW_MED_HIGH, True)),
)
# Tout modèle local : la bascule think on/off, sans niveau.
_OLLAMA_DEFAULT = ReasoningOptions(BOTH, (), False)

_Rules = tuple[tuple[tuple[str, ...], ReasoningOptions], ...]
_RULES: dict[AIProvider, tuple[_Rules, ReasoningOptions]] = {
    AIProvider.ANTHROPIC: (_ANTHROPIC_RULES, _ANTHROPIC_DEFAULT),
    AIProvider.OPENAI: (_OPENAI_RULES, _OPENAI_DEFAULT),
    AIProvider.OPENAI_COMPATIBLE: (_OPENAI_RULES, _OPENAI_DEFAULT),
    AIProvider.GOOGLE: (_GOOGLE_RULES, _GOOGLE_DEFAULT),
    AIProvider.OLLAMA: (_OLLAMA_RULES, _OLLAMA_DEFAULT),
}


def normalize_model_name(model: str) -> str:
    return model.strip().lower().removeprefix("models/")


def _by_rules(provider: AIProvider, name: str) -> ReasoningOptions:
    rules, default = _RULES[provider]
    for prefixes, options in rules:
        if name.startswith(prefixes):
            return options
    return default


def reasoning_options(provider: AIProvider, model: str) -> ReasoningOptions:
    """Options à proposer pour ce couple — profil embarqué d'abord (modèle qui
    ne raisonne pas → rien ; niveaux Anthropic déclarés → repris tels quels),
    puis règles par préfixe, sinon défauts du provider (``known=False``)."""
    if provider not in _RULES:
        return NOTHING
    name = normalize_model_name(model)
    if not name:
        return ReasoningOptions((), (), False)
    profile = model_profile(provider, name)
    if declares_no_reasoning(profile):
        return NOTHING
    if provider is AIProvider.ANTHROPIC:
        levels = reasoning_effort_levels(profile)
        if levels:
            return ReasoningOptions(BOTH, levels, True)
        if profile:  # connu sans effort natif (Sonnet/Haiku 4.5) : presets budget
            return ReasoningOptions(BOTH, _LOW_MED_HIGH, True)
    return _by_rules(provider, name)
