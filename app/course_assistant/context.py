"""Contexte d'un tour d'assistant : références courtes, sommaire, sources.

Helpers purs (aucune I/O, testables sans DB ni storage) :

- :func:`build_refs` numérote l'instantané du cours (``B1``/``R1``/``M1`` —
  et ``Q1…`` pour les questions du bloc exercice édité) ;
- :func:`system_prompt_for` donne le system prompt du contexte de
  conversation — ``course``
  (:data:`~app.course_assistant.prompts.COURSE_SYSTEM_PROMPT`) ou un contexte
  d'édition (descripteur :class:`~app.course_assistant.editing.EditContext`).
  Il est **statique** (cacheable par le provider) : aucun contenu de cours ;
- :func:`build_turn_context` assemble le **contexte du tour** : la cible
  d'un contexte d'édition (``focus_block`` OU ``focus_module``, rendue en
  entier) puis le **sommaire** du cours (:func:`render_outline` :
  :mod:`app.course_assistant.render`) — jamais le contenu des autres blocs,
  que le modèle lit avec ``read_block`` ; le tuteur d'exercice réutilise
  :func:`render_outline` et :func:`turn_message` ;
- :func:`turn_message` place ce contexte en tête du message utilisateur du
  tour, **après l'historique** : system prompt, tools et historique forment
  un préfixe stable d'un tour à l'autre (cache de prompt), seuls le contexte
  et la demande changent ;
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

from app.course_assistant.editing.base import EditContext
from app.course_assistant.prompts import COURSE_SYSTEM_PROMPT
from app.course_assistant.refs import CourseRefs
from app.course_assistant.render import (
    FOCUS_MODULE_MAX_CHARS,
    block_title,
    focus_pointer,
    format_block,
    format_module,
    libraries_section,
    outline_block,
)
from app.models.block import TYPE_EXERCISE

# Garde-fou du sommaire en CARACTÈRES (heuristique assumée : pas de tokenizer
# par provider) : au-delà, les plans internes des blocs sont omis.
OUTLINE_MAX_CHARS = 12_000

OUTLINE_NOTICE = (
    "Sommaire seulement : le contenu d'un bloc se lit avec `read_block` "
    "(sa référence en paramètre)."
)
TEACHER_LABEL = "Demande du professeur"
_TURN_SEPARATOR = "\n\n---\n\n"

_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_BLOCK_REF_RE = re.compile(rf"oc-block:({_UUID_RE})")
_RESOURCE_REF_RE = re.compile(rf"oc-resource:({_UUID_RE})")


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


def system_prompt_for(edit: EditContext | None) -> str:
    """System prompt du contexte de conversation — statique, sans contenu de
    cours : celui du descripteur d'édition, sinon celui du contexte ``course``."""
    return COURSE_SYSTEM_PROMPT if edit is None else edit.system_prompt


def render_outline(
    course,
    refs: CourseRefs,
    *,
    focus_block=None,
    focus_note: str = "bloc mis en avant",
    headings: bool = True,
) -> str:
    """Sommaire du cours : en-tête (titre, description), une entrée
    :func:`outline_block` par bloc (le ``focus_block``, s'il est donné, y est
    remplacé par un pointeur d'une ligne annoté ``focus_note``), puis les
    bibliothèques. Au-delà de :data:`OUTLINE_MAX_CHARS`, re-rendu sans les
    plans internes (titres de blocs seuls) — jamais de contenu de bloc.
    Partagé par :func:`build_turn_context` et le tuteur d'exercice."""
    head = [f"# Cours : {course.title}"]
    if course.description:
        head.append(course.description)
    entries = []
    for entry in refs.entries["block"]:
        block = entry.entity
        if focus_block is not None and block.id == focus_block.id:
            entries.append(focus_pointer(block, refs, focus_note))
        else:
            entries.append(outline_block(block, refs, headings=headings))
    text = "\n\n".join(
        [
            *head,
            f"\n## Sommaire du cours\n\n{OUTLINE_NOTICE}",
            "\n\n".join(entries) if entries else "(aucun bloc)",
            libraries_section(refs),
        ]
    )
    if headings and len(text) > OUTLINE_MAX_CHARS:
        return render_outline(
            course, refs, focus_block=focus_block, focus_note=focus_note, headings=False
        )
    return text


def build_turn_context(
    course,
    refs: CourseRefs,
    *,
    focus_block=None,
    focus_module=None,
    edit: EditContext | None = None,
) -> str:
    """Contexte du tour d'assistant : cible d'édition (en entier) puis sommaire.

    Contexte d'édition (``edit`` et **exactement une** cible — ``focus_block``
    ou ``focus_module`` — toujours ensemble) : la cible est rendue **en
    entier** dans une section dédiée (« Bloc / Module en cours d'édition »),
    le professeur édite CETTE cible et l'assistant doit toujours en voir
    l'état exact ; un bloc édité est remplacé par un pointeur dans le
    sommaire. Hors contexte d'édition : le sommaire seul.
    """
    focus = focus_block if focus_block is not None else focus_module
    if focus_block is not None and focus_module is not None:
        raise ValueError("une seule cible d'édition (bloc OU module)")
    if (focus is None) != (edit is None):
        raise ValueError("la cible d'édition et edit vont ensemble (contexte d'édition)")
    sections: list[str] = []
    if focus_block is not None:
        sections += ["## Bloc en cours d'édition", format_block(focus_block, refs)]
    elif focus_module is not None:
        sections += [
            "## Module en cours d'édition",
            format_module(focus_module, refs, max_chars=FOCUS_MODULE_MAX_CHARS),
        ]
    sections.append(
        render_outline(course, refs, focus_block=focus_block, focus_note="bloc en cours d'édition")
    )
    return "\n\n".join(sections)


def teacher_message(content: str) -> str:
    """Demande du professeur, titrée pour la distinguer du contexte du tour."""
    return f"## {TEACHER_LABEL}\n\n{content}"


def turn_message(context: str, message: str, *, notice: str | None = None) -> str:
    """Message utilisateur du tour : le contexte (:func:`build_turn_context`
    ou celui du tuteur), une éventuelle note (historique tronqué…), puis —
    après un séparateur ``---`` — le message réel, déjà étiqueté
    (:func:`teacher_message`, ``student_message`` du tuteur). Le contexte
    n'est jamais persisté : la ligne ``user`` garde le message brut."""
    parts = [context]
    if notice:
        parts.append(notice)
    return "\n\n".join(parts) + _TURN_SEPARATOR + message


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
