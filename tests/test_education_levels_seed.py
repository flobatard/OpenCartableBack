"""Intégrité du seed des niveaux d'étude — aucun accès DB, on valide la donnée pure."""

import uuid
from collections import Counter

from app.education_levels.seed_data import (
    SEED_NAMESPACE,
    SYSTEMES,
    education_level_id,
    iter_rows,
)
from app.models.education_level import CITE_MAX, PROFONDEUR_MAX
from app.subjects.seed_data import SEED_NAMESPACE as SUBJECTS_SEED_NAMESPACE

ROWS = list(iter_rows())
BY_ID = {r["id"]: r for r in ROWS}


def test_codes_et_ids_uniques():
    codes = [r["code"] for r in ROWS]
    assert len(set(codes)) == len(codes)
    assert len(BY_ID) == len(ROWS)


def test_unicite_systeme_parent_nom_racines_comprises():
    # Miroir de la contrainte UNIQUE NULLS NOT DISTINCT (systeme, parent_id, nom)
    doublons = [
        k
        for k, v in Counter((r["systeme"], r["parent_id"], r["nom"]) for r in ROWS).items()
        if v > 1
    ]
    assert doublons == []


def test_profondeurs_coherentes():
    for r in ROWS:
        assert 0 <= r["profondeur"] <= PROFONDEUR_MAX
        if r["parent_id"] is None:
            assert r["profondeur"] == 0
        else:
            assert r["profondeur"] == BY_ID[r["parent_id"]]["profondeur"] + 1


def test_parents_avant_enfants_et_pas_d_orphelin():
    # La migration de seed insère dans l'ordre d'itération : chaque parent
    # doit avoir été yieldé avant ses enfants.
    vus: set[uuid.UUID] = set()
    for r in ROWS:
        if r["parent_id"] is not None:
            assert r["parent_id"] in vus, f"orphelin ou parent tardif : {r['code']}"
        vus.add(r["id"])


def test_volumetrie_attendue():
    # Voie générale par système, hors préélémentaire. À faire évoluer avec
    # les appends (maternelle, voie pro, BTS/BUT/CPGE, autres systèmes...).
    attendu = {
        "fr": 22, "de": 20, "uk": 20, "es": 19, "it": 20, "be": 18,
        "ch": 20, "nl": 18, "pt": 18, "us": 19, "ca": 19, "ca-qc": 20,
    }
    assert Counter(r["systeme"] for r in ROWS) == attendu
    assert len(ROWS) == 233


def test_systemes_declares_et_codes_prefixes():
    assert {r["systeme"] for r in ROWS} == set(SYSTEMES)
    for r in ROWS:
        assert r["code"].startswith(f"{r['systeme']}.")
        if r["parent_id"] is not None:
            parent = BY_ID[r["parent_id"]]
            assert r["systeme"] == parent["systeme"]
            assert r["code"].startswith(f"{parent['code']}.")


def test_filtre_systemes():
    # Point d'entrée des futures data migrations d'append : ne yield que
    # les systèmes demandés, sans toucher aux IDs.
    partiel = list(iter_rows(systemes=["de", "us"]))
    assert {r["systeme"] for r in partiel} == {"de", "us"}
    assert [r["id"] for r in partiel] == [
        r["id"] for r in ROWS if r["systeme"] in ("de", "us")
    ]


def test_cite_valides():
    # Pivot international : toute classe porte un CITE ; seuls des cycles
    # multi-CITE (supérieur, secondaires à cheval 2/3...) restent à None.
    for r in ROWS:
        assert r["cite"] is None or 0 <= r["cite"] <= CITE_MAX
        if r["profondeur"] == PROFONDEUR_MAX:
            assert r["cite"] is not None, f"classe sans CITE : {r['code']}"
        if r["cite"] is None:
            assert r["profondeur"] == 0, f"classe multi-CITE impossible : {r['code']}"


def test_ages_coherents():
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


def test_namespace_fige():
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
