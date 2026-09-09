"""Factories provider → chat model LangChain.

Instanciation **directe** des classes ``Chat*`` (pas ``init_chat_model`` : son
provider ``huggingface`` part en backend ``pipeline`` = chargement transformers
local, inacceptable sur Pi ; et sa table ne connaît ni ``openai_compatible`` ni
la distinction Ollama local/distant). Les imports langchain sont **paresseux,
par factory** : seul le package du provider demandé est importé — un partenaire
manquant ne casse pas les autres, et le démarrage de l'app reste léger.

Toutes les classes acceptent les kwargs standardisés (``model``, ``api_key``,
``base_url``, ``timeout``, ``max_retries``, ``max_tokens``) par alias pydantic
(``populate_by_name`` — vérifié sur les packages installés), sauf exceptions
notées par factory.

**Raisonnement** (``AIRequestConfig.reasoning`` / ``reasoning_effort``) : les
API divergent, chaque factory encode les deux préférences portables dans les
kwargs de SON provider (helpers purs ``_*_reasoning_kwargs``, testables sans
langchain) :

- Anthropic : palier choisi d'après le profil embarqué du modèle
  (``reasoning_effort_levels``) puis, pour les alias absents du profil, par
  préfixe de nom — « adaptive » (``thinking adaptive`` + ``output_config.effort``,
  Opus 4.7+/Opus 5/Sonnet 5 et tout modèle inconnu), « effort_budget » (effort
  natif, thinking par ``budget_tokens`` : Opus 4.5/4.6, Sonnet 4.6) ou
  « budget » (``budget_tokens`` seulement : Sonnet 4.5, Haiku 4.5, Claude 3).
  L'effort passe TOUJOURS par ``output_config`` (le champ ``reasoning_effort``
  de la lib force un thinking « summarized » à l'insu de l'utilisateur). ⚠ La
  lib ne valide la forme de ``thinking`` qu'À LA REQUÊTE : un mauvais palier
  n'est pas rattrapé par la validation eager (503 en plein flux) — d'où
  « inconnu = adaptive » (les modèles récents sont adaptatifs).
- OpenAI et openai_compatible : ``reasoning_effort`` au niveau natif (Chat
  Completions, aucun changement de transport — ``reasoning=`` basculerait sur
  la Responses API) ; coupé = ``"none"``, demandé sans niveau = « medium ».
- Google : famille par nom — ``gemini-2*`` pilotée par ``thinking_budget``,
  les autres par ``thinking_level`` (jamais les deux : la lib jette le
  budget) ; ``include_thoughts`` pour recevoir les deltas.
- Ollama : ``reasoning`` (→ ``think``), booléen ou niveau (gpt-oss).
- Mistral, HuggingFace : rien (le schéma du credential refuse déjà ces
  préférences pour eux ; ignorées défensivement ici).

Doctrine : le gating par PROVIDER est dur (schéma, niveaux natifs de
``PROVIDER_REASONING_EFFORTS``), le support par MODÈLE est proposé par le
catalogue (:mod:`app.core.ai.reasoning`) mais laissé au provider — ce qui est
demandé est transmis, jamais abandonné en silence ; un refus remonte par
``errors.py`` (400 → 422).
"""

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from app.core.ai.errors import invalid_config
from app.core.ai.types import AIProvider, AIRequestConfig
from app.core.config import settings

if TYPE_CHECKING:  # uniquement pour les annotations — jamais importé au runtime
    from langchain_core.language_models.chat_models import BaseChatModel


def _require_api_key(cfg: AIRequestConfig) -> str:
    if cfg.api_key is None or not cfg.api_key.get_secret_value():
        raise invalid_config(f"Clé API requise pour le provider « {cfg.provider.value} »")
    return cfg.api_key.get_secret_value()


def _common_kwargs(cfg: AIRequestConfig) -> dict[str, Any]:
    """Kwargs partagés — les optionnels ne sont passés que s'ils sont fournis
    (None écraserait le défaut du SDK chez certains providers)."""
    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "timeout": settings.AI_TIMEOUT_SECONDS,
        "max_retries": settings.AI_MAX_RETRIES,
    }
    if cfg.temperature is not None:
        kwargs["temperature"] = cfg.temperature
    if cfg.max_tokens is not None:
        kwargs["max_tokens"] = cfg.max_tokens
    return kwargs


# ---------------------------------------------------------------- raisonnement

