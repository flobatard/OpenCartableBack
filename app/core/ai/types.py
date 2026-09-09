"""Types publics de la brique IA — zéro import langchain.

Ces modèles sont l'interface que les features (assistant, tuteur, et demain
RAG, résumés, quiz…) importent via :mod:`app.core.ai`. Ils ne dépendent
d'aucun SDK provider : remplacer LangChain ne les toucherait pas.
"""

from enum import StrEnum
from typing import Any, Literal

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


# Niveaux d'effort de raisonnement NATIFS acceptés par provider — l'union sur
# ses modèles, gating dur (422) ; les options réellement proposées pour un
# modèle donné viennent du catalogue :mod:`app.core.ai.reasoning`. OpenAI :
# « none » n'est pas un niveau mais l'encodage de ``reasoning=False``.
PROVIDER_REASONING_EFFORTS: dict[AIProvider, tuple[str, ...]] = {
    AIProvider.ANTHROPIC: ("low", "medium", "high", "xhigh", "max"),
    AIProvider.OPENAI: ("minimal", "low", "medium", "high", "xhigh"),
    AIProvider.OPENAI_COMPATIBLE: ("minimal", "low", "medium", "high", "xhigh"),
    AIProvider.GOOGLE: ("minimal", "low", "medium", "high"),
    AIProvider.OLLAMA: ("low", "medium", "high"),
}

# Capacités de raisonnement par provider — miroirs front
# PROVIDERS_WITH_REASONING_TOGGLE / PROVIDERS_WITH_REASONING_EFFORT
# (ai-credentials.model.ts). Le gating par PROVIDER est dur (422 au PUT du
# credential et à la résolution du fallback) ; le support par MODÈLE est
# proposé par le catalogue (:mod:`app.core.ai.reasoning`) mais jamais imposé :
# l'encodage (providers.py) transmet ce qui est demandé et le 4xx du provider
# remonte par errors.py.
# Providers dont le raisonnement peut être forcé (et affiché) ou coupé — chez
# OpenAI, coupé = ``reasoning_effort="none"`` (gpt-5.1+).
PROVIDERS_WITH_REASONING_TOGGLE = frozenset(
    {
        AIProvider.ANTHROPIC,
        AIProvider.OPENAI,
        AIProvider.OPENAI_COMPATIBLE,
        AIProvider.GOOGLE,
        AIProvider.OLLAMA,
    }
)
# Providers acceptant un niveau d'effort.
PROVIDERS_WITH_REASONING_EFFORT = frozenset(PROVIDER_REASONING_EFFORTS)

# Longueur max d'un niveau d'effort (colonne users.ai_reasoning_effort).
REASONING_EFFORT_MAX_LENGTH = 20


def check_reasoning_support(
    provider: AIProvider, reasoning: bool | None, reasoning_effort: str | None
) -> None:
    """Règle unique du gating par provider — credential utilisateur (422 au
    PUT) ET fallback serveur ``AI_REASONING*`` (422 à la résolution) :
    ``ValueError`` si une préférence est posée pour un provider hors
    capacités ou si le niveau n'est pas un niveau natif du provider ;
    ``None`` explicite accepté partout."""
    if reasoning is not None and provider not in PROVIDERS_WITH_REASONING_TOGGLE:
        raise ValueError(
            "reasoning ne s'applique qu'aux providers "
            + ", ".join(sorted(p.value for p in PROVIDERS_WITH_REASONING_TOGGLE))
        )
    if reasoning_effort is None:
        return
    efforts = PROVIDER_REASONING_EFFORTS.get(provider)
    if efforts is None:
        raise ValueError(f"reasoning_effort ne s'applique pas au provider {provider.value}")
    if reasoning_effort not in efforts:
        raise ValueError(
            f"reasoning_effort « {reasoning_effort} » inconnu du provider {provider.value} "
            f"(niveaux : {', '.join(efforts)})"
        )


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
    # Préférences de raisonnement : None = défaut du provider/modèle ;
    # ``reasoning`` True = raisonnement demandé ET affiché (deltas ``thinking``),
    # False = coupé (``reasoning_effort`` est alors ignoré) ; ``reasoning_effort``
    # = un niveau NATIF du provider (``PROVIDER_REASONING_EFFORTS``, validé
    # par ``check_reasoning_support``, jamais ici : les features passent par
    # les schémas HTTP). Encodage par provider dans providers.py.
    reasoning: bool | None = None
    reasoning_effort: str | None = Field(None, min_length=1, max_length=REASONING_EFFORT_MAX_LENGTH)


