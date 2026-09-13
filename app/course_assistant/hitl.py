"""Reprises HITL en attente d'une réponse du professeur — registre in-memory.

Deux genres d'attente (:data:`KIND_PROPOSAL`, :data:`KIND_QUESTIONS`) passent
par l'**interrupt LangGraph** : un tool bloquant — proposition d'édition d'un
contexte d'édition (:mod:`app.course_assistant.editing`), ou questions au
professeur (:mod:`app.course_assistant.questions`, tous contextes) — appelle
:func:`suspend`, **seul point d'appel d'``agent_interrupt``** du package ; le
flux SSE émet ``interrupt`` et SE FERME ; l'état du run vit au **checkpointer
InMemory** du client IA (:mod:`app.core.ai`), et CE registre retient de quoi
le reprendre — thread, appel en attente, genre, config résolue, la
numérotation ``Q…`` des questions du bloc édité (``question_refs``, rejouée à
la reprise pour que les références restent stables le temps du tour —
docstring de :mod:`app.course_assistant.refs`) et, pour des questions, la
forme de la réponse attendue (``answer_shape``, contrôlée par la route AVANT
toute consommation). La route de décision ou de réponse consomme l'entrée
(:func:`take`, genre compris) et rouvre un flux qui reprend le run
(``stream_agent(..., thread_id=, resume=)``) : le résultat du tool est la
réponse du professeur.

La **config est réutilisée telle quelle** à la reprise (même provider garanti —
les ids de tool calls du thread sont propres au provider — pas de nouvelle
cascade ni de quota : un tour HITL = un appel compté, quel que soit le nombre
de reprises).

Contraintes assumées, cohérentes avec le checkpointer InMemory : **mono-
processus** (la reprise doit arriver sur le worker qui tient le checkpoint) et
**perdu au redémarrage** — registre et checkpoints disparaissent ensemble.
Le passage à ``AsyncPostgresSaver`` (reprises durables) attend le multi-nœud
(TODO.md racine). Une entrée jamais reprise expire
(:data:`PENDING_TTL_SECONDS`) : :func:`take`/:func:`peek` l'ignorent,
:func:`sweep_expired` la retire ; l'appelant purge le thread checkpointé
correspondant (``AIClient.drop_agent_thread``).
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.ai import AIRequestConfig, AIToolCall, agent_interrupt

# Au-delà, une attente jamais reprise est considérée abandonnée.
PENDING_TTL_SECONDS = 6 * 3600

# Genres d'attente : la route qui reprend doit correspondre au tool figé.
KIND_PROPOSAL = "proposal"
KIND_QUESTIONS = "questions"


@dataclass
class PendingInterrupt:
    """Une reprise en attente : le run figé d'UNE conversation (au plus une)."""

    thread_id: str
    tool_call_id: str
    provider: str
    config: AIRequestConfig | None
    # Références ``Q…`` → id des questions du bloc édité (contexte exercice),
    # telles que numérotées au tour de l'interrupt ; ``None`` sinon.
    question_refs: dict[str, str] | None = None
    kind: str = KIND_PROPOSAL
    # Questions au professeur : ``[{"multi_select": bool, "options": n}]``,
    # une entrée par question (cf. ``questions.answer_shape``) ; ``None`` sinon.
    answer_shape: list[dict[str, Any]] | None = None
    created_at: float = field(default_factory=time.time)

    def expired(self) -> bool:
        return time.time() - self.created_at > PENDING_TTL_SECONDS


_PENDING: dict[uuid.UUID, PendingInterrupt] = {}


def suspend(
    call: AIToolCall, *, kind: str, answer_shape: list[dict[str, Any]] | None = None
) -> Any:
    """Fige le run en attendant le professeur (interrupt LangGraph — le flux
    SSE se ferme) ; à la reprise, RETOURNE la valeur de reprise (décision ou
    réponses). Le payload relaie au flux l'id d'appel (clé de reprise), le
    genre et, pour des questions, la forme de réponse attendue.

    À appeler APRÈS validation des args : un échec de validation doit répondre
    immédiatement (aucun run figé) — et, le tool étant ré-exécuté depuis le
    début à la reprise, cette validation doit être idempotente.
    """
    payload: dict[str, Any] = {"tool_call_id": call.id or "?", "kind": kind}
    if answer_shape is not None:
        payload["answer_shape"] = answer_shape
    return agent_interrupt(payload)


def register(conversation_id: uuid.UUID, pending: PendingInterrupt) -> PendingInterrupt | None:
    """Enregistre LA reprise en attente d'une conversation (une seule à la
    fois) ; retourne l'entrée remplacée, dont l'appelant purge le thread."""
    previous = _PENDING.pop(conversation_id, None)
    _PENDING[conversation_id] = pending
    return previous


def peek(conversation_id: uuid.UUID, tool_call_id: str, *, kind: str) -> PendingInterrupt | None:
    """La reprise en attente pour cet appel et ce genre, SANS la consommer
    (contrôle d'une réponse avant :func:`take`) ; ``None`` si absente, d'un
    autre appel ou d'un autre genre, ou expirée."""
    pending = _PENDING.get(conversation_id)
    if pending is None or pending.expired():
        return None
    if pending.tool_call_id != tool_call_id or pending.kind != kind:
        return None
    return pending


def take(conversation_id: uuid.UUID, tool_call_id: str, *, kind: str) -> PendingInterrupt | None:
    """Consomme la reprise si elle correspond à cet appel et à ce genre ;
    ``None`` sinon, sans rien retirer (une expirée est laissée à
    :func:`sweep_expired`, qui en purge le thread)."""
    if peek(conversation_id, tool_call_id, kind=kind) is None:
        return None
    return _PENDING.pop(conversation_id)


def drop(conversation_id: uuid.UUID) -> PendingInterrupt | None:
    """Abandonne la reprise en attente d'une conversation (nouveau message
    envoyé, conversation supprimée) ; l'appelant purge le thread."""
    return _PENDING.pop(conversation_id, None)


def sweep_expired() -> list[PendingInterrupt]:
    """Retire toutes les reprises expirées ; l'appelant purge leurs threads
    (sans ce balayage, une conversation jamais rouverte garderait son
    checkpoint jusqu'au redémarrage)."""
    expired = [cid for cid, pending in _PENDING.items() if pending.expired()]
    return [_PENDING.pop(cid) for cid in expired]
