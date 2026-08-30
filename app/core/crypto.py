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
SEL_LEN = 16
_NONCE_LEN = 12
_CLE_LEN = 32
_INFO = b"opencartable/ai-credentials/v1"


class CleMaitreAbsente(Exception):
    """AI_CREDENTIALS_MASTER_KEY absente ou invalide (feature indisponible)."""


class ErreurDechiffrement(Exception):
    """Blob illisible : version inconnue, tronqué, ou clé/sel ne correspondent pas."""


def nouveau_sel() -> bytes:
    return os.urandom(SEL_LEN)


def decoder_cle_maitre(valeur: str) -> bytes:
    """Décode la clé maître du .env (32 octets base64 urlsafe)."""
    if not valeur:
        raise CleMaitreAbsente("AI_CREDENTIALS_MASTER_KEY non configurée")
    try:
        cle = base64.urlsafe_b64decode(valeur.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise CleMaitreAbsente(
            "AI_CREDENTIALS_MASTER_KEY illisible (base64 urlsafe attendu)"
        ) from exc
    if len(cle) != _CLE_LEN:
        raise CleMaitreAbsente("AI_CREDENTIALS_MASTER_KEY doit faire 32 octets décodés")
    return cle


def _deriver(cle_maitre: bytes, sel: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=_CLE_LEN, salt=sel, info=_INFO
    ).derive(cle_maitre)


def chiffrer_secret(clair: str, cle_maitre: bytes, sel: bytes) -> bytes:
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(_deriver(cle_maitre, sel)).encrypt(nonce, clair.encode("utf-8"), b"")
    return bytes([FORMAT_V1]) + nonce + ciphertext


def dechiffrer_secret(blob: bytes, cle_maitre: bytes, sel: bytes) -> str:
    if len(blob) < 1 + _NONCE_LEN + 16 or blob[0] != FORMAT_V1:
        raise ErreurDechiffrement("Format de blob inconnu ou tronqué")
    nonce, ciphertext = blob[1 : 1 + _NONCE_LEN], blob[1 + _NONCE_LEN :]
    try:
        clair = AESGCM(_deriver(cle_maitre, sel)).decrypt(nonce, ciphertext, b"")
    except InvalidTag as exc:
        raise ErreurDechiffrement("Clé maître ou sel ne correspondent pas au blob") from exc
    return clair.decode("utf-8")
