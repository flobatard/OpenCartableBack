"""Tests des références courtes de l'assistant (``app/course_assistant/refs``) :
construction, résolveur tolérant, réécriture des citations en flux — purs."""

import uuid
from types import SimpleNamespace

import pytest

from app.course_assistant.refs import (
    CANDIDATES_LISTED,
    QUESTION_TITLE_CHARS,
    CitationRewriter,
    CourseRefs,
)

B1, B2, B3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
R1, R2 = uuid.uuid4(), uuid.uuid4()
M1 = uuid.uuid4()
Q1, Q2, Q3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _block(id, title, type="text"):
    return SimpleNamespace(id=id, title=title, type=type)


def _refs() -> CourseRefs:
    return CourseRefs.build(
        [
            _block(B1, "Introduction"),
            _block(B2, None, type="exercise"),
            _block(B3, "Théorème de Pythagore"),
        ],
        [
            SimpleNamespace(id=R1, original_name="cours.pdf"),
            SimpleNamespace(id=R2, original_name="figure.png"),
        ],
        [SimpleNamespace(id=M1, title="Balance interactive")],
        block_titles={B2: "Exercice"},
    )


# ------------------------------------------------------------ construction


def test_build_numbers_in_order_and_titles() -> None:
    refs = _refs()
    assert refs.refs("block") == ["B1", "B2", "B3"]
    assert refs.refs("resource") == ["R1", "R2"]
    assert refs.refs("module") == ["M1"]
    assert refs.ref_of("block", B3) == "B3"
    assert refs.ref_of("block", uuid.uuid4()) is None
    assert refs.by_ref("block", "B2").title == "Exercice"  # libellé de type fourni
    assert refs.ids("resource") == {R1, R2}


# -------------------------------------------------------------- résolution


@pytest.mark.parametrize("raw", ["B3", "b3", " B3 ", "3", "#3", "bloc 3", "Bloc 3", "block 3"])
def test_resolve_by_reference_variants(raw) -> None:
    assert _refs().resolve("block", raw).entry.id == B3


def test_resolve_reference_of_wrong_kind_falls_through() -> None:
    # « R1 » n'est pas une référence de bloc : ni référence, ni titre proche.
    resolution = _refs().resolve("block", "R1")
    assert resolution.entry is None
    assert "introuvable" in resolution.error


def test_resolve_exact_uuid() -> None:
    assert _refs().resolve("resource", str(R2)).entry.id == R2
    assert _refs().resolve("resource", str(R2).upper()).entry.id == R2


def test_resolve_deformed_uuid_by_common_prefix() -> None:
    # Cas réel : début correct, divergence à mi-chemin (pas un préfixe strict).
    real = str(B3)
    deformed = real[:22] + "f-9e37194af"
    assert _refs().resolve("block", deformed).entry.id == B3


def test_resolve_uuid_too_short_or_unknown_lists_candidates() -> None:
    refs = _refs()
    unknown = refs.resolve("block", str(uuid.uuid4()))
    assert unknown.entry is None
    assert "Blocs du cours : B1 — Introduction; B2 — Exercice; B3 — Théorème de Pythagore." in (
        unknown.error
    )
    short = refs.resolve("block", str(B3)[:7])  # < 8 hex : pas un UUID plausible
    assert short.entry is None


def test_resolve_by_title_exact_ignoring_case_and_accents() -> None:
    assert _refs().resolve("block", "theoreme de pythagore").entry.id == B3
    assert _refs().resolve("module", "BALANCE INTERACTIVE").entry.id == M1
    assert _refs().resolve("resource", "figure.png").entry.id == R2


def test_resolve_by_close_title() -> None:
    assert _refs().resolve("block", "Théorème de Pytagore").entry.id == B3


def test_resolve_ambiguous_title_lists_matches() -> None:
    refs = CourseRefs.build(
        [_block(B1, "Exercice"), _block(B2, "Exercice")], [], []
    )
    resolution = refs.resolve("block", "exercice")
    assert resolution.entry is None
    assert "ambiguë" in resolution.error
    assert "B1 — Exercice" in resolution.error and "B2 — Exercice" in resolution.error


def test_resolve_missing_argument() -> None:
    for raw in (None, "", "   "):
        resolution = _refs().resolve("block", raw)
        assert resolution.entry is None
        assert "non précisé" in resolution.error
        assert "B1 — Introduction" in resolution.error


