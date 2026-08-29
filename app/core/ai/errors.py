"""Traduction des erreurs provider → HTTPException, AU BORD du module confiné.

Sémantique (motif de :mod:`app.core.auth`) : **jamais de 500**, jamais de clé
API ni de prompt dans les logs ou les ``detail``.

- Config invalide fournie par l'appelant (validation locale) → **422** ;
- clé refusée par le provider (401/403 provider) → **400** — surtout PAS 401 :
  le 401 est réservé au JWT Zitadel (le front redéclencherait un login) et ne
  doit jamais porter de ``WWW-Authenticate`` ici ;
- requête refusée par le provider (modèle inconnu, params invalides) → **422** ;
- rate-limit/quota provider → **429** ;
- provider injoignable, timeout, 5xx provider, ou tout imprévu → **503**.

La détection du status provider se fait par duck-typing (``.status_code`` chez
openai/anthropic/ollama, ``.code`` chez google-genai, ``.response.status_code``
chez httpx/HfHub) : aucun import de SDK provider ici.
"""

import asyncio
import logging

import httpx
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

_DETAIL_KEY_REFUSED = "Clé API refusée par le fournisseur IA"
_DETAIL_BAD_REQUEST = "Modèle ou paramètres refusés par le fournisseur IA"
_DETAIL_RATE_LIMITED = "Limite de débit ou quota du fournisseur IA atteint"
_DETAIL_UNAVAILABLE = "Fournisseur IA injoignable"


def invalid_config(detail: str) -> HTTPException:
    """422 de validation locale (avant tout réseau) — détail explicite."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _provider_status(exc: Exception) -> int | None:
    """Extrait le status HTTP d'une exception provider, tous SDK confondus."""
    for attr in ("status_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def translate_provider_error(exc: Exception, provider: str) -> HTTPException:
    """Traduit une exception survenue pendant un appel provider.

    Ne relaie jamais le message brut du SDK (il peut contenir des fragments de
    requête) : seuls le type d'exception et le status sont loggés.
    """
    provider_status = _provider_status(exc)
    logger.warning(
        "Erreur provider IA (%s) : %s, status=%s",
        provider,
        type(exc).__name__,
        provider_status,
    )

    if provider_status in (401, 403):
        return HTTPException(status.HTTP_400_BAD_REQUEST, detail=_DETAIL_KEY_REFUSED)
    if provider_status == 429:
        return HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail=_DETAIL_RATE_LIMITED)
    if provider_status is not None and 400 <= provider_status < 500:
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_DETAIL_BAD_REQUEST)

    # 5xx provider, transport (httpx), timeouts, et filet générique : le
    # fournisseur est indisponible pour nous — même sémantique que l'IdP
    # injoignable de app/core/auth.py.
    if provider_status is None and not isinstance(
        exc, httpx.HTTPError | ConnectionError | asyncio.TimeoutError | TimeoutError
    ):
        # Exception sans status ni nature réseau identifiable : on la garde en
        # 503 (jamais 500) mais on la logge en erreur pour investigation.
        logger.error("Erreur provider IA inattendue (%s) : %s", provider, type(exc).__name__)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DETAIL_UNAVAILABLE)