# Budgets de réflexion Anthropic (``budget_tokens``) par niveau portable, pour
# les paliers sans effort natif ; « medium » quand seul le raisonnement est
# demandé.
_ANTHROPIC_BUDGETS: dict[str, int] = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 32768,
    "max": 65536,
}
_ANTHROPIC_MIN_BUDGET = 1024  # minimum imposé par l'API
_ANTHROPIC_OUTPUT_MARGIN = 1024  # budget_tokens doit rester < max_tokens
# Alias absents du profil embarqué (ids datés, « -latest ») : palier par
# préfixe. Opus 4.0 daté = « claude-opus-4-2025… ».
_ANTHROPIC_EFFORT_BUDGET_PREFIXES = ("claude-opus-4-5", "claude-opus-4-6", "claude-sonnet-4-6")
_ANTHROPIC_BUDGET_PREFIXES = (
    "claude-3",
    "claude-opus-4-1",
    "claude-opus-4-2025",
    "claude-sonnet-4",
    "claude-haiku-4",
)
# Budgets Gemini 2.x (``thinking_budget``) ; -1 = dynamique, 0 = coupé.
_GOOGLE_BUDGETS: dict[str, int] = {"minimal": 512, "low": 1024, "medium": 8192, "high": 24576}
# Effort explicite quand OpenAI est « demandé » sans niveau (gpt-5.1+ raisonne
# à « none » par défaut : « on » doit donc poser un niveau).
_OPENAI_DEFAULT_EFFORT = "medium"

_TIER_ADAPTIVE = "adaptive"
_TIER_EFFORT_BUDGET = "effort_budget"
_TIER_BUDGET = "budget"


def _no_reasoning_preference(cfg: AIRequestConfig) -> bool:
    return cfg.reasoning is None and cfg.reasoning_effort is None


def _anthropic_tier(model: str, profile: Mapping[str, Any] | None) -> str:
    """Palier d'encodage : profil embarqué d'abord (niveaux d'effort déclarés),
    préfixes de nom pour les alias inconnus du profil, sinon « adaptive »."""
    levels = tuple((profile or {}).get("reasoning_effort_levels") or ())
    if "xhigh" in levels:
        return _TIER_ADAPTIVE
    if levels:
        return _TIER_EFFORT_BUDGET
    if profile:  # modèle connu du profil, sans effort natif (Sonnet/Haiku 4.5)
        return _TIER_BUDGET
    if model.startswith(_ANTHROPIC_EFFORT_BUDGET_PREFIXES):
        return _TIER_EFFORT_BUDGET
    if model.startswith(_ANTHROPIC_BUDGET_PREFIXES):
        return _TIER_BUDGET
    return _TIER_ADAPTIVE


def _anthropic_budget(effort: str | None, max_tokens: int) -> int:
    """``budget_tokens`` du niveau, borné à ``[1024, max_tokens − marge]``."""
    ceiling = max_tokens - _ANTHROPIC_OUTPUT_MARGIN
    if ceiling < _ANTHROPIC_MIN_BUDGET:
        raise invalid_config(
            "max_tokens trop bas pour le raisonnement Anthropic "
            f"(au moins {_ANTHROPIC_MIN_BUDGET + _ANTHROPIC_OUTPUT_MARGIN} requis)"
        )
    return max(_ANTHROPIC_MIN_BUDGET, min(_ANTHROPIC_BUDGETS[effort or "medium"], ceiling))


def _anthropic_reasoning_kwargs(
    cfg: AIRequestConfig, profile: Mapping[str, Any] | None, max_tokens: int
) -> dict[str, Any]:
    """Kwargs ``thinking`` / ``output_config`` de :class:`ChatAnthropic`.

    ``reasoning=False`` coupe tout (effort ignoré). Sinon, par palier :
    « adaptive » → effort natif, thinking adaptatif « summarized » seulement
    si le raisonnement est demandé (le défaut adaptatif du modèle n'est pas
    touché par un effort seul) ; « effort_budget » → effort natif, thinking
    ``enabled`` + budget si demandé ; « budget » → ``enabled`` + budget dérivé
    de l'effort, seul encodage possible (asymétrie assumée).
    """
    if _no_reasoning_preference(cfg):
        return {}
    if cfg.reasoning is False:
        return {"thinking": {"type": "disabled"}}
    tier = _anthropic_tier(cfg.model, profile)
    effort = cfg.reasoning_effort
    if tier == _TIER_BUDGET:
        return {
            "thinking": {
                "type": "enabled",
                "budget_tokens": _anthropic_budget(effort, max_tokens),
            }
        }
    kwargs: dict[str, Any] = {}
    if effort is not None:
        kwargs["output_config"] = {"effort": effort}
    if cfg.reasoning is True:
        kwargs["thinking"] = (
            {"type": "adaptive", "display": "summarized"}
            if tier == _TIER_ADAPTIVE
            else {"type": "enabled", "budget_tokens": _anthropic_budget(effort, max_tokens)}
        )
    return kwargs


