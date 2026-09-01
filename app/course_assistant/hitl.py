"""Reprises HITL en attente de décision — registre in-memory (décision actée).

Le flux HITL d'un contexte d'édition passe par l'**interrupt LangGraph** : quand
l'agent propose (``agent_interrupt`` dans le tool ``propose_block_edit``), le
flux SSE émet ``interrupt`` et SE FERME ; l'état du run vit au **checkpointer
InMemory** du client IA (:mod:`app.core.ai`), et CE registre retient de quoi le
reprendre — thread, appel en attente, config résolue. La route de décision
consomme l'entrée (:func:`take`) et rouvre un flux qui reprend le run
(``stream_agent(..., thread_id=, resume=)``) : le résultat du tool est la
décision du professeur.

La **config est réutilisée telle quelle** à la reprise (même provider garanti —
les ids de tool calls du thread sont propres au provider — pas de nouvelle
cascade ni de quota : un tour HITL = un appel compté, quel que soit le nombre
de reprises ; décision actée).

Contraintes assumées, cohérentes avec le checkpointer InMemory : **mono-
processus** (la reprise doit arriver sur le worker qui tient le checkpoint) et
**perdu au redémarrage** — registre et checkpoints disparaissent ensemble.
Passage à ``AsyncPostgresSaver`` (reprises durables, multi-nœud) acté pour le
jour du multi-nœud — suivi dans le TODO.md racine. Une entrée jamais reprise
expire (:data:`PENDING_TTL_SECONDS`, contrôle paresseux) ; l'appelant purge le
thread checkpointé correspondant (``AIClient.drop_agent_thread``).
"""

import time
import uuid
from dataclasses import dataclass, field

from app.core.ai import AIRequestConfig

# Au-delà, une proposition jamais tranchée est considérée abandonnée (le front
# ne la ré-offre pas après un rechargement de page — rouvrir la conversation
# montre le round incomplet, replié au replay).
PENDING_TTL_SECONDS = 6 * 3600


@dataclass
class PendingProposal:
    """Une reprise en attente : le run figé d'UNE conversation (au plus une)."""

    thread_id: str
    tool_call_id: str
    provider: str
    config: AIRequestConfig | None
    created_at: float = field(default_factory=time.time)

    def expired(self) -> bool:
        return time.time() - self.created_at > PENDING_TTL_SECONDS


_PENDING: dict[uuid.UUID, PendingProposal] = {}


def register(conversation_id: uuid.UUID, pending: PendingProposal) -> PendingProposal | None:
    """Enregistre LA reprise en attente d'une conversation (une seule à la
    fois) ; retourne l'entrée remplacée, dont l'appelant purge le thread."""
    previous = _PENDING.pop(conversation_id, None)
    _PENDING[conversation_id] = pending
    return previous


def take(conversation_id: uuid.UUID, tool_call_id: str) -> PendingProposal | None:
    """Consomme la reprise si elle correspond à cet appel ; ``None`` sinon
    (absente, id différent, expirée — l'entrée expirée est retirée)."""
    pending = _PENDING.get(conversation_id)
    if pending is None:
        return None
    if pending.expired():
        _PENDING.pop(conversation_id, None)
        return None
    if pending.tool_call_id != tool_call_id:
        return None
    return _PENDING.pop(conversation_id)


def drop(conversation_id: uuid.UUID) -> PendingProposal | None:
    """Abandonne la reprise en attente d'une conversation (nouveau message
    envoyé alors qu'une proposition attendait) ; l'appelant purge le thread."""
    return _PENDING.pop(conversation_id, None)
