"""Magasin clé-valeur partagé (Redis) — le canal entre l'API et le scheduler.

**Seul module autorisé à importer redis** (même exigence de remplaçabilité que
boto3 dans :mod:`app.core.storage`) : les consommateurs ne voient qu'une
interface étroite, en chaînes, et une seule exception, :class:`KVUnavailable`.

Rien de ce qui vit ici n'est une donnée du domaine : demandes de passe
manuelle, statut publié par le scheduler (:mod:`app.maintenance.control`).
Redis tourne **sans persistance** (service compose `redis`) — un redémarrage
perd tout, et c'est acceptable par construction.

Le client ne se connecte qu'au premier appel : construire le magasin ne coûte
rien, et une API sans Redis démarre (seul le backoffice s'en trouve dégradé).
"""

from functools import lru_cache

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import settings

# Un Redis local répond en millisecondes : au-delà, il est en panne, et une
# requête du backoffice ne doit pas rester pendue.
CONNECT_TIMEOUT_SECONDS = 3
COMMAND_TIMEOUT_SECONDS = 5


class KVUnavailable(Exception):
    """Magasin injoignable ou en erreur."""


class KeyValueStore:
    """Les quelques opérations dont l'application a besoin, erreurs traduites."""

    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client

    async def set_if_absent(self, key: str, value: str, ttl_seconds: int) -> bool:
        """Pose la clé si elle n'existe pas (``SET NX EX``) ; ``False`` sinon."""
        try:
            return bool(await self._client.set(key, value, nx=True, ex=ttl_seconds))
        except RedisError as exc:
            raise KVUnavailable(str(exc)) from exc

    async def put(self, key: str, value: str) -> None:
        try:
            await self._client.set(key, value)
        except RedisError as exc:
            raise KVUnavailable(str(exc)) from exc

    async def get(self, key: str) -> str | None:
        try:
            return await self._client.get(key)
        except RedisError as exc:
            raise KVUnavailable(str(exc)) from exc

    async def get_many(self, keys: list[str]) -> list[str | None]:
        """Valeurs dans l'ordre des clés, ``None`` pour une clé absente (``MGET``)."""
        if not keys:
            return []
        try:
            return await self._client.mget(keys)
        except RedisError as exc:
            raise KVUnavailable(str(exc)) from exc

    async def take(self, key: str) -> str | None:
        """Lit et supprime d'un seul geste (``GETDEL``) : une seule prise, même à deux."""
        try:
            return await self._client.getdel(key)
        except RedisError as exc:
            raise KVUnavailable(str(exc)) from exc

    async def delete(self, key: str) -> None:
        try:
            await self._client.delete(key)
        except RedisError as exc:
            raise KVUnavailable(str(exc)) from exc

    async def close(self) -> None:
        await self._client.aclose()


@lru_cache
def get_kv() -> KeyValueStore:
    """Dépendance FastAPI : magasin partagé du process (overridable en test)."""
    return KeyValueStore(
        aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
            socket_timeout=COMMAND_TIMEOUT_SECONDS,
        )
    )


async def close_kv() -> None:
    """Ferme le client s'il a été créé (arrêt de l'API ou du scheduler)."""
    if get_kv.cache_info().currsize:
        await get_kv().close()
        get_kv.cache_clear()