def _openai_reasoning_kwargs(cfg: AIRequestConfig) -> dict[str, Any]:
    """``reasoning_effort`` (Chat Completions, niveau natif) : coupé →
    ``"none"`` (gpt-5.1+ ; les autres modèles le refusent → 400 → 422),
    demandé sans niveau → « medium » (gpt-5.1+ raisonne à « none » par
    défaut). Un modèle sans raisonnement (gpt-4o, gpt-4.1) refuse tout."""
    if cfg.reasoning is False:
        return {"reasoning_effort": "none"}
    if cfg.reasoning_effort is not None:
        return {"reasoning_effort": cfg.reasoning_effort}
    if cfg.reasoning is True:
        return {"reasoning_effort": _OPENAI_DEFAULT_EFFORT}
    return {}


def _google_reasoning_kwargs(cfg: AIRequestConfig) -> dict[str, Any]:
    """Famille budget (``gemini-2*``) ou niveau (Gemini 3+, inconnus) — jamais
    les deux clés. « Coupé » sur la famille niveau = ``low`` (Gemini 3 ne se
    désactive pas) ; ``thinking_budget=0`` est refusé par 2.5 Pro (→ 422)."""
    if _no_reasoning_preference(cfg):
        return {}
    name = cfg.model.lower().removeprefix("models/")
    budget_family = "gemini-2" in name
    if cfg.reasoning is False:
        return {"thinking_budget": 0} if budget_family else {"thinking_level": "low"}
    kwargs: dict[str, Any] = {}
    if cfg.reasoning is True:
        kwargs["include_thoughts"] = True
    effort = cfg.reasoning_effort
    if budget_family:
        if effort is not None:
            kwargs["thinking_budget"] = _GOOGLE_BUDGETS[effort]
        elif cfg.reasoning is True:
            kwargs["thinking_budget"] = -1
    elif effort is not None:
        kwargs["thinking_level"] = effort
    return kwargs


def _ollama_reasoning_kwargs(cfg: AIRequestConfig) -> dict[str, Any]:
    """``reasoning`` de :class:`ChatOllama` (→ ``think``) : booléen, ou niveau
    (chaîne) pour les modèles à niveaux (gpt-oss) — le niveau vaut activation."""
    if cfg.reasoning is False:
        return {"reasoning": False}
    if cfg.reasoning_effort is not None:
        return {"reasoning": cfg.reasoning_effort}
    if cfg.reasoning is True:
        return {"reasoning": True}
    return {}


# ---------------------------------------------------------------- factories


def _build_anthropic(cfg: AIRequestConfig) -> "BaseChatModel":
    """Sans préférence de raisonnement, une seule construction. Sinon la
    première sert de sonde (profil embarqué → palier, ``max_tokens`` résolu
    par la lib : construction pydantic pure, aucun client réseau) et le
    modèle retourné est reconstruit avec les kwargs de raisonnement."""
    from langchain_anthropic import ChatAnthropic

    kwargs = _common_kwargs(cfg)
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    api_key = _require_api_key(cfg)
    model = ChatAnthropic(api_key=api_key, **kwargs)
    if _no_reasoning_preference(cfg):
        return model
    extra = _anthropic_reasoning_kwargs(cfg, model.profile, model.max_tokens)
    return ChatAnthropic(api_key=api_key, **kwargs, **extra)


def _build_openai(cfg: AIRequestConfig) -> "BaseChatModel":
    """``stream_usage=True`` : langchain n'active ``stream_options.include_usage``
    que sur l'endpoint officiel (toute ``base_url`` le désactive) — forcé ici
    pour que l'usage arrive en flux, proxy OpenAI compris."""
    from langchain_openai import ChatOpenAI

    kwargs = _common_kwargs(cfg)
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return ChatOpenAI(
        api_key=_require_api_key(cfg),
        stream_usage=True,
        **kwargs,
        **_openai_reasoning_kwargs(cfg),
    )


