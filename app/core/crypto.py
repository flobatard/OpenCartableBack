"""Chiffrement des secrets applicatifs (clés API IA des utilisateurs).

Seul module autorisé à importer ``cryptography`` (même exigence de
confinement que boto3/IdP/langchain). Fonctions pures : la lecture des
settings (``AI_CREDENTIALS_MASTER_KEY``) reste chez l'appelant.

Schéma v1 : AES-256-GCM, clé dérivée par HKDF-SHA256(clé maître serveur,
sel par utilisateur, info constante), nonce aléatoire par chiffrement.
Blob stocké : ``version(1 octet) || nonce(12) || ciphertext+tag``.
L'octet de version permet une rotation d'algorithme/format sans migration
destructive (v2 = nouveau format, l'ancien reste déchiffrable).

Portée de sécurité (assumée) : un dump DB seul est inexploitable (pas de
clé maître) et une fuite du seul .env aussi (pas de ciphertexts/sels) ; le
sel par utilisateur cloisonne la dérivation (aucune clé AES commune à la
table). En revanche un serveur compromis (RAM + .env + DB) peut déchiffrer
— inhérent au besoin : l'API doit lire la clé pour appeler le provider.
HKDF (deux HMAC) est quasi gratuit — pas de KDF lourd par requête sur Pi ;
la clé maître porte déjà 256 bits d'entropie, rien à étirer.
"""

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT_V1 = 0x01
SALT_LEN = 16
_NONCE_LEN = 12
_KEY_LEN = 32
_INFO = b"opencartable/ai-credentials/v1"


class MasterKeyMissing(Exception):
    """AI_CREDENTIALS_MASTER_KEY absente ou invalide (feature indisponible)."""


class DecryptionError(Exception):
    """Blob illisible : version inconnue, tronqué, ou clé/sel ne correspondent pas."""


def new_salt() -> bytes:
    return os.urandom(SALT_LEN)


def decode_master_key(value: str) -> bytes:
    """Décode la clé maître du .env (32 octets base64 urlsafe)."""
    if not value:
        raise MasterKeyMissing("AI_CREDENTIALS_MASTER_KEY non configurée")
    try:
        key = base64.urlsafe_b64decode(value.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise MasterKeyMissing(
            "AI_CREDENTIALS_MASTER_KEY illisible (base64 urlsafe attendu)"
        ) from exc
    if len(key) != _KEY_LEN:
        raise MasterKeyMissing("AI_CREDENTIALS_MASTER_KEY doit faire 32 octets décodés")
    return key


def _derive(master_key: bytes, salt: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=_KEY_LEN, salt=salt, info=_INFO
    ).derive(master_key)


def encrypt_secret(plaintext: str, master_key: bytes, salt: bytes) -> bytes:
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(_derive(master_key, salt)).encrypt(nonce, plaintext.encode("utf-8"), b"")
    return bytes([FORMAT_V1]) + nonce + ciphertext


def decrypt_secret(blob: bytes, master_key: bytes, salt: bytes) -> str:
    if len(blob) < 1 + _NONCE_LEN + 16 or blob[0] != FORMAT_V1:
        raise DecryptionError("Format de blob inconnu ou tronqué")
    nonce, ciphertext = blob[1 : 1 + _NONCE_LEN], blob[1 + _NONCE_LEN :]
    try:
        plaintext = AESGCM(_derive(master_key, salt)).decrypt(nonce, ciphertext, b"")
    except InvalidTag as exc:
        raise DecryptionError("Clé maître ou sel ne correspondent pas au blob") from exc
    return plaintext.decode("utf-8")
