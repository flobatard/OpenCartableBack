"""Accès au stockage objet S3 — bucket privé, URL présignées.

**Seul module autorisé à importer boto3** (même exigence de remplaçabilité que
l'IdP dans :mod:`app.core.auth` : changer de backend S3 ne doit toucher qu'ici).

Le bucket n'est jamais public : tout accès passe par une URL présignée mintée
par l'API — ``PUT`` pour l'upload direct navigateur→S3 (on ne fait pas transiter
les binaires par le backend, contrainte Pi), ``GET`` à TTL court pour la lecture.

``generate_presigned_url`` est du **calcul local** (signature, aucune I/O
réseau) : l'appeler de façon synchrone dans un handler async ne bloque pas
l'event loop. En revanche ``head_object``/``delete_objects`` parlent au réseau :
ils sont déportés dans un thread (:func:`run_in_threadpool`) pour ne pas bloquer.
"""

from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings


class Storage:
    """Enveloppe boto3 pour le bucket de l'application.

    Construite depuis les settings ; ``signature_version="s3v4"`` est requis
    pour les URL présignées PUT compatibles avec MinIO et S3.
    """

    def __init__(self) -> None:
        self._bucket = settings.S3_BUCKET
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT_URL or None,
            region_name=settings.S3_REGION,
            aws_access_key_id=settings.S3_ACCESS_KEY or None,
            aws_secret_access_key=settings.S3_SECRET_KEY or None,
            config=Config(signature_version="s3v4"),
        )

    def presign_put(self, s3_key: str, content_type: str) -> str:
        """URL présignée pour l'upload direct (PUT) d'un objet.

        Le ``Content-Type`` est figé dans la signature : le navigateur doit
        envoyer exactement ce type, sinon S3 rejette le PUT.
        """
        return self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self._bucket, "Key": s3_key, "ContentType": content_type},
            ExpiresIn=settings.S3_PRESIGN_PUT_TTL,
        )

    def presign_get(self, s3_key: str, nom_original: str) -> str:
        """URL présignée (GET, TTL court) pour lire/télécharger un objet.

        ``ResponseContentDisposition`` restitue le nom de fichier d'origine.
        """
        disposition = f'attachment; filename="{nom_original}"'
        return self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": s3_key,
                "ResponseContentDisposition": disposition,
            },
            ExpiresIn=settings.S3_PRESIGN_GET_TTL,
        )

    async def head(self, s3_key: str) -> dict | None:
        """Métadonnées de l'objet (``ContentLength``/``ContentType``), ou ``None``.

        Sert la confirmation d'upload (cohérence DB↔S3) : ``None`` si l'objet
        n'existe pas encore (404/NoSuchKey), l'upload n'a donc pas eu lieu.
        """
        try:
            return await run_in_threadpool(
                self._client.head_object, Bucket=self._bucket, Key=s3_key
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    async def delete_many(self, s3_keys: list[str]) -> None:
        """Supprime en lot les objets donnés (no-op si la liste est vide)."""
        if not s3_keys:
            return
        await run_in_threadpool(
            self._client.delete_objects,
            Bucket=self._bucket,
            Delete={"Objects": [{"Key": key} for key in s3_keys], "Quiet": True},
        )


@lru_cache
def get_storage() -> Storage:
    """Dépendance FastAPI : client S3 partagé (overridable en test)."""
    return Storage()
