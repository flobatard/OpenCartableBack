"""Tests du module de chiffrement des secrets (app/core/crypto.py) — pur, sans I/O."""

import base64
import os

import pytest

from app.core import crypto

CLE_MAITRE = os.urandom(32)
CLE_MAITRE_B64 = base64.urlsafe_b64encode(CLE_MAITRE).decode()


def test_aller_retour():
    sel = crypto.nouveau_sel()
    blob = crypto.chiffrer_secret("sk-très-secrète-é✓", CLE_MAITRE, sel)
    assert blob[0] == crypto.FORMAT_V1
    assert crypto.dechiffrer_secret(blob, CLE_MAITRE, sel) == "sk-très-secrète-é✓"


def test_deux_chiffrements_different():
    """Nonce aléatoire : le même clair ne produit jamais deux fois le même blob."""
    sel = crypto.nouveau_sel()
    assert crypto.chiffrer_secret("x", CLE_MAITRE, sel) != crypto.chiffrer_secret(
        "x", CLE_MAITRE, sel
    )


def test_le_clair_n_apparait_pas_dans_le_blob():
    sel = crypto.nouveau_sel()
    blob = crypto.chiffrer_secret("sk-ma-cle-api", CLE_MAITRE, sel)
    assert b"sk-ma-cle-api" not in blob


def test_mauvais_sel_ou_mauvaise_cle():
    sel = crypto.nouveau_sel()
    blob = crypto.chiffrer_secret("x", CLE_MAITRE, sel)
    with pytest.raises(crypto.ErreurDechiffrement):
        crypto.dechiffrer_secret(blob, CLE_MAITRE, crypto.nouveau_sel())
    with pytest.raises(crypto.ErreurDechiffrement):
        crypto.dechiffrer_secret(blob, os.urandom(32), sel)


def test_version_inconnue_ou_blob_tronque():
    sel = crypto.nouveau_sel()
    blob = crypto.chiffrer_secret("x", CLE_MAITRE, sel)
    with pytest.raises(crypto.ErreurDechiffrement):
        crypto.dechiffrer_secret(bytes([0x02]) + blob[1:], CLE_MAITRE, sel)
    with pytest.raises(crypto.ErreurDechiffrement):
        crypto.dechiffrer_secret(blob[:10], CLE_MAITRE, sel)


def test_decoder_cle_maitre():
    assert crypto.decoder_cle_maitre(CLE_MAITRE_B64) == CLE_MAITRE
    with pytest.raises(crypto.CleMaitreAbsente):
        crypto.decoder_cle_maitre("")
    with pytest.raises(crypto.CleMaitreAbsente):
        crypto.decoder_cle_maitre("pas-du-base64-!!")
    # Base64 valide mais pas 32 octets décodés.
    with pytest.raises(crypto.CleMaitreAbsente):
        crypto.decoder_cle_maitre(base64.urlsafe_b64encode(b"court").decode())
