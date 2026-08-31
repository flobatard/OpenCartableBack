"""Helpers PURS de l'assistant de cours : contexte, citations, replay.

Aucune I/O ici (testable sans DB ni storage) :

- :func:`build_course_context` assemble le system prompt (consignes + cours en
  markdown, ids inclus pour les citations) ;
- :func:`format_block` produit la représentation textuelle d'un bloc — partagée
  entre le contexte et le tool ``read_block`` ; :func:`format_module` celle
  d'un module interactif (titre + code HTML/CSS/JS plafonné, tool
  ``read_module``) ;
- :func:`extract_sources` valide les citations ``oc-block:``/``oc-resource:``
  d'une réponse (ids hallucinés filtrés — le markdown, lui, n'est jamais
  réécrit : un id inconnu rend simplement un lien inerte côté front) ;
- :func:`replay_messages` reconstruit l'historique :class:`ChatMessage` à
  rejouer au modèle depuis les lignes persistées (troncature aux frontières de
  round, repli en texte des rounds d'outils issus d'un autre provider — les
  formats d'id de tool call ne sont pas interchangeables entre providers).
"""

import json
import re
import uuid
from collections.abc import Sequence

from app.core.ai import AIToolCall, ChatMessage
from app.models.ai_message import ROLE_ASSISTANT, ROLE_TOOL, ROLE_USER
from app.models.block import TYPE_DOCUMENT, TYPE_EXERCISE, TYPE_MODULE, TYPE_TEXT

# Plafond du contexte en CARACTÈRES (heuristique assumée : pas de tokenizer
# par provider). Au-delà, bascule en mode sommaire + tool read_block.
CONTEXT_MAX_CHARS = 60_000
SUMMARY_EXCERPT_CHARS = 300

# Replay : nombre max de messages persistés rejoués au modèle, et taille max
# d'un contenu d'outil replié en texte (round issu d'un autre provider).
REPLAY_MESSAGE_LIMIT = 30
FOLDED_TOOL_RESULT_CHARS = 500

# Plafond (caractères) du code d'un module lu par ``read_module`` — même ordre
# de grandeur que la lecture d'un PDF (contrainte contexte + persistance).
MODULE_MAX_CHARS = 40_000

_TYPE_LABELS = {
    TYPE_TEXT: "Texte",
    TYPE_EXERCISE: "Exercice",
    TYPE_DOCUMENT: "Document",
    TYPE_MODULE: "Module interactif",
}

_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_BLOCK_REF_RE = re.compile(rf"oc-block:({_UUID_RE})")
_RESOURCE_REF_RE = re.compile(rf"oc-resource:({_UUID_RE})")

_SYSTEM_PROMPT = """\
Vous êtes l'assistant pédagogique d'OpenCartable, aux côtés d'un professeur \
qui édite son cours. Vous l'aidez à explorer, critiquer et synthétiser ce \
cours : structure, clarté, progression pédagogique, exactitude, exercices et \
corrigés. Vouvoyez toujours votre interlocuteur et répondez en français, en \
markdown (formules LaTeX entre $…$ ou $$…$$ si utile).

Citez vos sources : quand votre réponse s'appuie sur un bloc du cours, \
insérez un lien markdown de la forme [titre du bloc](oc-block:<id>) ; pour \
une ressource de la bibliothèque, [nom de la ressource](oc-resource:<id>). \
Utilisez uniquement les ids présents dans le cours ci-dessous.

Outils à votre disposition : `read_block` relit un bloc en entier ; \
`read_resource_pdf` extrait le texte d'une ressource PDF de la bibliothèque ; \
`read_resource_image` vous montre une ressource image de la bibliothèque \
(PNG, JPEG, GIF ou WebP — nécessite un modèle acceptant les images) ; \
`read_module` lit le code HTML/CSS/JS d'un module interactif. \
Ne les appelez que si le contexte ci-dessous ne suffit pas.
"""

_SUMMARY_NOTICE = (
    "\n\n> Cours volumineux : seuls des extraits sont fournis ci-dessous — "
    "utilisez `read_block` pour lire un bloc en entier.\n"
)

TRUNCATED_HISTORY_NOTICE = (
    "\n\nNote : la conversation est longue, seuls ses derniers messages vous "
    "sont rejoués."
)


