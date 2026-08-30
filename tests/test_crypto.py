"""Tests du module de chiffrement des secrets (app/core/crypto.py) — pur, sans I/O."""

import base64
import os

import pytest

from app.core import crypto

MASTER_KEY = os.urandom(32)
MASTER_KEY_B64 = base64.urlsafe_b64encode(MASTER_KEY).decode()


def test_round_trip():
    salt = crypto.new_salt()
    blob = crypto.encrypt_secret("sk-très-secrète-é✓", MASTER_KEY, salt)
    assert blob[0] == crypto.FORMAT_V1
    assert crypto.decrypt_secret(blob, MASTER_KEY, salt) == "sk-très-secrète-é✓"


def test_two_encryptions_differ():
    """Nonce aléatoire : le même clair ne produit jamais deux fois le même blob."""
    salt = crypto.new_salt()
    assert crypto.encrypt_secret("x", MASTER_KEY, salt) != crypto.encrypt_secret(
        "x", MASTER_KEY, salt
    )


def test_plaintext_absent_from_blob():
    salt = crypto.new_salt()
    blob = crypto.encrypt_secret("sk-ma-cle-api", MASTER_KEY, salt)
    assert b"sk-ma-cle-api" not in blob


def test_wrong_salt_or_wrong_key():
    salt = crypto.new_salt()
    blob = crypto.encrypt_secret("x", MASTER_KEY, salt)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_secret(blob, MASTER_KEY, crypto.new_salt())
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_secret(blob, os.urandom(32), salt)


def test_unknown_version_or_truncated_blob():
    salt = crypto.new_salt()
    blob = crypto.encrypt_secret("x", MASTER_KEY, salt)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_secret(bytes([0x02]) + blob[1:], MASTER_KEY, salt)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_secret(blob[:10], MASTER_KEY, salt)


def test_decode_master_key():
    assert crypto.decode_master_key(MASTER_KEY_B64) == MASTER_KEY
    with pytest.raises(crypto.MasterKeyMissing):
        crypto.decode_master_key("")
    with pytest.raises(crypto.MasterKeyMissing):
        crypto.decode_master_key("pas-du-base64-!!")
    # Base64 valide mais pas 32 octets décodés.
    with pytest.raises(crypto.MasterKeyMissing):
        crypto.decode_master_key(base64.urlsafe_b64encode(b"court").decode())
