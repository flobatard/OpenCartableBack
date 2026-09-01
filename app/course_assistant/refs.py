"""Références courtes des entités du cours pour l'assistant IA — helpers PURS.

Problème traité : un UUID (36 caractères d'hexadécimal aléatoire, ~20 tokens)
doit être **recopié verbatim** par le modèle pour appeler un tool ou citer une
source, et les modèles le déforment régulièrement (fin fusionnée avec un autre
id, groupe sauté). Trois parades standards, combinées ici :

1. **Handles courts côté modèle, UUID côté serveur** : le prompt et les tools
   ne manipulent que des références ``B1, B2…`` (blocs, dans l'ordre
   d'affichage), ``R1…`` (ressources) et ``M1…`` (modules) ; le serveur tient la
   table de correspondance (:class:`CourseRefs`). Les références sont
   **propres à un tour** (instantané du cours) — un tool call rejoué depuis
   l'historique garde son ``B3`` d'origine, dont le résultat porte de toute
   façon le titre du bloc.
2. **Résolveur tolérant** (:meth:`CourseRefs.resolve`), chaîne : référence
   (``B3``, ``b3``, ``bloc 3``, ``3``) → UUID exact → **plus long préfixe
   commun** d'UUID (≥ :data:`UUID_PREFIX_MIN_HEX` hex — un id halluciné qui
   diverge après 19 caractères est ainsi rattrapé, là où un simple
   ``startswith`` échouerait) → titre exact (casse/accents ignorés) → titre
   approchant (``difflib``). Échec ou ambiguïté = message **actionnable**
   listant les candidats, que le modèle lit pour se corriger au tour suivant.
3. Les specs des tools portent un ``enum`` des références valides (construit
   par tour dans :mod:`app.course_assistant.tools`) : les providers à décodage
   contraint garantissent alors une valeur valide.

Les citations ``[titre](oc-block:B3)`` / ``oc-resource:R2`` écrites par le
modèle sont **réécrites en UUID** avant tout usage (front, ``extract_sources``,
persistance) par :class:`CitationRewriter`, qui travaille **en flux** (retenue
minimale d'un suffixe pouvant amorcer une citation) pour que le texte streamé
soit identique au texte persisté — le contrat SSE et le front ne changent pas.
"""

import difflib
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Literal

Kind = Literal["block", "resource", "module"]

# Préfixe commun minimal (hex, tirets retirés) pour rattraper un UUID déformé :
# 8 hex = 32 bits, collision entre deux ids d'un même cours ≈ impossible.
UUID_PREFIX_MIN_HEX = 8
# Seuil difflib (0..1) du rapprochement par titre.
TITLE_FUZZY_CUTOFF = 0.6
# Nombre max de candidats listés dans un message d'échec.
CANDIDATES_LISTED = 40

_KIND_PREFIX: dict[Kind, str] = {"block": "B", "resource": "R", "module": "M"}
_KIND_WORDS: dict[Kind, frozenset[str]] = {
    "block": frozenset({"b", "bloc", "block"}),
    "resource": frozenset({"r", "ressource", "resource"}),
    "module": frozenset({"m", "module"}),
}
_KIND_LABELS: dict[Kind, tuple[str, str]] = {
    "block": ("Bloc", "Blocs du cours"),
    "resource": ("Ressource", "Ressources du cours"),
    "module": ("Module", "Modules du cours"),
}
_REF_RE = re.compile(r"^(?:([a-z]+)\s*)?#?(\d+)$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")


def _normalize(text: str) -> str:
    """Casse et accents ignorés, espaces repliés (comparaison de titres)."""
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )
    return " ".join(stripped.casefold().split())


@dataclass(frozen=True)
class RefEntry:
    ref: str
    id: uuid.UUID
    title: str
    entity: object


@dataclass(frozen=True)
class Resolution:
    """Résultat de :meth:`CourseRefs.resolve` : l'entrée trouvée, ou le
    message d'échec (jamais les deux)."""

    entry: RefEntry | None = None
    error: str | None = None


