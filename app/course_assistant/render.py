"""Rendu textuel du cours pour le modèle : blocs, modules, bibliothèques.

Helpers purs (aucune I/O), partagés entre le contexte du tour
(:mod:`app.course_assistant.context`), les tools de lecture (``read_block``,
``read_module``) et le tuteur d'exercice. Le modèle ne voit **jamais
d'UUID** : chaque entité est désignée par sa référence courte (``B3``,
``R2``, ``M1``, ``Q1`` — :class:`~app.course_assistant.refs.CourseRefs`).
"""

import uuid

from app.course_assistant.refs import CourseRefs
from app.models.block import TYPE_DOCUMENT, TYPE_EXERCISE, TYPE_MODULE, TYPE_TEXT

# Extrait d'un bloc en mode sommaire (contexte trop long pour tout rendre).
SUMMARY_EXCERPT_CHARS = 300

# Plafond (caractères) du code d'un module lu par ``read_module`` — même ordre
# de grandeur que la lecture d'un PDF (contrainte contexte + persistance).
MODULE_MAX_CHARS = 40_000
# Plafond du module EN COURS D'ÉDITION, plus large : c'est le sujet du tour,
# l'assistant doit en voir l'état exact (miroir du bloc édité, rendu en entier).
FOCUS_MODULE_MAX_CHARS = 120_000

_TYPE_LABELS = {
    TYPE_TEXT: "Texte",
    TYPE_EXERCISE: "Exercice",
    TYPE_DOCUMENT: "Document",
    TYPE_MODULE: "Module interactif",
}


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
        questions = [q for q in content.get("questions", []) if isinstance(q, dict)]
        if not questions:
            lines.append("(aucune question)")
        for i, question in enumerate(questions, start=1):
            # Jamais l'id de la question (règle « pas d'identifiant long ») :
            # sa référence courte quand le bloc est celui en cours d'édition.
            ref = _question_ref(refs, question)
            label = f"**Question {i}**" + (f" (ref: {ref})" if ref else "")
            lines.append(f"{label} : {question.get('statement', '')}")
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


def _question_ref(refs: CourseRefs, question: dict) -> str | None:
    """Référence courte d'une question (``Q2``) si elle est numérotée dans
    l'instantané (questions du bloc édité), ``None`` sinon."""
    try:
        return refs.ref_of("question", uuid.UUID(str(question.get("id"))))
    except ValueError:
        return None


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


def excerpt_block(block, refs: CourseRefs) -> str:
    """En-tête + extrait court, pour le mode sommaire."""
    full = format_block(block, refs)
    header, _, body = full.partition("\n\n")
    body = " ".join(body.split())
    if len(body) > SUMMARY_EXCERPT_CHARS:
        body = body[:SUMMARY_EXCERPT_CHARS] + "…"
    return f"{header}\n\n{body}" if body else header


def libraries_section(refs: CourseRefs) -> str:
    """Sections « Bibliothèque de ressources » et « Modules interactifs »."""
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


def focus_pointer(block, refs: CourseRefs, note: str) -> str:
    """Pointeur d'une ligne remplaçant le bloc mis en avant dans la liste du
    cours (son contenu complet vit dans la section dédiée de l'appelant —
    « Bloc en cours d'édition », « Exercice en cours de résolution »…)."""
    ref = refs.ref_of("block", block.id) or "B?"
    return (
        f"### Bloc {ref[1:]} — {block_title(block)} (ref: {ref})\n\n"
        f"({note} — contenu complet dans la section dédiée ci-dessus)"
    )