class AIToolSpec(BaseModel):
    """Déclaration d'un tool exposé au modèle — JSON Schema neutre.

    ``parameters`` suit le format « OpenAI tools » (objet JSON Schema des
    arguments) : c'est la forme que le ``bind_tools`` de tous les providers
    LangChain sait convertir, sans qu'aucun type langchain ne franchisse la
    frontière du package.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str
    parameters: dict[str, Any]


class AIToolCall(BaseModel):
    """Demande d'exécution d'un tool émise par le modèle.

    ``id`` apparie l'appel à son résultat — dans l'historique rejoué comme
    pendant l'exécution : l'exécuteur reçoit l'id de l'événement ``tool_call``
    du flux (propagé par le middleware du client, contextvar) et peut s'en
    servir de clé d'appariement (attentes HITL de l'assistant, notamment).
    Vide seulement si le provider n'en fournit pas.
    """

    id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class AIToolImage(BaseModel):
    """Image jointe au résultat d'un tool (lecture d'une ressource image).

    Le client l'expédie au modèle dans un **message utilisateur** placé après
    le résultat textuel de l'outil (et après tous les résultats d'outils du
    même round) : c'est la seule forme acceptée par TOUS les providers — les
    images dans un message ``tool`` ne le sont que chez certains. ``data`` est
    le binaire en base64 ; ``caption`` accompagne l'image en texte (nom de la
    ressource, id) pour que le modèle sache à quoi elle correspond.
    """

    mime_type: str = Field(min_length=1)
    data: str = Field(min_length=1)
    caption: str = ""


class AIToolResult(BaseModel):
    """Résultat renvoyé par l'exécuteur d'un tool de la feature appelante.

    ``is_error=True`` signale une erreur « métier » (cible inconnue, plafond
    dépassé…) que le modèle lit et peut contourner — l'exécuteur ne lève
    jamais d'exception (contrat de :meth:`AIClient.stream_agent`).
    ``image`` joint une image au résultat (cf. :class:`AIToolImage`) ;
    ``content`` reste le texte persistable/rejouable (l'image, elle, n'est
    jamais rejouée par l'historique : elle ne vaut que pour le tour courant).
    """

    content: str
    is_error: bool = False
    image: AIToolImage | None = None


class ChatMessage(BaseModel):
    """Message de conversation (nom distinct de l'AIMessage de langchain).

    Rôles ``tool`` et champs associés (tours d'agent rejoués) :

    - un message ``assistant`` peut porter ``tool_calls`` (les appels émis) ;
    - un message ``tool`` porte le résultat d'UN appel : ``tool_call_id``
      l'apparie, ``is_error`` relaie l'échec métier au modèle.
    """

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[AIToolCall] | None = None
    tool_call_id: str | None = None
    is_error: bool = False


class AIUsage(BaseModel):
    """Consommation de tokens relayée par le provider (souvent partielle).

    ``cached_input_tokens`` : part de ``input_tokens`` servie depuis le cache
    de prompt du provider (``input_token_details.cache_read`` normalisé par
    langchain — Anthropic, OpenAI, Gemini…) ; ``None`` si le provider ne le
    relaie pas.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


class AICompletion(BaseModel):
    """Réponse complète d'un appel classique (:meth:`AIClient.complete`)."""

    content: str
    provider: str
    model: str
    usage: AIUsage | None = None


class AIStreamEvent(BaseModel):
    """Événement du flux de :meth:`AIClient.stream` / :meth:`AIClient.stream_agent`.

    - ``token`` : ``delta`` de texte de la réponse ;
    - ``thinking`` : ``delta`` de raisonnement (blocs « reasoning » relayés par
      certains providers — absent sinon, sans erreur) ;
    - ``tool_call`` : le modèle demande un tool (``tool_call`` renseigné),
      émis AVANT l'exécution ;
    - ``tool_result`` : résultat d'exécution — ``tool_call`` reprend id/nom,
      ``tool_result_error`` relaie l'échec métier, ``delta`` porte le contenu
      (pour la persistance par l'appelant ; les routes SSE ne le relaient pas) ;
    - ``interrupt`` : le run est FIGÉ par ``agent_interrupt`` (HITL) —
      ``interrupt_value`` porte le payload du tool, ``interrupt_id`` l'id
      LangGraph, ``usage`` le cumul des rounds déjà joués par cet appel
      (``None`` si le provider ne relaie rien) ; le flux se termine ensuite
      SANS ``done``, la reprise passe par un nouvel appel
      ``stream_agent(..., thread_id=, resume=)`` dont l'usage repart de zéro ;
    - ``done`` : clôt le flux avec l'usage cumulé quand fourni (sinon ``None``).
    """

    type: Literal["token", "thinking", "tool_call", "tool_result", "interrupt", "done"]
    delta: str = ""
    usage: AIUsage | None = None
    tool_call: AIToolCall | None = None
    tool_result_error: bool | None = None
    interrupt_id: str | None = None
    interrupt_value: dict[str, Any] | None = None