@dataclass
class CourseRefs:
    """Table de correspondance références courtes ↔ entités, pour un tour."""

    entries: dict[Kind, list[RefEntry]] = field(
        default_factory=lambda: {"block": [], "resource": [], "module": []}
    )

    @classmethod
    def build(cls, blocks, resources, modules, *, block_titles: dict | None = None) -> "CourseRefs":
        """Numérote blocs (ordre reçu = ordre d'affichage), ressources et
        modules. ``block_titles`` (optionnel) donne le titre affiché d'un bloc
        sans titre (libellé de type, cf. ``context.format_block``)."""
        refs = cls()
        titles = block_titles or {}
        for kind, items, title_of in (
            ("block", blocks, lambda b: b.title or titles.get(b.id) or b.type),
            ("resource", resources, lambda r: r.original_name),
            ("module", modules, lambda m: m.title),
        ):
            prefix = _KIND_PREFIX[kind]
            refs.entries[kind] = [
                RefEntry(ref=f"{prefix}{i}", id=item.id, title=title_of(item), entity=item)
                for i, item in enumerate(items, start=1)
            ]
        return refs

    # ------------------------------------------------------------ lookups

    def ref_of(self, kind: Kind, entity_id: uuid.UUID) -> str | None:
        for entry in self.entries[kind]:
            if entry.id == entity_id:
                return entry.ref
        return None

    def refs(self, kind: Kind) -> list[str]:
        return [entry.ref for entry in self.entries[kind]]

    def ids(self, kind: Kind) -> set[uuid.UUID]:
        return {entry.id for entry in self.entries[kind]}

    def by_ref(self, kind: Kind, ref: str) -> RefEntry | None:
        """Référence stricte (``B3``/``b3``/``bloc 3``/``3``) ou UUID exact."""
        entries = self.entries[kind]
        raw = ref.strip().casefold()
        match = _REF_RE.match(raw)
        if match:
            word, number = match.groups()
            if word is None or word in _KIND_WORDS[kind]:
                index = int(number) - 1
                if 0 <= index < len(entries):
                    return entries[index]
        return None

    def by_uuid(self, kind: Kind, raw: str) -> RefEntry | None:
        try:
            parsed = uuid.UUID(raw.strip())
        except ValueError:
            return None
        for entry in self.entries[kind]:
            if entry.id == parsed:
                return entry
        return None

    def by_uuid_prefix(self, kind: Kind, raw: str) -> list[RefEntry]:
        """Candidats dont l'UUID partage le plus long préfixe commun (≥ minimum)
        avec ``raw`` — liste vide si ``raw`` n'a pas l'allure d'un UUID."""
        query = raw.strip().casefold().replace("-", "")
        if len(query) < UUID_PREFIX_MIN_HEX or not _HEX_RE.match(query):
            return []
        best_len = 0
        best: list[RefEntry] = []
        for entry in self.entries[kind]:
            candidate = entry.id.hex
            common = 0
            for a, b in zip(query, candidate, strict=False):
                if a != b:
                    break
                common += 1
            if common > best_len:
                best_len, best = common, [entry]
            elif common == best_len and best_len:
                best.append(entry)
        return best if best_len >= UUID_PREFIX_MIN_HEX else []

    def by_title(self, kind: Kind, raw: str) -> list[RefEntry]:
        """Titre exact (normalisé), sinon titres approchants (``difflib``)."""
        query = _normalize(raw)
        if not query:
            return []
        entries = self.entries[kind]
        exact = [e for e in entries if _normalize(e.title) == query]
        if exact:
            return exact
        by_norm: dict[str, list[RefEntry]] = {}
        for entry in entries:
            by_norm.setdefault(_normalize(entry.title), []).append(entry)
        close = difflib.get_close_matches(query, list(by_norm), n=5, cutoff=TITLE_FUZZY_CUTOFF)
        return [entry for key in close for entry in by_norm[key]]

    # ---------------------------------------------------------- résolution

    def resolve(self, kind: Kind, raw: object, *, eligible=None) -> Resolution:
        """Chaîne tolérante (docstring du module). ``eligible`` (prédicat sur
        l'entité, optionnel) restreint les candidats listés dans les messages
        d'échec — la résolution elle-même reste globale, pour que l'appelant
        puisse expliquer *pourquoi* une entité trouvée est inéligible."""
        label, _ = _KIND_LABELS[kind]
        query = str(raw or "").strip()
        listing = self._listing(kind, eligible)
        if not query:
            return Resolution(error=f"{label} non précisé — indiquez sa référence. {listing}")

        found = self.by_ref(kind, query) or self.by_uuid(kind, query)
        if found is not None:
            return Resolution(entry=found)
        candidates = self.by_uuid_prefix(kind, query) or self.by_title(kind, query)
        if len(candidates) == 1:
            return Resolution(entry=candidates[0])
        if candidates:
            options = ", ".join(self._describe(e) for e in candidates)
            return Resolution(
                error=(
                    f"Référence ambiguë « {query} » — plusieurs correspondances : "
                    f"{options}. Précisez la référence."
                )
            )
        return Resolution(
            error=f"{label} introuvable dans ce cours pour « {query} ». {listing}"
        )

    def _listing(self, kind: Kind, eligible) -> str:
        _, plural = _KIND_LABELS[kind]
        entries = [e for e in self.entries[kind] if eligible is None or eligible(e.entity)]
        if not entries:
            return f"{plural} : aucun disponible."
        shown = "; ".join(self._describe(e) for e in entries[:CANDIDATES_LISTED])
        if len(entries) > CANDIDATES_LISTED:
            shown += f"; … ({len(entries) - CANDIDATES_LISTED} de plus)"
        return f"{plural} : {shown}."

    @staticmethod
    def _describe(entry: RefEntry) -> str:
        return f"{entry.ref} — {entry.title}"

    # ----------------------------------------------------------- citations

    def rewrite_citations(self, text: str) -> str:
        """``oc-block:<ref>`` / ``oc-resource:<ref>`` → forme UUID ; une cible
        déjà en UUID ou inconnue est laissée telle quelle (filtrée ensuite par
        ``extract_sources``)."""
        return _CITATION_RE.sub(self._rewrite_match, text)

    def _rewrite_match(self, match: re.Match[str]) -> str:
        scheme, target = match.group(1), match.group(2)
        kind: Kind = "block" if scheme == "block" else "resource"
        entry = self.by_ref(kind, target)
        if entry is None:
            candidates = self.by_uuid_prefix(kind, target)
            entry = candidates[0] if len(candidates) == 1 else None
        return f"oc-{scheme}:{entry.id}" if entry is not None else match.group(0)

    def rewrite_content_refs(self, text: str) -> str:
        """Liens de CONTENU d'un markdown proposé (``propose_block_edit``) :
        ``oc-resource:<cible>`` / ``oc-module:<cible>`` réécrits en UUID —
        le modèle insère par référence courte (``oc-resource:R2``), un lien
        existant recopié en UUID passe tel quel (``by_uuid_prefix`` le
        retrouve), une cible inconnue reste verbatim (note « indisponible »
        côté rendu, visible au diff)."""
        return _CONTENT_REF_RE.sub(self._rewrite_content_match, text)

    def _rewrite_content_match(self, match: re.Match[str]) -> str:
        scheme, target = match.group(1), match.group(2)
        kind: Kind = "resource" if scheme == "resource" else "module"
        entry = self.by_ref(kind, target)
        if entry is None:
            candidates = self.by_uuid_prefix(kind, target)
            entry = candidates[0] if len(candidates) == 1 else None
        return f"oc-{scheme}:{entry.id}" if entry is not None else match.group(0)


