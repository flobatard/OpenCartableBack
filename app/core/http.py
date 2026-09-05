"""Constructeurs d'``HTTPException`` partagés par les services.

Les services lèvent eux-mêmes les erreurs HTTP (les routeurs ne traduisent
rien) ; le texte de ``detail`` fait partie du contrat des tests d'API.
"""

from fastapi import HTTPException, status


def not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def invalid(detail: str) -> HTTPException:
    """422 : requête bien formée mais impossible à appliquer."""
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def unavailable(detail: str) -> HTTPException:
    """503 : dépendance externe (S3, clé maître…) absente ou injoignable."""
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)
