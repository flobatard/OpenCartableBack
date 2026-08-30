"""Lecture/écriture de l'archive ``.zip`` d'un cours, réécriture des références.

Helpers **synchrones** (I/O fichier + S3) : à appeler depuis un thread
(``run_in_threadpool``), jamais directement dans l'event loop. Les helpers de
réécriture (``rewrite_refs``/``rewrite_block_content``) sont purs et testés
isolément.

Sécurité d'import : on ne lit **que** ``manifest.json`` et les entrées
``resources/<id>`` déclarées au manifest — jamais d'itération sur
``namelist()`` ni d'extraction de chemins arbitraires. Anti zip-bomb : la
taille déclarée de chaque entrée est vérifiée (``file_size`` du zip **et**
compteur au flux décompressé), la somme est bornée par
``TRANSFER_MAX_ZIP_BYTES`` et le manifest est lu borné.
"""

import json
import re
import zipfile
from tempfile import SpooledTemporaryFile

from pydantic import ValidationError

from app.core.config import settings
from app.core.storage import Storage
from app.course_transfer.schemas import (
    MAX_MANIFEST_BYTES,
    CourseManifest,
    normalize_manifest_v1,
)

MANIFEST_NAME = "manifest.json"

# Bascule RAM→disque des fichiers temporaires (petit cours en RAM, gros sur
# disque — jamais une archive entière en mémoire, contrainte Pi).
SPOOL_MAX_BYTES = 8 * 1024 * 1024
_CHUNK = 1024 * 1024

