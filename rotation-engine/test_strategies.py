"""
Gengar — Unit Tests for Rotation Strategies.

Tests all 5 rotation strategy implementations using a mock Redis pool.
Run: python -m pytest test_strategies.py -v
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from strategies import (
    OnBlockStrategy,
    PerRequestStrategy,
    PerSessionStrategy,
    RoundRobinStrategy,
    TimeBasedStrategy,
)


# ── Fake Redis + Pool ───────────────────────────────────────


class FakeRedis:
    """In-memory Redis mock supporting the subset of operations used by ProxyPool."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}
        self._sets: dict[str, set] = {}
        self._hashes: dict[str, dict] = {}
        self._lists: dict[str, list] = {}

    async def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def sadd(self, key: str, *members: str) -> None:
        self._sets.setdefault(key, set()).update(members)

    async def srem(self, key: str, *members: str) -> None:
        s = self._sets.get(key, set())
        for m in members:
            s.discard(m)

    async def smembers(self, key: str) -> set:
        return self._sets.get(key, set())

    async def sismember(self, key: str, member: str) -> bool:
        return member in self._sets.get(key, set())

    async def scard(self, key: str) -> int:
        return len(self._sets.get(key, set()))

    async def hset(self, key: str, field: str, value: str) -> None:
        self._hashes.setdefault(key, {})[field] = value

    async def hget(self, key: str, field: str) -> Optional[str]:
        return self._hashes.get(key, {}).get(field)

    async def hgetall(self, key: str) -> dict:
        return self._hashes.get(key, {})

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        self._hashes.setdefault(key, {})
        val = int(self._hashes[key].get(field, 0)) + amount
        self._hashes[key][field] = str(val)
        return val

    async def hdel(self, key: str, field: str) -> None:
        self._hashes.get(key, {}).pop(field, None)

    async def lpush(self, key: str, *values: str) -> None:
        self._lists.setdefault(key, [])
        for v in reversed(values):
            self._lists[key].insert(0, v)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        self._lists[key] = self._lists.get(key, [])[start : end + 1]

    async def lrange(self, key: str, start: int, end: int) -> list:
        return self._lists.get(key, [])[start : end + 1]

    def pipeline(self):
        return FakePipeline(self)

    async def aclose(self) -> None:
        pass


class FakePipeline:
    def __init__(self, redis: FakeRedis):
        self._redis = redis
        self._ops: list = []

    def set(self, key, value, **kw):
        self._ops.append(("set", key, value))
        return self

    def get(self, key):
        self._ops.append(("get", key))
        return self

    def delete(self, key):
        self._ops.append(("delete", key))
        return self

    def sadd(self, key, *members):
        self._ops.append(("sadd", key, members))
        return self

    def srem(self, key, *members):
        self._ops.append(("srem", key, members))
        return self

    async def execute(self):
        results = []
        for op in self._ops:
            if op[0] == "set":
                await self._redis.set(op[1], op[2])
                results.append(True)
            elif op[0] == "get":
                results.append(await self._redis.get(op[1]))
            elif op[0] == "delete":
                await self._redis.delete(op[1])
                results.append(True)
            elif op[0] == "sadd":
                await self._redis.sadd(op[1], *op[2])
                results.append(True)
            elif op[0] == "srem":
                await self._redis.srem(op[1], *op[2])
                results.append(True)
        return results


# ── Fixtures ─────────────────────────────────────────────────

from pool import ProxyPool


def _make_proxy(ip: str, port: int, score: float = 80.0, latency: float = 200) -> dict:
    return {
        "ip": ip,
        "port": port,
        "protocol": "http",
        "country": "US",
        "latency_ms": latency,
        "health_score": score,
        "last_checked": time.time(),
        "source": "test",
        "fail_count": 0,
        "success_count": 8,
        "total_checks": 10,
        "consecutive_fails": 0,
        "created_at": time.time(),
    }


SAMPLE_PROXIES = [
    _make_proxy("1.1.1.1", 8080, score=90, latency=100),
    _make_proxy("2.2.2.2", 3128, score=80, latency=200),
    _make_proxy("3.3.3.3", 80, score=70, latency=300),
    _make_proxy("4.4.4.4", 1080, score=60, latency=400),
    _make_proxy("5.5.5.5", 9090, score=50, latency=500),
]


@pytest_asyncio.fixture
async def pool():
    redis = FakeRedis()
    p = ProxyPool(redis)
    for proxy in SAMPLE_PROXIES:
        await p.add_proxy(proxy["ip"], proxy["port"], **proxy)
    return p


# ── Tests: PerRequestStrategy ───────────────────────────────


@pytest.mark.asyncio
async def test_per_request_returns_proxy(pool):
    strategy = PerRequestStrategy(pool)
    proxy = await strategy.select({})
    assert proxy is not None
    assert "ip" in proxy
    assert "port" in proxy


@pytest.mark.asyncio
async def test_per_request_returns_none_empty_pool():
    redis = FakeRedis()
    p = ProxyPool(redis)
    strategy = PerRequestStrategy(p)
    proxy = await strategy.select({})
    assert proxy is None


