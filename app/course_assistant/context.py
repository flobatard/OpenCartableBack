"""Contexte d'un tour d'assistant : références courtes, system prompt, sources.

Helpers purs (aucune I/O, testables sans DB ni storage) :

- :func:`build_refs` numérote l'instantané du cours (``B1``/``R1``/``M1`` —
  et ``Q1…`` pour les questions du bloc exercice édité) ;
- :func:`build_course_context` assemble le system prompt : consignes du
  contexte de conversation — ``course``
  (:data:`~app.course_assistant.prompts.COURSE_SYSTEM_PROMPT`) ou un
  contexte d'édition (descripteur
  :class:`~app.course_assistant.editing.EditContext`, dont la cible
  ``focus_block`` OU ``focus_module`` est rendue en entier) — puis le cours
  en markdown (:mod:`app.course_assistant.render`) et ses bibliothèques ;
  :func:`assemble_context` en est l'assembleur commun, réutilisé par le tuteur
  d'exercice ;
- :func:`extract_sources` valide les citations ``oc-block:``/``oc-resource:``
  d'une réponse **déjà réécrite en UUID** ; les ids hallucinés sont filtrés —
  le markdown, lui, n'est jamais réécrit au-delà de la résolution des
  références (un id inconnu rend un lien inerte côté front).

Le modèle ne voit jamais d'UUID — seule exception : les liens ``oc-resource:``/
``oc-module:`` recopiés verbatim DANS le contenu d'une proposition d'édition.
Les fragments de prompt vivent dans :mod:`app.course_assistant.prompts`
(feuille du graphe d'imports), le replay de l'historique dans
:mod:`app.course_assistant.replay`.
"""

import re
import uuid
from collections.abc import Sequence

from app.course_assistant.editing.base import EditContext
from app.course_assistant.prompts import COURSE_SYSTEM_PROMPT
from app.course_assistant.refs import CourseRefs
from app.course_assistant.render import (
    FOCUS_MODULE_MAX_CHARS,
    block_title,
    excerpt_block,
    focus_pointer,
    format_block,
    format_module,
    libraries_section,
)
from app.models.block import TYPE_EXERCISE

# Plafond du contexte en CARACTÈRES (heuristique assumée : pas de tokenizer
# par provider). Au-delà, bascule en mode sommaire + tool read_block.
CONTEXT_MAX_CHARS = 60_000

_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_BLOCK_REF_RE = re.compile(rf"oc-block:({_UUID_RE})")
_RESOURCE_REF_RE = re.compile(rf"oc-resource:({_UUID_RE})")

_SUMMARY_NOTICE = (
    "\n\n> Cours volumineux : seuls des extraits sont fournis ci-dessous — "
    "utilisez `read_block` pour lire un bloc en entier.\n"
)


def build_refs(
    blocks, resources, modules, *, focus_block=None, question_refs=None
) -> CourseRefs:
    """Références courtes du tour — blocs déjà triés (``position, id``), le
    titre affiché d'un bloc sans titre étant son libellé de type.

    ``focus_block`` (bloc en cours d'édition) de type exercice : ses questions
    reçoivent les références ``Q1…`` — ``question_refs`` (mapping capturé à
    l'interrupt) rejoue la numérotation du tour lors d'une reprise HITL
    (docstring de :mod:`app.course_assistant.refs`).
    """
    questions: list = []
    if focus_block is not None and focus_block.type == TYPE_EXERCISE:
        questions = list((focus_block.content or {}).get("questions") or [])
    return CourseRefs.build(
        blocks,
        resources,
        modules,
        block_titles={b.id: block_title(b) for b in blocks},
        questions=questions,
        question_refs=question_refs,
    )


def build_course_context(
    course,
    refs: CourseRefs,
    *,
    max_chars: int = CONTEXT_MAX_CHARS,
    focus_block=None,
    focus_module=None,
    edit: EditContext | None = None,
) -> str:
    """System prompt complet : consignes + cours en markdown + bibliothèques.

    ``refs`` porte l'instantané du cours (:func:`build_refs`). Si le rendu
    complet dépasse ``max_chars``, bascule en **mode sommaire** (extraits +
    invite à ``read_block``) — jamais de coupe au milieu d'un bloc.

    Contexte d'édition (``edit`` et **exactement une** cible — ``focus_block``
    ou ``focus_module`` — toujours ensemble) : le prompt est celui du
    descripteur (mission + règles d'édition du contexte), et la cible est
    rendue **en entier** dans une section dédiée (« Bloc / Module en cours
    d'édition ») — y compris en mode sommaire (le professeur édite CETTE
    cible, l'assistant doit toujours en voir l'état exact) ; un bloc édité est
    en outre remplacé par un pointeur d'une ligne dans la liste du cours.
    """
    focus = focus_block if focus_block is not None else focus_module
    if focus_block is not None and focus_module is not None:
        raise ValueError("une seule cible d'édition (bloc OU module)")
    if (focus is None) != (edit is None):
        raise ValueError("la cible d'édition et edit vont ensemble (contexte d'édition)")
    prompt = COURSE_SYSTEM_PROMPT if edit is None else edit.system_prompt
    focus_section: list[str] = []
    if focus_block is not None:
        focus_section = ["\n## Bloc en cours d'édition", format_block(focus_block, refs)]
    elif focus_module is not None:
        focus_section = [
            "\n## Module en cours d'édition",
            format_module(focus_module, refs, max_chars=FOCUS_MODULE_MAX_CHARS),
        ]
    return assemble_context(
        prompt,
        course,
        refs,
        focus_section=focus_section,
        focus_block=focus_block,
        focus_note="bloc en cours d'édition",
        max_chars=max_chars,
    )


def assemble_context(
    prompt: str,
    course,
    refs: CourseRefs,
    *,
    focus_section: Sequence[str] = (),
    focus_block=None,
    focus_note: str = "bloc mis en avant",
    max_chars: int = CONTEXT_MAX_CHARS,
) -> str:
    """Assemblage commun des system prompts adossés à un cours : ``prompt``
    (mission + règles), en-tête du cours, ``focus_section`` (sections propres
    à l'appelant, rendues AVANT le cours et conservées en mode sommaire),
    contenu du cours (le ``focus_block``, s'il est donné, y est remplacé par
    un pointeur d'une ligne annoté ``focus_note``) et bibliothèques. Partagé
    par :func:`build_course_context` et le tuteur d'exercice élève
    (:mod:`app.student_exercises.context`).
    """
    head = [prompt, f"\n# Cours : {course.title}"]
    if course.description:
        head.append(course.description)
    tail = libraries_section(refs)
    blocks = [entry.entity for entry in refs.entries["block"]]

    def _render(block, renderer) -> str:
        if focus_block is not None and block.id == focus_block.id:
            return focus_pointer(block, refs, focus_note)
        return renderer(block, refs)

    full_blocks = "\n\n".join(_render(b, format_block) for b in blocks)
    context = "\n\n".join([*head, *focus_section, "\n## Contenu du cours", full_blocks, tail])
    if len(context) <= max_chars:
        return context

    summary_blocks = "\n\n".join(_render(b, excerpt_block) for b in blocks)
    return "\n\n".join(
        [
            *head,
            *focus_section,
            _SUMMARY_NOTICE,
            "\n## Contenu du cours (extraits)",
            summary_blocks,
            tail,
        ]
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