# Références inter-entités enfouies dans le markdown (schémas front
# course-resource-ref.ts / course-module-ref.ts) : ``oc-resource:<uuid>`` /
# ``oc-module:<uuid>``. L'uuid est validé en forme ; le lookup se fait en
# minuscules (uuid insensibles à la casse).
_UUID = (
    "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
REF_RE = re.compile(rf"\boc-(resource|module):({_UUID})\b")


class InvalidArchive(ValueError):
    """Archive illisible ou incohérente avec son manifest (→ 422)."""


def rewrite_refs(
    text: str, resource_map: dict[str, str], module_map: dict[str, str]
) -> str:
    """Remplace les uuid des références ``oc-*:`` selon les maps (clés minuscules).

    Une référence dont l'uuid est inconnu des maps est laissée **verbatim** :
    une référence cassée à l'export reste cassée à l'import (contenu
    strictement identique, jamais de réparation silencieuse).
    """

    def _sub(match: re.Match[str]) -> str:
        maps = resource_map if match.group(1) == "resource" else module_map
        new_ref = maps.get(match.group(2).lower())
        return f"oc-{match.group(1)}:{new_ref}" if new_ref else match.group(0)

    return REF_RE.sub(_sub, text)


def rewrite_block_content(
    type_: str,
    content: dict,
    resource_map: dict[str, str],
    module_map: dict[str, str],
) -> dict:
    """NOUVEAU dict de content aux références réécrites (jamais de mutation).

    Seuls les champs markdown sont réécrits : ``markdown`` (texte),
    ``statement`` et ``questions[].statement`` (exercice). ``caption`` et
    ``expected_answer`` sont du texte simple jamais rendu en markdown — non
    touchés.
    """
    if type_ == "text":
        return {
            **content,
            "markdown": rewrite_refs(content.get("markdown", ""), resource_map, module_map),
        }
    if type_ == "exercise":
        return {
            **content,
            "statement": rewrite_refs(content.get("statement", ""), resource_map, module_map),
            "questions": [
                {
                    **question,
                    "statement": rewrite_refs(
                        question.get("statement", ""), resource_map, module_map
                    ),
                }
                for question in content.get("questions", [])
            ],
        }
    return dict(content)


def build_zip_sync(
    manifest_bytes: bytes,
    resources: list[tuple[str, str]],
    storage: Storage,
) -> SpooledTemporaryFile:
    """Assemble l'archive d'export dans un fichier temporaire (thread S3 compris).

    ``resources`` = paires ``(id exporté, s3_key)``. Manifest en DEFLATE ;
    binaires en STORE (PDF/images/audio/vidéo déjà compressés — DEFLATE
    coûterait du CPU Pi pour ~0 % de gain), streamés depuis S3 par chunks —
    jamais un binaire entier en RAM. Le fichier est rembobiné, prêt à lire.
    """
    tmp = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
    with zipfile.ZipFile(tmp, "w") as zf:
        zf.writestr(MANIFEST_NAME, manifest_bytes, compress_type=zipfile.ZIP_DEFLATED)
        for entry_id, s3_key in resources:
            with zf.open(f"resources/{entry_id}", mode="w") as stream:
                storage.read_object_into(s3_key, stream)
    tmp.seek(0)
    return tmp


def parse_zip_sync(fileobj) -> tuple[zipfile.ZipFile, CourseManifest]:
    """Ouvre l'archive d'import et valide manifest + cohérence des entrées.

    Le ``ZipFile`` retourné reste ouvert : l'appelant en extraira les entrées
    binaires (``extract_entry_sync``). Toute incohérence → ``InvalidArchive``
    (422) : pas un zip, manifest absent/trop gros/invalide, entrée binaire
    déclarée absente, ``file_size`` ≠ ``size`` déclarée, somme des tailles
    au-dessus de ``TRANSFER_MAX_ZIP_BYTES``.
    """
    try:
        zf = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile as exc:
        raise InvalidArchive("Le fichier n'est pas une archive zip valide") from exc
    try:
        info = zf.getinfo(MANIFEST_NAME)
    except KeyError:
        raise InvalidArchive("manifest.json absent de l'archive") from None
    if info.file_size > MAX_MANIFEST_BYTES:
        raise InvalidArchive("manifest.json trop volumineux")
    with zf.open(info) as fh:
        raw = fh.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        # file_size mentait (zip-bomb) : le flux décompressé fait foi.
        raise InvalidArchive("manifest.json trop volumineux")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise InvalidArchive(f"manifest.json invalide : {exc}") from exc
    # Compat v1 : le dict brut est traduit AVANT validation (clés/valeurs
    # françaises historiques → nomenclature v2) ; toute autre version
    # inconnue échoue au Literal de CourseManifest (422).
    if isinstance(data, dict) and data.get("format_version") == 1:
        data = normalize_manifest_v1(data)
    try:
        manifest = CourseManifest.model_validate(data)
    except ValidationError as exc:
        raise InvalidArchive(f"manifest.json invalide : {exc}") from exc

    total = 0
    for resource in manifest.resources:
        name = f"resources/{resource.id}"
        try:
            entry = zf.getinfo(name)
        except KeyError:
            raise InvalidArchive(f"Entrée absente de l'archive : {name}") from None
        if entry.file_size != resource.size:
            raise InvalidArchive(
                f"Taille incohérente pour {name} "
                f"(déclarée {resource.size}, archive {entry.file_size})"
            )
        total += resource.size
    if total > settings.TRANSFER_MAX_ZIP_BYTES:
        raise InvalidArchive("Archive au-dessus du plafond global")
    return zf, manifest


def extract_entry_sync(
    zf: zipfile.ZipFile, name: str, expected_size: int
) -> SpooledTemporaryFile:
    """Extrait une entrée binaire vers un fichier temporaire, taille contrôlée.

    Double filet au-delà du ``file_size`` déclaré (déjà vérifié par
    ``parse_zip_sync``) : compteur sur le flux décompressé — au premier octet
    excédentaire, ``InvalidArchive``. ``zipfile`` vérifie le CRC en fin de
    lecture. Le fichier est rembobiné, prêt pour ``storage.put_object``.
    """
    tmp = SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
    total = 0
    try:
        with zf.open(name) as src:
            while chunk := src.read(_CHUNK):
                total += len(chunk)
                if total > expected_size:
                    raise InvalidArchive(f"{name} dépasse la taille déclarée")
                tmp.write(chunk)
        if total != expected_size:
            raise InvalidArchive(f"{name} plus courte que la taille déclarée")
    except zipfile.BadZipFile as exc:
        tmp.close()
        raise InvalidArchive(f"Entrée corrompue : {name}") from exc
    except InvalidArchive:
        tmp.close()
        raise
    tmp.seek(0)
    return tmp