def test_resolve_error_listing_respects_eligibility_and_cap() -> None:
    refs = _refs()
    only_pdf = refs.resolve(
        "resource", "inconnu", eligible=lambda r: r.original_name.endswith(".pdf")
    )
    assert "R1 — cours.pdf" in only_pdf.error
    assert "figure.png" not in only_pdf.error
    none = refs.resolve("resource", "inconnu", eligible=lambda r: False)
    assert "aucun disponible" in none.error

    many = CourseRefs.build(
        [_block(uuid.uuid4(), f"Bloc numéro {i}") for i in range(CANDIDATES_LISTED + 5)], [], []
    )
    error = many.resolve("block", "zzz").error
    assert f"B{CANDIDATES_LISTED} —" in error
    assert f"B{CANDIDATES_LISTED + 1} —" not in error
    assert "(5 de plus)" in error


# --------------------------------------------------------------- citations


def test_rewrite_citations_refs_uuids_and_unknown() -> None:
    refs = _refs()
    other = uuid.uuid4()
    text = (
        f"[Intro](oc-block:B1), [Pyth](oc-block:b3), [PDF](oc-resource:R1), "
        f"[déjà](oc-block:{B2}), [faux](oc-block:{other}), [num](oc-resource:2), "
        "[inconnu](oc-block:B9)"
    )
    assert refs.rewrite_citations(text) == (
        f"[Intro](oc-block:{B1}), [Pyth](oc-block:{B3}), [PDF](oc-resource:{R1}), "
        f"[déjà](oc-block:{B2}), [faux](oc-block:{other}), [num](oc-resource:{R2}), "
        "[inconnu](oc-block:B9)"
    )


def test_rewrite_citations_repairs_deformed_uuid() -> None:
    refs = _refs()
    deformed = str(R1)[:20] + "0000-000000000000"
    assert refs.rewrite_citations(f"(oc-resource:{deformed})") == f"(oc-resource:{R1})"


def _stream(refs: CourseRefs, chunks: list[str]) -> tuple[list[str], str]:
    rewriter = CitationRewriter(refs)
    out = [rewriter.feed(c) for c in chunks]
    out.append(rewriter.flush())
    return out, "".join(out)


def test_rewriter_streams_split_citation() -> None:
    refs = _refs()
    chunks = ["Voir [Intro](o", "c-blo", "ck:B", "1) et [PDF](oc-resource:R1", ") fin"]
    parts, joined = _stream(refs, chunks)
    assert joined == f"Voir [Intro](oc-block:{B1}) et [PDF](oc-resource:{R1}) fin"
    # Le texte sans rapport part immédiatement, le préfixe ambigu est retenu.
    assert parts[0] == "Voir [Intro]("
    assert parts[1] == ""
    assert parts[2] == ""
    assert parts[3] == f"oc-block:{B1}) et [PDF]("


def test_rewriter_flush_completes_pending_citation() -> None:
    refs = _refs()
    parts, joined = _stream(refs, ["Voir oc-block:B3"])
    assert parts[0] == "Voir "
    assert joined == f"Voir oc-block:{B3}"


def test_rewriter_releases_false_prefix() -> None:
    refs = _refs()
    _, joined = _stream(refs, ["un o", "c-bl", "eu ciel"])
    assert joined == "un oc-bleu ciel"


def test_rewriter_never_holds_pathological_run() -> None:
    refs = _refs()
    rewriter = CitationRewriter(refs)
    assert rewriter.feed("oc-block:" + "a" * 100) == "oc-block:" + "a" * 100
    assert rewriter.flush() == ""


# ------------------------------------------- liens de contenu (propositions)


def test_rewrite_content_refs_short_refs_to_uuid() -> None:
    refs = _refs()
    text = "Voir [le PDF](oc-resource:R1) et la [balance](oc-module:M1)."
    assert refs.rewrite_content_refs(text) == (
        f"Voir [le PDF](oc-resource:{R1}) et la [balance](oc-module:{M1})."
    )


def test_rewrite_content_refs_keeps_uuid_and_unknown_verbatim() -> None:
    refs = _refs()
    unknown = uuid.uuid4()
    text = f"![fig](oc-resource:{R2}) puis oc-resource:R9 et oc-module:{unknown}"
    # UUID existant conservé, référence hors bornes et UUID inconnu laissés
    # verbatim (note « indisponible » au rendu, visible au diff).
    assert refs.rewrite_content_refs(text) == (
        f"![fig](oc-resource:{R2}) puis oc-resource:R9 et oc-module:{unknown}"
    )


def test_rewrite_content_refs_ignores_block_citations() -> None:
    # oc-block: n'est pas un lien de CONTENU (réécrit ailleurs, en flux).
    refs = _refs()
    assert refs.rewrite_content_refs("Voir oc-block:B1") == "Voir oc-block:B1"


# ------------------------------------------ questions du bloc exercice édité


def _question(id, statement="Énoncé"):
    return {"id": str(id), "statement": statement, "type": "free_text", "expected_answer": ""}


