"""RedisCache adapter branches: healthy passthrough and the no-raise
degraded contract when Redis is unreachable."""

from catalog.adapters.cache import RedisCache
from redis.exceptions import RedisError


class StubRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.set_kwargs: list[dict] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, **kwargs):
        self.set_kwargs.append(kwargs)
        if kwargs.get("nx") and key in self.store:
            return None  # redis semantics: NX on existing key → nil
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)


class BoomRedis:
    async def get(self, key):
        raise RedisError("down")

    async def set(self, key, value, **kwargs):
        raise RedisError("down")

    async def delete(self, key):
        raise RedisError("down")


async def test_healthy_passthrough():
    stub = StubRedis()
    cache = RedisCache(stub)  # type: ignore[arg-type]
    await cache.set("k", "v", 60)
    assert stub.set_kwargs[0] == {"ex": 60}
    assert await cache.get("k") == "v"
    await cache.delete("k")
    assert await cache.get("k") is None


async def test_healthy_lock_cycle():
    stub = StubRedis()
    cache = RedisCache(stub)  # type: ignore[arg-type]
    assert await cache.acquire_lock("lock", 3000) is True
    assert stub.set_kwargs[0] == {"nx": True, "px": 3000}
    assert await cache.acquire_lock("lock", 3000) is False  # held
    await cache.release_lock("lock")
    assert await cache.acquire_lock("lock", 3000) is True  # released


async def test_down_redis_degrades_never_raises():
    cache = RedisCache(BoomRedis())  # type: ignore[arg-type]
    assert await cache.get("k") is None  # miss, not error
    await cache.set("k", "v", 60)  # swallowed
    await cache.delete("k")  # swallowed
    assert await cache.acquire_lock("lock", 3000) is True  # render freely
    await cache.release_lock("lock")  # swallowed (PX ttl cleans up)
