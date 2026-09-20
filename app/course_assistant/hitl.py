"""Reprises HITL en attente d'une réponse du professeur — registre in-memory.

Deux genres d'attente (:data:`KIND_PROPOSAL`, :data:`KIND_QUESTIONS`) passent
par l'**interrupt LangGraph** : un tool bloquant — proposition d'édition d'un
contexte d'édition (:mod:`app.course_assistant.editing`), proposition
structurelle de l'assistant global (:mod:`app.course_assistant.structure`), ou
questions au professeur (:mod:`app.course_assistant.questions`, tous contextes) — appelle
:func:`suspend`, **seul point d'appel d'``agent_interrupt``** du package ; le
flux SSE émet ``interrupt`` et SE FERME ; l'état du run vit au **checkpointer
InMemory** du client IA (:mod:`app.core.ai`), et CE registre retient de quoi
le reprendre — thread, appel en attente, genre, config résolue, la
numérotation ``Q…`` des questions du bloc édité (``question_refs``, rejouée à
la reprise pour que les références restent stables le temps du tour —
docstring de :mod:`app.course_assistant.refs`), pour des questions la forme
de la réponse attendue (``answer_shape``, contrôlée par la route AVANT toute
consommation), l'option d'édition globale du tour (``allow_edit``, rejouée
pour que la reprise rebâtisse les MÊMES tools et le même prompt) et, quand le
run figé est celui d'un **sous-assistant d'édition**
(:mod:`app.course_assistant.delegation`), le lien vers le run parent figé
(:class:`Delegation`). La route de décision ou de réponse consomme l'entrée
(:func:`take`, genre compris) et rouvre un flux qui reprend le run
(``stream_agent(..., thread_id=, resume=)``) : le résultat du tool est la
réponse du professeur.

Un troisième genre, :data:`KIND_DELEGATION`, fige le run PARENT quand
l'assistant global délègue une édition ; il est traité **dans le flux même**
par le driver de :mod:`app.course_assistant.streaming` (le sous-assistant
démarre aussitôt) : jamais relayé au front, jamais enregistré ici
(:func:`register` le refuse).

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
:func:`sweep_expired` la retire ; l'appelant purge les threads checkpointés
qu'elle tenait (``AIClient.drop_agent_thread`` sur chaque
:meth:`PendingInterrupt.thread_ids` — le run parent figé d'une délégation
compris).
"""

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.ai import AIRequestConfig, AIToolCall, agent_interrupt

# Au-delà, une attente jamais reprise est considérée abandonnée.
PENDING_TTL_SECONDS = 6 * 3600

# Genres d'attente : la route qui reprend doit correspondre au tool figé.
KIND_PROPOSAL = "proposal"
KIND_QUESTIONS = "questions"
# Délégation d'une édition par l'assistant global : le parent est figé le temps
# du sous-assistant, traité dans le flux — jamais en attente d'une route.
KIND_DELEGATION = "delegation"

# Seuls genres qu'une route reprend — les seuls admis au registre.
RESUMABLE_KINDS = frozenset({KIND_PROPOSAL, KIND_QUESTIONS})


@dataclass(frozen=True)
class Delegation:
    """Le run parent figé derrière un sous-assistant d'édition en attente.

    ``parent_thread_id`` / ``parent_call_id`` : thread et appel ``edit_*`` de
    l'assistant global, repris avec le compte rendu quand le sous-assistant
    termine ; ``context`` (contexte d'édition du descripteur), ``target_id``
    et ``instructions`` rebâtissent le sous-assistant à la reprise ;
    ``outcomes`` = lignes de compte rendu des propositions déjà tranchées
    (:func:`app.course_assistant.delegation.outcome_line`) ;
    ``pending_summary`` = résumé de la proposition en attente (sa ligne se
    compose à la décision) ; ``count`` = délégations déjà lancées dans ce tour
    (plafond du driver).
    """

    parent_thread_id: str
    parent_call_id: str
    context: str
    target_id: str
    instructions: str
    outcomes: tuple[str, ...] = ()
    pending_summary: str | None = None
    count: int = 1


