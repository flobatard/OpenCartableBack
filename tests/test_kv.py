"""Magasin clé-valeur (:mod:`app.core.kv`) — l'enveloppe du client redis.

Sans serveur : un faux client asynchrone enregistre les commandes. Ce qu'on
protège : les options qui portent la sémantique (``NX`` + ``EX`` du dépôt
d'une demande, ``GETDEL`` de sa prise) et la traduction de **toute** erreur
redis en ``KVUnavailable`` — la seule exception que voient les consommateurs.
"""

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.kv import KeyValueStore, KVUnavailable


class FakeRedis:
    def __init__(self, *, fail=False):
        self.calls: list[tuple] = []
        self.fail = fail

    def _record(self, *call):
        self.calls.append(call)
        if self.fail:
            raise RedisConnectionError("Connection refused")

    async def set(self, key, value, nx=False, ex=None):
        self._record("set", key, value, nx, ex)
        return True if nx else "OK"

    async def get(self, key):
        self._record("get", key)
        return "v"

    async def mget(self, keys):
        self._record("mget", keys)
        return [None for _ in keys]

    async def getdel(self, key):
        self._record("getdel", key)
        return "v"

    async def delete(self, key):
        self._record("delete", key)

    async def aclose(self):
        self._record("aclose")


@pytest.mark.anyio
async def test_set_if_absent_is_a_set_nx_with_expiry():
    client = FakeRedis()

    assert await KeyValueStore(client).set_if_absent("k", "v", 3600) is True
    assert client.calls == [("set", "k", "v", True, 3600)]


@pytest.mark.anyio
async def test_take_is_an_atomic_getdel():
    client = FakeRedis()

    assert await KeyValueStore(client).take("k") == "v"
    assert client.calls == [("getdel", "k")]


@pytest.mark.anyio
async def test_get_many_of_nothing_does_not_call_redis():
    client = FakeRedis()

    assert await KeyValueStore(client).get_many([]) == []
    assert client.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "operation",
    [
        lambda kv: kv.set_if_absent("k", "v", 1),
        lambda kv: kv.put("k", "v"),
        lambda kv: kv.get("k"),
        lambda kv: kv.get_many(["k"]),
        lambda kv: kv.take("k"),
        lambda kv: kv.delete("k"),
    ],
)
async def test_every_redis_error_becomes_kv_unavailable(operation):
    with pytest.raises(KVUnavailable, match="Connection refused"):
        await operation(KeyValueStore(FakeRedis(fail=True)))