def format_block(block, index: int, modules_by_id: dict | None = None) -> str:
    """Représentation markdown complète d'un bloc, ids inclus (citations).

    ``modules_by_id`` (optionnel) donne le titre du module pointé par un bloc
    ``module`` — le code, lui, se lit avec ``read_module``.
    """
    title = block.title or _TYPE_LABELS.get(block.type, block.type)
    lines = [f"### Bloc {index} — {title} (id: {block.id})"]
    if block.description:
        lines.append(f"*{block.description}*")
    content = block.content or {}
    if block.type == TYPE_TEXT:
        lines.append(content.get("markdown", ""))
    elif block.type == TYPE_EXERCISE:
        lines.append(content.get("statement", ""))
        for i, question in enumerate(content.get("questions", []), start=1):
            if not isinstance(question, dict):
                continue
            lines.append(f"**Question {i}** (id: {question.get('id')}) : "
                         f"{question.get('statement', '')}")
            expected = question.get("expected_answer")
            if expected:
                lines.append(f"Réponse attendue (corrigé du professeur) : {expected}")
    elif block.type == TYPE_DOCUMENT:
        caption = content.get("caption")
        if caption:
            lines.append(caption)
        if block.resource_id:
            lines.append(f"Ressource pointée : oc-resource:{block.resource_id}")
        else:
            lines.append("(bloc document vide — aucune ressource pointée)")
    elif block.type == TYPE_MODULE:
        if block.module_id:
            module = (modules_by_id or {}).get(block.module_id)
            named = f" « {module.title} »" if module is not None else ""
            lines.append(
                f"Module interactif pointé{named} : oc-module:{block.module_id} "
                "(code lisible avec `read_module`)"
            )
        else:
            lines.append("(bloc module vide — aucun module pointé)")
    return "\n\n".join(line for line in lines if line)


def format_module(module, *, max_chars: int = MODULE_MAX_CHARS) -> str:
    """Représentation markdown d'un module interactif : titre, id, code.

    Les trois morceaux (HTML, CSS, JS) sont rendus en blocs de code ; au-delà
    de ``max_chars``, coupe nette avec mention de troncature (motif PDF).
    """
    lines = [f"### Module interactif — {module.title} (id: {module.id})"]
    for label, lang, code in (
        ("HTML", "html", module.html),
        ("CSS", "css", module.css),
        ("JavaScript", "javascript", module.js),
    ):
        if code:
            lines.append(f"**{label}**\n\n```{lang}\n{code}\n```")
        else:
            lines.append(f"**{label}** : (vide)")
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[Module tronqué : plafond de lecture atteint]"
    return text


def _excerpt_block(block, index: int, modules_by_id: dict | None = None) -> str:
    """En-tête + extrait court, pour le mode sommaire."""
    full = format_block(block, index, modules_by_id)
    header, _, body = full.partition("\n\n")
    body = " ".join(body.split())
    if len(body) > SUMMARY_EXCERPT_CHARS:
        body = body[:SUMMARY_EXCERPT_CHARS] + "…"
    return f"{header}\n\n{body}" if body else header


def _libraries_section(resources, modules) -> str:
    lines = ["\n## Bibliothèque de ressources du cours"]
    if resources:
        for r in resources:
            lines.append(
                f"- {r.original_name} (id: {r.id}, type: {r.type}, mime: {r.mime}, "
                f"taille: {r.size} octets, statut: {r.status})"
            )
    else:
        lines.append("(aucune ressource)")
    lines.append("\n## Modules interactifs du cours")
    if modules:
        lines.extend(f"- {m.title} (id: {m.id})" for m in modules)
    else:
        lines.append("(aucun module)")
    return "\n".join(lines)


def build_course_context(
    course, blocks, resources, modules, *, max_chars: int = CONTEXT_MAX_CHARS
) -> str:
    """System prompt complet : consignes + cours en markdown + bibliothèques.

    Les blocs arrivent déjà triés (``position, id``, contrat des services).
    Si le rendu complet dépasse ``max_chars``, bascule en **mode sommaire**
    (extraits + invite à ``read_block``) — jamais de coupe au milieu d'un bloc.
    """
    head = [_SYSTEM_PROMPT, f"\n# Cours : {course.title}"]
    if course.description:
        head.append(course.description)
    tail = _libraries_section(resources, modules)
    modules_by_id = {m.id: m for m in modules}

    full_blocks = "\n\n".join(
        format_block(b, i, modules_by_id) for i, b in enumerate(blocks, start=1)
    )
    context = "\n\n".join([*head, "\n## Contenu du cours", full_blocks, tail])
    if len(context) <= max_chars:
        return context

    summary_blocks = "\n\n".join(
        _excerpt_block(b, i, modules_by_id) for i, b in enumerate(blocks, start=1)
    )
    return "\n\n".join(
        [*head, _SUMMARY_NOTICE, "\n## Contenu du cours (extraits)", summary_blocks, tail]
    )