def _question_refs(questions=None, question_refs=None) -> CourseRefs:
    default = [
        _question(Q1, "Calculer 2+2."),
        _question(Q2, "Développer $(a+b)^2$."),
        _question(Q3, "Conclure."),
    ]
    return CourseRefs.build(
        [],
        [],
        [],
        questions=default if questions is None else questions,
        question_refs=question_refs,
    )


def test_build_question_refs_in_order_and_skips_bad_ids() -> None:
    refs = CourseRefs.build(
        [],
        [],
        [],
        questions=[
            _question(Q1, "Calculer 2+2."),
            {"id": "pas-un-uuid", "statement": "ignorée"},
            "n'importe quoi",
            {"statement": "sans id"},
            _question(Q3, ""),
        ],
    )
    # Numérotation continue sur les seules questions à id valide.
    assert refs.refs("question") == ["Q1", "Q2"]
    assert refs.ref_of("question", Q3) == "Q2"
    assert refs.by_ref("question", "Q1").title == "Calculer 2+2."
    assert refs.by_ref("question", "Q2").title == "Question 2"  # énoncé vide : repli
    assert refs.by_ref("question", "Q2").entity["id"] == str(Q3)
    # Aucune question : genre vide, jamais absent (``refs("question")`` sûr).
    assert CourseRefs.build([], [], []).refs("question") == []


def test_build_question_title_is_a_folded_excerpt() -> None:
    refs = CourseRefs.build(
        [],
        [],
        [],
        questions=[_question(Q1, "  ligne 1\n\nligne   2  "), _question(Q2, "mot " * 100)],
    )
    assert refs.by_ref("question", "Q1").title == "ligne 1 ligne 2"
    long_title = refs.by_ref("question", "Q2").title
    assert long_title.endswith("…")
    assert len(long_title) <= QUESTION_TITLE_CHARS + 1


def test_build_question_refs_replays_mapping_never_reuses_a_freed_ref() -> None:
    """Reprise HITL : Q2 supprimée entre-temps, une question ajoutée — Q1 et Q3
    gardent leur référence, Q2 n'est jamais réattribuée, la nouvelle est Q4."""
    q4 = uuid.uuid4()
    mapping = {"Q1": str(Q1), "Q2": str(Q2), "Q3": str(Q3)}
    refs = CourseRefs.build(
        [],
        [],
        [],
        # Ordre du bloc volontairement différent du mapping : le mapping prime.
        questions=[_question(Q3, "c"), _question(q4, "nouvelle"), _question(Q1, "a")],
        question_refs=mapping,
    )
    assert refs.refs("question") == ["Q1", "Q3", "Q4"]
    assert refs.by_ref("question", "Q3").id == Q3  # par libellé, jamais par position
    assert refs.by_ref("question", "3").id == Q3
    assert refs.by_ref("question", "Q4").id == q4
    assert refs.by_ref("question", "Q2") is None
    missing = refs.resolve("question", "Q2")
    assert missing.entry is None
    assert "Questions de l'exercice : Q1 — a; Q3 — c; Q4 — nouvelle." in missing.error


def test_build_question_refs_mapping_tolerates_garbage() -> None:
    mapping = {"Q1": str(Q1), "bidule": str(Q2), "Q7": "pas-un-uuid"}
    refs = _question_refs(questions=[_question(Q1, "a"), _question(Q2, "b")], question_refs=mapping)
    # Q1 conservée ; l'entrée « bidule » est ignorée (Q2 redevient nouvelle) ;
    # Q7 est illisible mais son numéro reste réservé → la nouvelle est Q8.
    assert refs.refs("question") == ["Q1", "Q8"]
    assert refs.by_ref("question", "Q8").id == Q2


@pytest.mark.parametrize(
    "raw",
    ["Q2", "q2", " Q2 ", "2", "#2", "question 2", "Question 2", "Développer $(a+b)^2$."],
)
def test_resolve_question_variants(raw) -> None:
    assert _question_refs().resolve("question", raw).entry.id == Q2


def test_resolve_question_by_uuid_prefix_and_close_statement() -> None:
    refs = _question_refs()
    assert refs.resolve("question", str(Q3)).entry.id == Q3
    assert refs.resolve("question", str(Q3)[:22] + "f-9e37194af").entry.id == Q3
    assert refs.resolve("question", "Calculer 2+3.").entry.id == Q1  # titre approchant


def test_resolve_question_lists_candidates() -> None:
    resolution = _question_refs().resolve("question", "Q9")
    assert resolution.entry is None
    assert "Question introuvable" in resolution.error
    assert "Q1 — Calculer 2+2.; Q2 — Développer $(a+b)^2$.; Q3 — Conclure." in resolution.error
    empty = CourseRefs.build([], [], []).resolve("question", "Q1")
    assert "Questions de l'exercice : aucun disponible." in empty.error