def _build_openai_compatible(cfg: AIRequestConfig) -> "BaseChatModel":
    """Tout endpoint parlant le protocole OpenAI (Groq, Together, vLLM, LM
    Studio…). ``base_url`` obligatoire ; clé absente → placeholder (les serveurs
    locaux type vLLM exigent une chaîne non vide mais ne la vérifient pas).
    ``stream_usage=True`` force ``stream_options.include_usage`` (jamais activé
    par langchain hors endpoint officiel) : l'usage arrive en flux. Risque
    assumé : un serveur compatible strict qui rejetterait ``stream_options``
    (repli possible : une option par provider)."""
    from langchain_openai import ChatOpenAI

    if not cfg.base_url:
        raise invalid_config("base_url requise pour le provider « openai_compatible »")
    api_key = cfg.api_key.get_secret_value() if cfg.api_key else "sk-no-key"
    return ChatOpenAI(
        api_key=api_key,
        base_url=cfg.base_url,
        stream_usage=True,
        **_common_kwargs(cfg),
        **_openai_reasoning_kwargs(cfg),
    )


def _build_google(cfg: AIRequestConfig) -> "BaseChatModel":
    # Gemini = endpoint fixe : pas de base_url custom ici.
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(
        api_key=_require_api_key(cfg), **_common_kwargs(cfg), **_google_reasoning_kwargs(cfg)
    )


def _build_mistral(cfg: AIRequestConfig) -> "BaseChatModel":
    from langchain_mistralai import ChatMistralAI

    kwargs = _common_kwargs(cfg)
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return ChatMistralAI(api_key=_require_api_key(cfg), **kwargs)


def _build_ollama(cfg: AIRequestConfig) -> "BaseChatModel":
    """Pas de clé ; ``base_url`` optionnelle (défaut SDK : localhost:11434 =
    Ollama local, sinon instance distante « Ollama custom »). Le plafond de
    tokens s'appelle ``num_predict`` et il n'y a ni timeout ni retries."""
    from langchain_ollama import ChatOllama

    kwargs: dict[str, Any] = {"model": cfg.model}
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    if cfg.temperature is not None:
        kwargs["temperature"] = cfg.temperature
    if cfg.max_tokens is not None:
        kwargs["num_predict"] = cfg.max_tokens
    return ChatOllama(**kwargs, **_ollama_reasoning_kwargs(cfg))


def _build_huggingface(cfg: AIRequestConfig) -> "BaseChatModel":
    """Toujours via :class:`HuggingFaceEndpoint` (API distante). **Jamais**
    ``ChatHuggingFace.from_model_id`` ni le backend ``pipeline`` : ils chargent
    le modèle transformers en local — mortel sur Pi."""
    from langchain_huggingface import ChatHuggingFace, HuggingFaceEndpoint

    kwargs: dict[str, Any] = {
        "repo_id": cfg.model,
        "huggingfacehub_api_token": _require_api_key(cfg),
        "timeout": settings.AI_TIMEOUT_SECONDS,
    }
    if cfg.temperature is not None:
        kwargs["temperature"] = cfg.temperature
    if cfg.max_tokens is not None:
        kwargs["max_new_tokens"] = cfg.max_tokens
    return ChatHuggingFace(llm=HuggingFaceEndpoint(**kwargs))


_FACTORIES: dict[AIProvider, Callable[[AIRequestConfig], "BaseChatModel"]] = {
    AIProvider.ANTHROPIC: _build_anthropic,
    AIProvider.OPENAI: _build_openai,
    AIProvider.OPENAI_COMPATIBLE: _build_openai_compatible,
    AIProvider.GOOGLE: _build_google,
    AIProvider.MISTRAL: _build_mistral,
    AIProvider.OLLAMA: _build_ollama,
    AIProvider.HUGGINGFACE: _build_huggingface,
}


def build_chat_model(cfg: AIRequestConfig) -> "BaseChatModel":
    """Construit le chat model du provider demandé (validation locale → 422)."""
    factory = _FACTORIES.get(cfg.provider)
    if factory is None:  # AIProvider est fermé, mais restons défensifs
        raise invalid_config(f"Provider IA inconnu : {cfg.provider}")
    return factory(cfg)
