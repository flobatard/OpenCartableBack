"""Accès au stockage objet S3 — bucket privé, URL présignées.

**Seul module autorisé à importer boto3** (même exigence de remplaçabilité que
l'IdP dans :mod:`app.core.auth` : changer de backend S3 ne doit toucher qu'ici).

Le bucket n'est jamais public : tout accès passe par une URL présignée mintée
par l'API — ``PUT`` pour l'upload direct navigateur→S3 (on ne fait pas transiter
les binaires par le backend, contrainte Pi), ``GET`` à TTL court pour la lecture.
**Seule exception** : l'export/import de cours (:mod:`app.course_transfer`) lit
et écrit les binaires via l'API (``read_object_into``/``put_object``), volumes
bornés par ``TRANSFER_MAX_ZIP_BYTES``.

``generate_presigned_url`` est du **calcul local** (signature, aucune I/O
réseau) : l'appeler de façon synchrone dans un handler async ne bloque pas
l'event loop. En revanche ``head_object``/``delete_objects``/``list_objects_v2``
parlent au réseau : ils sont déportés dans un thread
(:func:`run_in_threadpool`) pour ne pas bloquer.

``iter_objects`` est la seule façon d'énumérer le bucket ; elle n'existe que
pour la réconciliation des orphelins du job de purge (:mod:`app.maintenance`)
— l'API, elle, ne liste jamais S3 (elle liste la table ``resources``).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import BinaryIO

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from fastapi.concurrency import run_in_threadpool

from app.core.config import settings

# ``delete_objects`` plafonne à 1 000 clés par appel ; ``list_objects_v2`` à
# 1 000 par page. Mêmes lots des deux côtés : une page listée = un lot supprimé.
DELETE_BATCH_SIZE = 1000
LIST_PAGE_SIZE = 1000


@dataclass(frozen=True)
class S3Object:
    """Un objet du bucket, tel que listé (jamais son contenu)."""

    key: str
    last_modified: datetime
    size: int


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

    def presign_get(
        self, s3_key: str, original_name: str, inline: bool = False
    ) -> str:
        """URL présignée (GET, TTL court) pour lire/télécharger un objet.

        ``ResponseContentDisposition`` restitue le nom de fichier d'origine ;
        ``inline=True`` demande au navigateur d'afficher l'objet au lieu de le
        télécharger (gateway de lecture), ``False`` force le téléchargement.
        """
        disposition_type = "inline" if inline else "attachment"
        disposition = f'{disposition_type}; filename="{original_name}"'
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

    def read_object_into(self, s3_key: str, fileobj: BinaryIO) -> None:
        """Copie l'objet S3 dans ``fileobj`` par chunks de 1 Mio (RAM bornée).

        ⚠ Réseau SYNCHRONE : à appeler uniquement depuis un thread — l'export
        de cours déporte l'assemblage complet du zip en un seul
        ``run_in_threadpool``. Lecture séquentielle via ``get_object`` (pas
        ``download_fileobj`` : son TransferManager écrit en concurrence avec
        des ``seek``, incompatible avec un flux d'entrée zip non seekable).
        """
        body = self._client.get_object(Bucket=self._bucket, Key=s3_key)["Body"]
        try:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                fileobj.write(chunk)
        finally:
            body.close()

    async def put_object(self, s3_key: str, fileobj: BinaryIO, content_type: str) -> None:
        """Upload d'un binaire (fileobj seekable) vers le bucket.

        Sert l'import de cours (:mod:`app.course_transfer`) — seule exception à
        la règle « les binaires ne transitent jamais par le backend ».
        """
        await run_in_threadpool(
            self._client.put_object,
            Bucket=self._bucket,
            Key=s3_key,
            Body=fileobj,
            ContentType=content_type,
        )

    async def delete_many(self, s3_keys: list[str]) -> None:
        """Supprime en lot les objets donnés (no-op si la liste est vide).

        Découpé par lots de ``DELETE_BATCH_SIZE`` : ``delete_objects`` plafonne
        à 1 000 clés par appel. Les suppressions unitaires (une ressource, un
        cours) tiennent toujours dans un lot ; le balayage d'orphelins du job
        de purge, lui, en dépasse.
        """
        for start in range(0, len(s3_keys), DELETE_BATCH_SIZE):
            batch = s3_keys[start : start + DELETE_BATCH_SIZE]
            await run_in_threadpool(
                self._client.delete_objects,
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )

    async def iter_objects(
        self, prefix: str, page_size: int = LIST_PAGE_SIZE
    ) -> AsyncIterator[list[S3Object]]:
        """Énumère le bucket sous ``prefix``, **page par page**.

        Rend des pages (au plus ``page_size`` objets) plutôt que des objets un
        à un : l'appelant traite un lot borné et ne tient jamais le bucket
        entier en RAM (contrainte Pi). Chaque page est un aller-retour réseau,
        déporté en thread comme ``head``/``delete_many``.

        Sert la réconciliation des orphelins (:mod:`app.maintenance`) ; aucun
        autre appelant n'a besoin de lister le bucket.
        """
        token: str | None = None
        while True:
            params = {"Bucket": self._bucket, "Prefix": prefix, "MaxKeys": page_size}
            if token:
                params["ContinuationToken"] = token
            response = await run_in_threadpool(self._client.list_objects_v2, **params)
            page = [
                S3Object(
                    key=item["Key"],
                    last_modified=item["LastModified"],
                    size=item["Size"],
                )
                for item in response.get("Contents", [])
            ]
            if page:
                yield page
            token = response.get("NextContinuationToken")
            if not response.get("IsTruncated") or not token:
                return


@lru_cache
def get_storage() -> Storage:
    """Dépendance FastAPI : client S3 partagé (overridable en test)."""
    return Storage()