_CITATION_SCHEMES = ("oc-block:", "oc-resource:")
_CITATION_RE = re.compile(r"oc-(block|resource):([A-Za-z0-9-]+)")
# Liens de contenu (markdown d'un bloc) : ressources et modules intégrés.
_CONTENT_REF_RE = re.compile(r"oc-(resource|module):([A-Za-z0-9-]+)")
# Suffixes pouvant amorcer une citation encore incomplète : préfixe strict d'un
# schéma (``o``, ``oc-bl``…) ou schéma complet suivi d'une cible en cours.
_SCHEME_PREFIXES = sorted(
    {scheme[:i] for scheme in _CITATION_SCHEMES for i in range(1, len(scheme))},
    key=len,
    reverse=True,
)
_PENDING_RE = re.compile(r"oc-(?:block|resource):[A-Za-z0-9-]*$")
# Au-delà, aucune cible plausible : on libère (jamais de rétention pathologique).
_PENDING_MAX_CHARS = 64


class CitationRewriter:
    """Réécriture des citations **en flux** : :meth:`feed` rend le texte
    libérable (citations complètes réécrites), en retenant le suffixe qui
    pourrait amorcer une citation ; :meth:`flush` libère le reste."""

    def __init__(self, refs: CourseRefs) -> None:
        self._refs = refs
        self._buffer = ""

    def feed(self, delta: str) -> str:
        self._buffer += delta
        held = self._pending_length(self._buffer)
        ready, self._buffer = self._buffer[: len(self._buffer) - held], self._buffer[
            len(self._buffer) - held :
        ]
        return self._refs.rewrite_citations(ready)

    def flush(self) -> str:
        ready, self._buffer = self._buffer, ""
        return self._refs.rewrite_citations(ready)

    @staticmethod
    def _pending_length(text: str) -> int:
        pending = _PENDING_RE.search(text)
        if pending is not None:
            length = len(pending.group(0))
            return length if length <= _PENDING_MAX_CHARS else 0
        for prefix in _SCHEME_PREFIXES:
            if text.endswith(prefix):
                return len(prefix)
        return 0
