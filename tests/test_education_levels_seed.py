"""Intégrité du seed des niveaux d'étude — aucun accès DB, on valide la donnée pure.

NB : la surface de données du module seed reste volontairement en FRANÇAIS
(clés ``nom``/``profondeur``/``systeme``, constante ``SYSTEMES``, kwarg
``iter_rows(systemes=...)`` — contrat APPEND-ONLY : les migrations de seed
immuables rejouent ces dicts contre les noms de colonnes d'origine).
"""

import uuid
from collections import Counter

from app.education_levels.seed_data import (
    SEED_NAMESPACE,
    SYSTEMES,
    education_level_id,
    iter_rows,
)
from app.models.education_level import CITE_MAX, MAX_DEPTH
from app.subjects.seed_data import SEED_NAMESPACE as SUBJECTS_SEED_NAMESPACE

ROWS = list(iter_rows())
BY_ID = {r["id"]: r for r in ROWS}


def test_codes_and_ids_unique():
    codes = [r["code"] for r in ROWS]
    assert len(set(codes)) == len(codes)
    assert len(BY_ID) == len(ROWS)


def test_system_parent_name_unique_including_roots():
    # Miroir de la contrainte UNIQUE NULLS NOT DISTINCT (system, parent_id, name)
    duplicates = [
        k
        for k, v in Counter((r["systeme"], r["parent_id"], r["nom"]) for r in ROWS).items()
        if v > 1
    ]
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
    # Voie générale par système, hors préélémentaire. À faire évoluer avec
    # les appends (maternelle, voie pro, BTS/BUT/CPGE, autres systèmes...).
    expected = {
        "fr": 22, "de": 20, "uk": 20, "es": 19, "it": 20, "be": 18,
        "ch": 20, "nl": 18, "pt": 18, "us": 19, "ca": 19, "ca-qc": 20,
    }
    assert Counter(r["systeme"] for r in ROWS) == expected
    assert len(ROWS) == 233


def test_systems_declared_and_codes_prefixed():
    assert {r["systeme"] for r in ROWS} == set(SYSTEMES)
    for r in ROWS:
        assert r["code"].startswith(f"{r['systeme']}.")
        if r["parent_id"] is not None:
            parent = BY_ID[r["parent_id"]]
            assert r["systeme"] == parent["systeme"]
            assert r["code"].startswith(f"{parent['code']}.")


def test_systems_filter():
    # Point d'entrée des futures data migrations d'append : ne yield que
    # les systèmes demandés, sans toucher aux IDs.
    partial = list(iter_rows(systemes=["de", "us"]))
    assert {r["systeme"] for r in partial} == {"de", "us"}
    assert [r["id"] for r in partial] == [
        r["id"] for r in ROWS if r["systeme"] in ("de", "us")
    ]


def test_cite_values_valid():
    # Pivot international : toute classe porte un CITE ; seuls des cycles
    # multi-CITE (supérieur, secondaires à cheval 2/3...) restent à None.
    for r in ROWS:
        assert r["cite"] is None or 0 <= r["cite"] <= CITE_MAX
        if r["profondeur"] == MAX_DEPTH:
            assert r["cite"] is not None, f"classe sans CITE : {r['code']}"
        if r["cite"] is None:
            assert r["profondeur"] == 0, f"classe multi-CITE impossible : {r['code']}"


def test_ages_consistent():
    for r in ROWS:
        if r["age_min"] is not None and r["age_max"] is not None:
            assert r["age_min"] <= r["age_max"], r["code"]
        if r["parent_id"] is None:
            continue
        parent = BY_ID[r["parent_id"]]
        # Plage de l'enfant incluse dans celle du parent (bornes NULL = ouvertes).
        if r["age_min"] is not None and parent["age_min"] is not None:
            assert r["age_min"] >= parent["age_min"], r["code"]
        if r["age_max"] is not None and parent["age_max"] is not None:
            assert r["age_max"] <= parent["age_max"], r["code"]
        if r["age_max"] is None:
            assert parent["age_max"] is None, r["code"]


def test_namespace_frozen():
    # Garde-fou : si SEED_NAMESPACE ou un slug change, les IDs seedés changent
    # et la migration de seed n'est plus idempotente.
    assert education_level_id("fr.college") == uuid.UUID("256c64c8-4f62-525b-90cc-794975df5bb9")
    assert education_level_id("fr.superieur.doctorat") == uuid.UUID(
        "cb2b9537-d8fa-5317-b8f1-0568339e7269"
    )
    assert education_level_id("us.high.grade-12") == uuid.UUID(
        "04b18ead-47c6-53cc-a7b7-9823bfa9e9d1"
    )
    assert education_level_id("ca-qc.cegep") == uuid.UUID(
        "6ba8bd71-5d45-524e-9783-a431b718b9b4"
    )
    # Les deux classifications seedées ne partagent JAMAIS leur namespace.
    assert SEED_NAMESPACE != SUBJECTS_SEED_NAMESPACE