@dataclass
class PendingInterrupt:
    """Une reprise en attente : le run figé d'UNE conversation (au plus une).

    Derrière une délégation (``delegation`` posé), ``thread_id`` est celui du
    **sous-assistant** ; le run parent figé n'existe que par ce lien et se
    purge avec lui (:meth:`thread_ids`).
    """

    thread_id: str
    tool_call_id: str
    provider: str
    config: AIRequestConfig | None
    # Références ``Q…`` → id des questions du bloc édité (contexte exercice),
    # telles que numérotées au tour de l'interrupt ; ``None`` sinon.
    question_refs: dict[str, str] | None = None
    # Références ``B…`` → id des blocs du cours, telles que numérotées au tour
    # de l'interrupt. Jamais rejouées (les ``B…`` restent positionnelles) :
    # elles ne servent qu'à repérer, à la reprise d'une proposition
    # structurelle, le bloc apparu ou disparu (``CourseRefs.new_block_refs`` /
    # ``stale_blocks``).
    block_refs: dict[str, str] | None = None
    kind: str = KIND_PROPOSAL
    # Questions au professeur : ``[{"multi_select": bool, "options": n}]``,
    # une entrée par question (cf. ``questions.answer_shape``) ; ``None`` sinon.
    answer_shape: list[dict[str, Any]] | None = None
    # Édition globale demandée au tour (contexte ``course``) : rejouée à la
    # reprise du parent — mêmes tools, même prompt qu'à l'aller.
    allow_edit: bool = False
    # Run figé d'un sous-assistant d'édition : le parent figé est ici.
    delegation: Delegation | None = None
    # Usage d'un flux resté sans ligne à persister (sous-assistant seul) :
    # reporté sur le prochain segment persisté du tour.
    carried_usage: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)

    def expired(self) -> bool:
        return time.time() - self.created_at > PENDING_TTL_SECONDS

    def thread_ids(self) -> tuple[str, ...]:
        """Threads checkpointés que cette attente tient en vie — le run figé
        et, derrière une délégation, le run parent — à purger ensemble."""
        if self.delegation is None:
            return (self.thread_id,)
        return (self.thread_id, self.delegation.parent_thread_id)


_PENDING: dict[uuid.UUID, PendingInterrupt] = {}


def suspend(
    call: AIToolCall,
    *,
    kind: str,
    answer_shape: list[dict[str, Any]] | None = None,
    detail: Mapping[str, Any] | None = None,
) -> Any:
    """Fige le run en attendant le professeur (interrupt LangGraph — le flux
    SSE se ferme) ; à la reprise, RETOURNE la valeur de reprise (décision ou
    réponses). Le payload relaie au flux l'id d'appel (clé de reprise), le
    genre et, pour des questions, la forme de réponse attendue ; ``detail``
    (clés propres au genre — la cible d'une délégation) s'y ajoute.

    À appeler APRÈS validation des args : un échec de validation doit répondre
    immédiatement (aucun run figé) — et, le tool étant ré-exécuté depuis le
    début à la reprise, cette validation doit être idempotente.
    """
    payload: dict[str, Any] = {"tool_call_id": call.id or "?", "kind": kind}
    if answer_shape is not None:
        payload["answer_shape"] = answer_shape
    if detail:
        payload.update(detail)
    return agent_interrupt(payload)


def register(conversation_id: uuid.UUID, pending: PendingInterrupt) -> PendingInterrupt | None:
    """Enregistre LA reprise en attente d'une conversation (une seule à la
    fois) ; retourne l'entrée remplacée, dont l'appelant purge les threads.
    Seul un genre repris par une route s'enregistre (``ValueError`` sinon :
    une délégation se traite dans le flux, jamais ici)."""
    if pending.kind not in RESUMABLE_KINDS:
        raise ValueError(f"genre d'attente sans route de reprise : {pending.kind}")
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
    :func:`sweep_expired`, qui en purge les threads)."""
    if peek(conversation_id, tool_call_id, kind=kind) is None:
        return None
    return _PENDING.pop(conversation_id)


def drop(conversation_id: uuid.UUID) -> PendingInterrupt | None:
    """Abandonne la reprise en attente d'une conversation (nouveau message
    envoyé, conversation supprimée) ; l'appelant purge ses threads."""
    return _PENDING.pop(conversation_id, None)


def sweep_expired() -> list[PendingInterrupt]:
    """Retire toutes les reprises expirées ; l'appelant purge leurs threads
    (sans ce balayage, une conversation jamais rouverte garderait son
    checkpoint jusqu'au redémarrage)."""
    expired = [cid for cid, pending in _PENDING.items() if pending.expired()]
    return [_PENDING.pop(cid) for cid in expired]
