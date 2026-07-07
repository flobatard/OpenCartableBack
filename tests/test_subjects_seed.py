"""Intégrité de la taxonomie seed — aucun accès DB, on valide la donnée pure."""

import uuid
from collections import Counter

from app.models.subject import PROFONDEUR_MAX
from app.subjects.seed_data import iter_rows, subject_id

ROWS = list(iter_rows())
BY_ID = {r["id"]: r for r in ROWS}


def test_codes_et_ids_uniques():
    codes = [r["code"] for r in ROWS]
    assert len(set(codes)) == len(codes)
    assert len(BY_ID) == len(ROWS)


def test_unicite_parent_nom_racines_comprises():
    # Miroir de la contrainte UNIQUE NULLS NOT DISTINCT (parent_id, nom)
    doublons = [k for k, v in Counter((r["parent_id"], r["nom"]) for r in ROWS).items() if v > 1]
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
    racines = [r for r in ROWS if r["parent_id"] is None]
    assert len(racines) == 12
    assert 300 <= len(ROWS) <= 550


def test_disciplines_scientifiques_detaillees():
    for discipline in ("mathematiques", "physique"):
        assert any(
            r["profondeur"] == 3 and r["code"].startswith(f"{discipline}.") for r in ROWS
        )


def test_namespace_fige():
    # Garde-fou : si SEED_NAMESPACE ou le slug change, les IDs seedés changent
    # et la migration de seed n'est plus idempotente.
    assert subject_id("mathematiques") == uuid.UUID("071e6b37-6bf7-52aa-a5dd-4542511067c0")