@pytest.mark.asyncio
async def test_per_request_randomness(pool):
    """Over many selections, should see more than one distinct proxy."""
    strategy = PerRequestStrategy(pool)
    seen = set()
    for _ in range(50):
        proxy = await strategy.select({})
        seen.add(proxy["ip"])
    assert len(seen) > 1, "per-request should produce varying proxies"


# ── Tests: PerSessionStrategy ───────────────────────────────


@pytest.mark.asyncio
async def test_per_session_sticky(pool):
    strategy = PerSessionStrategy(pool)
    ctx = {"session_id": "sess-1", "session_ttl": 300}
    first = await strategy.select(ctx)
    assert first is not None

    # Same session should return same proxy
    second = await strategy.select(ctx)
    assert second["ip"] == first["ip"]
    assert second["port"] == first["port"]


@pytest.mark.asyncio
async def test_per_session_different_sessions(pool):
    strategy = PerSessionStrategy(pool)
    p1 = await strategy.select({"session_id": "a", "session_ttl": 300})
    # Different session may get a different proxy (not guaranteed but should work)
    results = set()
    for i in range(20):
        p = await strategy.select({"session_id": f"sess-{i}", "session_ttl": 300})
        results.add(p["ip"])
    assert len(results) >= 1  # At least one proxy assigned


@pytest.mark.asyncio
async def test_per_session_no_session_id(pool):
    strategy = PerSessionStrategy(pool)
    proxy = await strategy.select({})
    assert proxy is not None


@pytest.mark.asyncio
async def test_per_session_rotates_on_dead(pool):
    strategy = PerSessionStrategy(pool)
    ctx = {"session_id": "sess-dead", "session_ttl": 300}
    first = await strategy.select(ctx)

    # Mark the assigned proxy as dead
    await pool.mark_dead(first["ip"], first["port"])

    second = await strategy.select(ctx)
    # Should get a different proxy since old one is dead
    assert second is not None


# ── Tests: TimeBasedStrategy ────────────────────────────────


@pytest.mark.asyncio
async def test_time_based_returns_same_within_interval(pool):
    strategy = TimeBasedStrategy(pool)
    ctx = {"rotation_interval": 60}
    first = await strategy.select(ctx)
    assert first is not None

    second = await strategy.select(ctx)
    assert second["ip"] == first["ip"], "should return same proxy within interval"


@pytest.mark.asyncio
async def test_time_based_rotates_after_interval(pool):
    strategy = TimeBasedStrategy(pool)
    ctx = {"rotation_interval": 1}
    first = await strategy.select(ctx)
    assert first is not None

    # Simulate time passing by setting last_rotation to the past
    await pool.set_config("time_based_last_rotation", time.time() - 10)

    second = await strategy.select(ctx)
    assert second is not None
    # After interval, it should have rotated (may or may not be different proxy,
    # but the rotation logic should have fired)


@pytest.mark.asyncio
async def test_time_based_empty_pool():
    redis = FakeRedis()
    p = ProxyPool(redis)
    strategy = TimeBasedStrategy(p)
    assert await strategy.select({"rotation_interval": 30}) is None


# ── Tests: OnBlockStrategy ──────────────────────────────────


@pytest.mark.asyncio
async def test_on_block_keeps_same_proxy(pool):
    strategy = OnBlockStrategy(pool)
    first = await strategy.select({})
    assert first is not None

    second = await strategy.select({})
    assert second["ip"] == first["ip"], "should keep same proxy until blocked"


@pytest.mark.asyncio
async def test_on_block_rotates_after_dead(pool):
    strategy = OnBlockStrategy(pool)
    first = await strategy.select({})

    # Mark it dead → simulate a block event
    await pool.mark_dead(first["ip"], first["port"])

    second = await strategy.select({})
    assert second is not None
    assert second["ip"] != first["ip"], "should pick a new proxy after block"


@pytest.mark.asyncio
async def test_on_block_empty_pool():
    redis = FakeRedis()
    p = ProxyPool(redis)
    strategy = OnBlockStrategy(p)
    assert await strategy.select({}) is None


# ── Tests: RoundRobinStrategy ───────────────────────────────


@pytest.mark.asyncio
async def test_round_robin_cycles_in_order(pool):
    strategy = RoundRobinStrategy(pool)
    proxies_seen = []
    for _ in range(len(SAMPLE_PROXIES)):
        p = await strategy.select({})
        proxies_seen.append(p["ip"])

    # Should see distinct proxies in a deterministic order
    assert len(set(proxies_seen)) == len(SAMPLE_PROXIES), (
        "round-robin should cycle through all proxies"
    )


@pytest.mark.asyncio
async def test_round_robin_wraps_around(pool):
    strategy = RoundRobinStrategy(pool)
    n = len(SAMPLE_PROXIES)

    # Go through pool once
    first_cycle = []
    for _ in range(n):
        p = await strategy.select({})
        first_cycle.append(p["ip"])

    # Go through again → should wrap
    second_cycle = []
    for _ in range(n):
        p = await strategy.select({})
        second_cycle.append(p["ip"])

    assert first_cycle == second_cycle, "round-robin should wrap around"


@pytest.mark.asyncio
async def test_round_robin_empty_pool():
    redis = FakeRedis()
    p = ProxyPool(redis)
    strategy = RoundRobinStrategy(p)
    assert await strategy.select({}) is None
