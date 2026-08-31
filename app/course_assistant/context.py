"""Helpers PURS de l'assistant de cours : contexte, citations, replay.

Aucune I/O ici (testable sans DB ni storage) :

- :func:`build_course_context` assemble le system prompt (consignes + cours en
  markdown). Le modèle ne voit **jamais d'UUID** : chaque bloc, ressource et
  module est désigné par sa référence courte (``B3``, ``R2``, ``M1`` —
  :class:`~app.course_assistant.refs.CourseRefs`), pour les tools comme pour
  les citations ;
- :func:`format_block` produit la représentation textuelle d'un bloc — partagée
  entre le contexte et le tool ``read_block`` ; :func:`format_module` celle
  d'un module interactif (titre + code HTML/CSS/JS plafonné, tool
  ``read_module``) ;
- :func:`extract_sources` valide les citations ``oc-block:``/``oc-resource:``
  d'une réponse **déjà réécrite en UUID** (``CitationRewriter``) ; les ids
  hallucinés sont filtrés — le markdown, lui, n'est jamais réécrit au-delà de
  la résolution des références : un id inconnu rend simplement un lien inerte
  côté front ;
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
from app.course_assistant.refs import CourseRefs
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
markdown.

Formules mathématiques — règle stricte : utilisez EXCLUSIVEMENT les \
délimiteurs dollar, seule syntaxe rendue par l'application. En ligne : \
$u_{n+1} = a u_n + b$ ; formule centrée, seule sur sa ligne : \
$$u_n = (u_0 - \\alpha) a^n + \\alpha$$ \
N'écrivez JAMAIS \\( … \\), \\[ … \\], ( … ) autour d'une expression, ni \
\\begin{equation} : ces notations s'afficheraient en texte brut et la \
formule serait illisible. Toute expression mathématique, même un simple \
symbole comme $a$ ou $\\alpha$, doit être entre dollars.

Chaque bloc, ressource et module du cours porte une référence courte, \
indiquée entre parenthèses (ref: …) : B1, B2… pour les blocs dans l'ordre du \
cours, R1, R2… pour les ressources, M1, M2… pour les modules. Ces références \
sont un identifiant technique : utilisez-les pour appeler les outils et comme \
cible des liens de citation, mais ne les écrivez JAMAIS dans le texte visible \
de vos réponses (ni identifiant long). Dans votre prose, désignez toujours \
les blocs, ressources et modules par leur titre ou leur nom, tels qu'ils \
apparaissent dans le cours ci-dessous — par exemple « le bloc Introduction », \
jamais « B1 ».

Citez vos sources : quand votre réponse s'appuie sur un bloc du cours, \
insérez un lien markdown de la forme [titre du bloc](oc-block:<ref>), par \
exemple [Introduction](oc-block:B1) ; pour une ressource de la bibliothèque, \
[nom de la ressource](oc-resource:<ref>), par exemple [Sujet](oc-resource:R2). \
La référence courte reste dans la parenthèse du lien ; le texte du lien est \
toujours le vrai titre. Utilisez uniquement les références présentes dans le \
cours ci-dessous.

Outils à votre disposition (ils prennent la référence en paramètre) : \
`read_block` relit un bloc en entier ; `read_resource_pdf` extrait le texte \
d'une ressource PDF de la bibliothèque ; `read_resource_image` vous montre une \
ressource image de la bibliothèque (PNG, JPEG, GIF ou WebP — nécessite un \
modèle acceptant les images) ; `read_module` lit le code HTML/CSS/JS d'un \
module interactif. Ne les appelez que si le contexte ci-dessous ne suffit pas.
"""

_SUMMARY_NOTICE = (
    "\n\n> Cours volumineux : seuls des extraits sont fournis ci-dessous — "
    "utilisez `read_block` pour lire un bloc en entier.\n"
)

TRUNCATED_HISTORY_NOTICE = (
    "\n\nNote : la conversation est longue, seuls ses derniers messages vous "
    "sont rejoués."
)


def block_title(block) -> str:
    """Titre affiché d'un bloc : le sien, sinon le libellé de son type."""
    return block.title or _TYPE_LABELS.get(block.type, block.type)