def extract_sources(
    content: str,
    block_ids: set[uuid.UUID],
    resource_ids: set[uuid.UUID],
) -> dict[str, list[str]]:
    """Citations validées d'une réponse : ``{"blocks": [...], "resources": [...]}``.

    Les ids cités mais inconnus du cours (hallucinations) sont filtrés ;
    l'ordre de première apparition est conservé, sans doublon.
    """

    def _collect(pattern: re.Pattern[str], known: set[uuid.UUID]) -> list[str]:
        seen: list[str] = []
        for raw in pattern.findall(content):
            try:
                parsed = uuid.UUID(raw)
            except ValueError:  # pragma: no cover — la regex garantit la forme
                continue
            if parsed in known and str(parsed) not in seen:
                seen.append(str(parsed))
        return seen

    return {
        "blocks": _collect(_BLOCK_REF_RE, block_ids),
        "resources": _collect(_RESOURCE_REF_RE, resource_ids),
    }


def _fold_tool_round(assistant_row, tool_rows) -> ChatMessage:
    """Replie en texte un round d'outils issu d'un autre provider."""
    parts = [assistant_row.content] if assistant_row.content else []
    results_by_id = {t.tool_call_id: t for t in tool_rows}
    for call in assistant_row.tool_calls or []:
        name = call.get("name", "?")
        args = json.dumps(call.get("arguments", {}), ensure_ascii=False)
        result = results_by_id.get(call.get("id"))
        outcome = ""
        if result is not None:
            snippet = result.content[:FOLDED_TOOL_RESULT_CHARS]
            if len(result.content) > FOLDED_TOOL_RESULT_CHARS:
                snippet += "…"
            state = "échec" if result.is_error else "résultat"
            outcome = f" → {state} : {snippet}"
        parts.append(f"[Outil {name}({args}){outcome}]")
    return ChatMessage(role="assistant", content="\n\n".join(parts))


def replay_messages(
    rows: Sequence, current_provider: str, *, limit: int = REPLAY_MESSAGE_LIMIT
) -> tuple[list[ChatMessage], bool]:
    """Historique à rejouer au modèle depuis les lignes ``ai_messages`` triées.

    Retourne ``(messages, truncated)``. Troncature aux ``limit`` derniers
    messages **sans couper un round** : les tours ``tool`` orphelins de tête
    (leur assistant est hors fenêtre) sont écartés. Les rounds d'outils générés
    par un AUTRE provider que ``current_provider`` sont repliés en texte
    (:func:`_fold_tool_round`) au lieu d'être rejoués nativement.
    """
    truncated = len(rows) > limit
    window = list(rows[-limit:])
    while window and window[0].role == ROLE_TOOL:
        window.pop(0)

    messages: list[ChatMessage] = []
    i = 0
    while i < len(window):
        row = window[i]
        if row.role == ROLE_USER:
            messages.append(ChatMessage(role="user", content=row.content))
            i += 1
            continue
        if row.role == ROLE_ASSISTANT and row.tool_calls:
            tool_rows = []
            j = i + 1
            while j < len(window) and window[j].role == ROLE_TOOL:
                tool_rows.append(window[j])
                j += 1
            # Repli en texte : round d'un autre provider (ids de tool call non
            # interchangeables), ou round incomplet (résultats jamais persistés
            # — erreur mid-round : des tool_calls non appariés feraient un 400).
            if (row.provider and row.provider != current_provider) or not tool_rows:
                messages.append(_fold_tool_round(row, tool_rows))
            else:
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=row.content,
                        tool_calls=[
                            AIToolCall(
                                id=call.get("id") or "",
                                name=call.get("name", ""),
                                arguments=call.get("arguments") or {},
                            )
                            for call in row.tool_calls
                        ],
                    )
                )
                messages.extend(
                    ChatMessage(
                        role="tool",
                        content=t.content,
                        tool_call_id=t.tool_call_id or "",
                        is_error=t.is_error,
                    )
                    for t in tool_rows
                )
            i = j
            continue
        # Assistant sans tool_calls (ou ligne inattendue) : texte simple.
        messages.append(ChatMessage(role="assistant", content=row.content))
        i += 1
    return messages, truncated
