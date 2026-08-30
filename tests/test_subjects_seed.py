"""Intégrité de la taxonomie seed — aucun accès DB, on valide la donnée pure.

NB : la surface de données du module seed reste volontairement en FRANÇAIS
(clés ``nom``/``profondeur``, contrat APPEND-ONLY : les migrations de seed
immuables rejouent ces dicts contre les noms de colonnes d'origine).
"""

import uuid
from collections import Counter

from app.models.subject import MAX_DEPTH
from app.subjects.seed_data import iter_rows, subject_id

ROWS = list(iter_rows())
BY_ID = {r["id"]: r for r in ROWS}


def test_codes_and_ids_unique():
    codes = [r["code"] for r in ROWS]
    assert len(set(codes)) == len(codes)
    assert len(BY_ID) == len(ROWS)


def test_parent_name_unique_including_roots():
    # Miroir de la contrainte UNIQUE NULLS NOT DISTINCT (parent_id, name)
    duplicates = [k for k, v in Counter((r["parent_id"], r["nom"]) for r in ROWS).items() if v > 1]
    assert duplicates == []


def test_depths_consistent():
    for r in ROWS:
        assert 0 <= r["profondeur"] <= MAX_DEPTH
        if r["parent_id"] is None:
            assert r["profondeur"] == 0
        else:
            assert r["profondeur"] == BY_ID[r["parent_id"]]["profondeur"] + 1


def test_parents_before_children_and_no_orphan():
    # La migration de seed insère dans l'ordre d'itération : chaque parent
    # doit avoir été yieldé avant ses enfants.
    seen: set[uuid.UUID] = set()
    for r in ROWS:
        if r["parent_id"] is not None:
            assert r["parent_id"] in seen, f"orphelin ou parent tardif : {r['code']}"
        seen.add(r["id"])


def test_expected_volume():
    roots = [r for r in ROWS if r["parent_id"] is None]
    assert len(roots) == 12
    assert 300 <= len(ROWS) <= 550


def test_scientific_disciplines_detailed():
    for discipline in ("mathematiques", "physique"):
        assert any(
            r["profondeur"] == 3 and r["code"].startswith(f"{discipline}.") for r in ROWS
        )


def test_namespace_frozen():
    # Garde-fou : si SEED_NAMESPACE ou le slug change, les IDs seedés changent
    # et la migration de seed n'est plus idempotente.
    assert subject_id("mathematiques") == uuid.UUID("071e6b37-6bf7-52aa-a5dd-4542511067c0")
