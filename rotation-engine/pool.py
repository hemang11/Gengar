"""
Gengar — Rotation Engine: Redis Pool CRUD Operations.

Manages the proxy pool stored in Redis. Each proxy is a JSON hash
keyed by ``proxy:<ip>:<port>``. Provides add, remove, query, and
scoring helpers consumed by the rotation strategies and the scraper.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import redis.asyncio as aioredis

PROXY_KEY_PREFIX = "gengar:proxy:"
POOL_INDEX_KEY = "gengar:pool:index"
DEAD_SET_KEY = "gengar:pool:dead"
HEALTHY_SET_KEY = "gengar:pool:healthy"
SESSION_KEY_PREFIX = "gengar:session:"
STATS_KEY = "gengar:stats"
ROUND_ROBIN_KEY = "gengar:rr:index"


def _proxy_key(ip: str, port: int) -> str:
    return f"{PROXY_KEY_PREFIX}{ip}:{port}"


def _default_proxy(ip: str, port: int, **overrides: Any) -> dict:
    now = time.time()
    base = {
        "ip": ip,
        "port": port,
        "protocol": "http",
        "country": "",
        "latency_ms": 0,
        "health_score": 0.0,
        "last_checked": now,
        "source": "",
        "fail_count": 0,
        "success_count": 0,
        "total_checks": 0,
        "consecutive_fails": 0,
        "created_at": now,
    }
    base.update(overrides)
    return base


class ProxyPool:
    """Async Redis-backed proxy pool."""

    def __init__(self, redis: aioredis.Redis) -> None:
        self.redis = redis

    # ── Create / Update ──────────────────────────────────────

    async def add_proxy(self, ip: str, port: int, **kwargs: Any) -> dict:
        """Add or update a proxy in the pool."""
        key = _proxy_key(ip, port)
        existing_raw = await self.redis.get(key)
        if existing_raw:
            proxy = json.loads(existing_raw)
            proxy.update({k: v for k, v in kwargs.items() if v is not None})
        else:
            proxy = _default_proxy(ip, port, **kwargs)

        await self.redis.set(key, json.dumps(proxy))
        await self.redis.sadd(POOL_INDEX_KEY, f"{ip}:{port}")
        await self.redis.srem(DEAD_SET_KEY, f"{ip}:{port}")
        # Only add to healthy set if it has some checks or we want to allow new ones as candidates
        if proxy.get("health_score", 0) >= 0:
            await self.redis.sadd(HEALTHY_SET_KEY, f"{ip}:{port}")
        return proxy

    async def bulk_add(self, proxies: list[dict]) -> int:
        """Add multiple proxies in a pipeline. Returns count added."""
        pipe = self.redis.pipeline()
        count = 0
        for p in proxies:
            ip, port = p["ip"], p["port"]
            addr = f"{ip}:{port}"
            key = _proxy_key(ip, port)
            proxy = _default_proxy(ip, port, **p)
            pipe.set(key, json.dumps(proxy))
            pipe.sadd(POOL_INDEX_KEY, addr)
            pipe.srem(DEAD_SET_KEY, addr)
            pipe.sadd(HEALTHY_SET_KEY, addr)
            count += 1
        await pipe.execute()
        return count

    # ── Read ─────────────────────────────────────────────────

    async def get_proxy(self, ip: str, port: int) -> Optional[dict]:
        """Get a single proxy by ip:port."""
        raw = await self.redis.get(_proxy_key(ip, port))
        return json.loads(raw) if raw else None

    async def get_all_proxies(self) -> list[dict]:
        """Return every proxy in the index (healthy + unhealthy)."""
        members = await self.redis.smembers(POOL_INDEX_KEY)
        if not members:
            return []
        pipe = self.redis.pipeline()
        for m in members:
            ip, port = m.rsplit(":", 1)
            pipe.get(_proxy_key(ip, int(port)))
        results = await pipe.execute()
        proxies = []
        for raw in results:
            if raw:
                proxies.append(json.loads(raw))
        return proxies

    async def get_healthy_proxies(self, min_score: float = 0.0) -> list[dict]:
        """Return proxies with health_score > min_score and not dead."""
        members = await self.redis.smembers(HEALTHY_SET_KEY)
        if not members:
            return []

        pipe = self.redis.pipeline()
        for m in members:
            ip, port = m.rsplit(":", 1)
            pipe.get(_proxy_key(ip, int(port)))
        results = await pipe.execute()

        healthy = []
        for raw in results:
            if not raw:
                continue
            p = json.loads(raw)
            if p.get("health_score", 0) >= min_score:
                healthy.append(p)

        healthy.sort(
            key=lambda x: (-x.get("health_score", 0), x.get("latency_ms", 9999))
        )
        return healthy

    async def pool_size(self) -> int:
        """Number of proxies in the index (includes dead)."""
        return await self.redis.scard(POOL_INDEX_KEY)

    async def healthy_count(self) -> int:
        """Number of non-dead proxies."""
        total = await self.redis.scard(POOL_INDEX_KEY)
        dead = await self.redis.scard(DEAD_SET_KEY)
        return max(total - dead, 0)

    async def dead_count(self) -> int:
        return await self.redis.scard(DEAD_SET_KEY)

    # ── Health / Scoring ─────────────────────────────────────

    async def record_success(self, ip: str, port: int, latency_ms: float) -> dict:
        """Record a successful health check / request."""
        key = _proxy_key(ip, port)
        raw = await self.redis.get(key)
        if not raw:
            return {}
        proxy = json.loads(raw)
        proxy["success_count"] = proxy.get("success_count", 0) + 1
        proxy["total_checks"] = proxy.get("total_checks", 0) + 1
        proxy["consecutive_fails"] = 0
        proxy["latency_ms"] = latency_ms
        proxy["last_checked"] = time.time()
        total = proxy["total_checks"]
        proxy["health_score"] = (proxy["success_count"] / total) * 100 if total else 0
        await self.redis.set(key, json.dumps(proxy))
        await self.redis.sadd(HEALTHY_SET_KEY, f"{ip}:{port}")
        await self.redis.srem(DEAD_SET_KEY, f"{ip}:{port}")
        return proxy

    async def record_failure(self, ip: str, port: int) -> dict:
        """Record a failed check. After 3 consecutive fails → mark dead."""
        key = _proxy_key(ip, port)
        raw = await self.redis.get(key)
        if not raw:
            return {}
        proxy = json.loads(raw)
        proxy["fail_count"] = proxy.get("fail_count", 0) + 1
        proxy["total_checks"] = proxy.get("total_checks", 0) + 1
        proxy["consecutive_fails"] = proxy.get("consecutive_fails", 0) + 1
        proxy["last_checked"] = time.time()
        total = proxy["total_checks"]
        proxy["health_score"] = (
            (proxy.get("success_count", 0) / total) * 100 if total else 0
        )
        await self.redis.set(key, json.dumps(proxy))

        if proxy["consecutive_fails"] >= 3:
            await self.mark_dead(ip, port)
        return proxy

    async def mark_dead(self, ip: str, port: int) -> None:
        """Move a proxy to the dead set."""
        addr = f"{ip}:{port}"
        await self.redis.sadd(DEAD_SET_KEY, addr)
        await self.redis.srem(HEALTHY_SET_KEY, addr)

    async def remove_proxy(self, ip: str, port: int) -> None:
        """Permanently remove a proxy from the pool."""
        addr = f"{ip}:{port}"
        key = _proxy_key(ip, port)
        pipe = self.redis.pipeline()
        pipe.delete(key)
        pipe.srem(POOL_INDEX_KEY, addr)
        pipe.srem(DEAD_SET_KEY, addr)
        pipe.srem(HEALTHY_SET_KEY, addr)
        await pipe.execute()

    async def flush_dead(self) -> int:
        """Remove all dead proxies from pool entirely. Returns count flushed."""
        dead = await self.redis.smembers(DEAD_SET_KEY)
        if not dead:
            return 0
        pipe = self.redis.pipeline()
        for addr in dead:
            ip, port = addr.rsplit(":", 1)
            pipe.delete(_proxy_key(ip, int(port)))
            pipe.srem(POOL_INDEX_KEY, addr)
        pipe.delete(DEAD_SET_KEY)
        await pipe.execute()
        return len(dead)

    # ── Sessions ─────────────────────────────────────────────

    async def set_session_proxy(
        self, session_id: str, proxy: dict, ttl: int = 300
    ) -> None:
        """Pin a proxy to a session ID with TTL."""
        key = f"{SESSION_KEY_PREFIX}{session_id}"
        await self.redis.set(key, json.dumps(proxy), ex=ttl)

    async def get_session_proxy(self, session_id: str) -> Optional[dict]:
        """Get pinned proxy for a session."""
        raw = await self.redis.get(f"{SESSION_KEY_PREFIX}{session_id}")
        return json.loads(raw) if raw else None

    # ── Round-Robin state ────────────────────────────────────

    async def get_rr_index(self) -> int:
        return int(await self.redis.get(ROUND_ROBIN_KEY) or 0)

    async def set_rr_index(self, idx: int) -> None:
        await self.redis.set(ROUND_ROBIN_KEY, str(idx))

    # ── Stats helpers ────────────────────────────────────────

    async def incr_stat(self, field: str, amount: int = 1) -> None:
        await self.redis.hincrby(STATS_KEY, field, amount)

    async def get_stats(self) -> dict:
        raw = await self.redis.hgetall(STATS_KEY)
        return {k: int(v) for k, v in raw.items()} if raw else {}

    async def reset_stats(self) -> None:
        await self.redis.delete(STATS_KEY)

    # ── Request log (for live traffic) ───────────────────────

    async def log_request(self, entry: dict, max_entries: int = 500) -> None:
        """Push a request entry to the log list and trim to max_entries."""
        await self.redis.lpush("gengar:request_log", json.dumps(entry))
        await self.redis.ltrim("gengar:request_log", 0, max_entries - 1)

    async def get_recent_requests(self, count: int = 100) -> list[dict]:
        raw_list = await self.redis.lrange("gengar:request_log", 0, count - 1)
        return [json.loads(r) for r in raw_list]

    # ── Config persistence ───────────────────────────────────

    async def set_config(self, key: str, value: Any) -> None:
        await self.redis.set(f"gengar:config:{key}", json.dumps(value))

    async def get_config(self, key: str, default: Any = None) -> Any:
        raw = await self.redis.get(f"gengar:config:{key}")
        return json.loads(raw) if raw else default

    # ── Domain overrides ─────────────────────────────────────

    async def set_domain_override(self, domain: str, config: dict) -> None:
        await self.redis.hset("gengar:domain_overrides", domain, json.dumps(config))

    async def get_domain_overrides(self) -> dict[str, dict]:
        raw = await self.redis.hgetall("gengar:domain_overrides")
        return {k: json.loads(v) for k, v in raw.items()} if raw else {}

    async def get_domain_override(self, domain: str) -> Optional[dict]:
        raw = await self.redis.hget("gengar:domain_overrides", domain)
        return json.loads(raw) if raw else None

    async def delete_domain_override(self, domain: str) -> None:
        await self.redis.hdel("gengar:domain_overrides", domain)