def format_block(block, refs: CourseRefs) -> str:
    """Représentation markdown complète d'un bloc, références courtes incluses
    (en-tête ``### Bloc 3 — Titre (ref: B3)`` ; ressource ou module pointé
    nommé avec sa référence — le code d'un module se lit avec ``read_module``).
    """
    ref = refs.ref_of("block", block.id) or "B?"
    lines = [f"### Bloc {ref[1:]} — {block_title(block)} (ref: {ref})"]
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
            lines.append(_pointed("Ressource pointée", refs, "resource", block.resource_id))
        else:
            lines.append("(bloc document vide — aucune ressource pointée)")
    elif block.type == TYPE_MODULE:
        if block.module_id:
            lines.append(
                _pointed("Module interactif pointé", refs, "module", block.module_id)
                + " (code lisible avec `read_module`)"
            )
        else:
            lines.append("(bloc module vide — aucun module pointé)")
    return "\n\n".join(line for line in lines if line)


def _pointed(label: str, refs: CourseRefs, kind, entity_id: uuid.UUID) -> str:
    """« Ressource pointée : « nom » (ref: R2) » — ou mention d'une cible
    absente de l'instantané (supprimée, ou ressource pas encore confirmée)."""
    ref = refs.ref_of(kind, entity_id)
    if ref is None:
        return f"{label} : (introuvable dans la bibliothèque du cours)"
    entry = refs.by_ref(kind, ref)
    return f"{label} : « {entry.title} » (ref: {ref})"


def format_module(module, refs: CourseRefs, *, max_chars: int = MODULE_MAX_CHARS) -> str:
    """Représentation markdown d'un module interactif : titre, référence, code.

    Les trois morceaux (HTML, CSS, JS) sont rendus en blocs de code ; au-delà
    de ``max_chars``, coupe nette avec mention de troncature (motif PDF).
    """
    ref = refs.ref_of("module", module.id) or "M?"
    lines = [f"### Module interactif — {module.title} (ref: {ref})"]
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


def _excerpt_block(block, refs: CourseRefs) -> str:
    """En-tête + extrait court, pour le mode sommaire."""
    full = format_block(block, refs)
    header, _, body = full.partition("\n\n")
    body = " ".join(body.split())
    if len(body) > SUMMARY_EXCERPT_CHARS:
        body = body[:SUMMARY_EXCERPT_CHARS] + "…"
    return f"{header}\n\n{body}" if body else header


def _libraries_section(refs: CourseRefs) -> str:
    lines = ["\n## Bibliothèque de ressources du cours"]
    if refs.entries["resource"]:
        for entry in refs.entries["resource"]:
            r = entry.entity
            lines.append(
                f"- {r.original_name} (ref: {entry.ref}, type: {r.type}, mime: {r.mime}, "
                f"taille: {r.size} octets, statut: {r.status})"
            )
    else:
        lines.append("(aucune ressource)")
    lines.append("\n## Modules interactifs du cours")
    if refs.entries["module"]:
        lines.extend(f"- {e.title} (ref: {e.ref})" for e in refs.entries["module"])
    else:
        lines.append("(aucun module)")
    return "\n".join(lines)


def build_refs(blocks, resources, modules) -> CourseRefs:
    """Références courtes du tour — blocs déjà triés (``position, id``), le
    titre affiché d'un bloc sans titre étant son libellé de type."""
    return CourseRefs.build(
        blocks, resources, modules, block_titles={b.id: block_title(b) for b in blocks}
    )


def build_course_context(course, refs: CourseRefs, *, max_chars: int = CONTEXT_MAX_CHARS) -> str:
    """System prompt complet : consignes + cours en markdown + bibliothèques.

    ``refs`` porte l'instantané du cours (:func:`build_refs`). Si le rendu
    complet dépasse ``max_chars``, bascule en **mode sommaire** (extraits +
    invite à ``read_block``) — jamais de coupe au milieu d'un bloc.
    """
    head = [_SYSTEM_PROMPT, f"\n# Cours : {course.title}"]
    if course.description:
        head.append(course.description)
    tail = _libraries_section(refs)
    blocks = [entry.entity for entry in refs.entries["block"]]

    full_blocks = "\n\n".join(format_block(b, refs) for b in blocks)
    context = "\n\n".join([*head, "\n## Contenu du cours", full_blocks, tail])
    if len(context) <= max_chars:
        return context

    summary_blocks = "\n\n".join(_excerpt_block(b, refs) for b in blocks)
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
