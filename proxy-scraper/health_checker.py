"""
Gengar — Proxy Scraper: Health Checker Worker.

Concurrently tests proxies via httpbin.org/ip. Manages semaphore-limited
workers, tracks latency, scores proxies, and removes dead ones after
3 consecutive failures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

import httpx
import redis.asyncio as aioredis

# ── Logging ──────────────────────────────────────────────────

logger = logging.getLogger("health-checker")


# ── Config ───────────────────────────────────────────────────

HEALTH_CHECK_URL = "https://httpbin.org/ip"
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_CHECKS", "200"))
TIMEOUT = int(os.getenv("HEALTH_CHECK_TIMEOUT", "8"))
HEALTH_CHECK_INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", "600"))


# ── Pool import (shared with scraper) ────────────────────────


class _PoolAdapter:
    """Lightweight Redis adapter matching the ProxyPool interface we need."""

    PROXY_KEY_PREFIX = "gengar:proxy:"
    POOL_INDEX_KEY = "gengar:pool:index"
    DEAD_SET_KEY = "gengar:pool:dead"
    HEALTHY_SET_KEY = "gengar:pool:healthy"

    def __init__(self, redis: aioredis.Redis) -> None:
        self.redis = redis

    async def get_all_members(self) -> set[str]:
        return await self.redis.smembers(self.POOL_INDEX_KEY)

    async def get_proxy(self, ip: str, port: int) -> dict | None:
        raw = await self.redis.get(f"{self.PROXY_KEY_PREFIX}{ip}:{port}")
        return json.loads(raw) if raw else None

    async def save_proxy(self, proxy: dict) -> None:
        addr = f"{proxy['ip']}:{proxy['port']}"
        key = f"{self.PROXY_KEY_PREFIX}{addr}"
        await self.redis.set(key, json.dumps(proxy))
        # Add to healthy set if we are saving it as healthy (called after success)
        if proxy.get("consecutive_fails", 0) == 0:
            await self.redis.sadd(self.HEALTHY_SET_KEY, addr)
            await self.redis.srem(self.DEAD_SET_KEY, addr)

    async def mark_dead(self, ip: str, port: int) -> None:
        addr = f"{ip}:{port}"
        await self.redis.sadd(self.DEAD_SET_KEY, addr)
        await self.redis.srem(self.HEALTHY_SET_KEY, addr)

    async def remove_proxy(self, ip: str, port: int) -> None:
        addr = f"{ip}:{port}"
        pipe = self.redis.pipeline()
        pipe.delete(f"{self.PROXY_KEY_PREFIX}{addr}")
        pipe.srem(self.POOL_INDEX_KEY, addr)
        pipe.srem(self.DEAD_SET_KEY, addr)
        pipe.srem(self.HEALTHY_SET_KEY, addr)
        await pipe.execute()

    async def is_dead(self, ip: str, port: int) -> bool:
        return await self.redis.sismember(self.DEAD_SET_KEY, f"{ip}:{port}")


async def check_single_proxy(
    proxy: dict,
    pool: _PoolAdapter,
    semaphore: asyncio.Semaphore,
) -> bool:
    """Test one proxy against httpbin.org/ip. Returns True if healthy."""
    ip = proxy["ip"]
    port = proxy["port"]
    proxy_url = f"http://{ip}:{port}"

    async with semaphore:
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=TIMEOUT,
                follow_redirects=False,
            ) as client:
                resp = await client.get(HEALTH_CHECK_URL)

            elapsed_ms = (time.monotonic() - start) * 1000

            if resp.status_code == 200:
                body = resp.json()
                if "origin" in body:
                    # Success
                    existing = await pool.get_proxy(ip, port)
                    if existing:
                        existing["success_count"] = existing.get("success_count", 0) + 1
                        existing["total_checks"] = existing.get("total_checks", 0) + 1
                        existing["consecutive_fails"] = 0
                        existing["latency_ms"] = round(elapsed_ms, 1)
                        existing["last_checked"] = time.time()
                        total = existing["total_checks"]
                        existing["health_score"] = (
                            (existing["success_count"] / total) * 100 if total else 0
                        )
                        await pool.save_proxy(existing)
                    logger.debug(
                        json.dumps(
                            {
                                "event": "health_check_pass",
                                "proxy": f"{ip}:{port}",
                                "latency_ms": round(elapsed_ms, 1),
                            }
                        )
                    )
                    return True

        except Exception:
            pass

        # Failure path
        existing = await pool.get_proxy(ip, port)
        if existing:
            existing["fail_count"] = existing.get("fail_count", 0) + 1
            existing["total_checks"] = existing.get("total_checks", 0) + 1
            existing["consecutive_fails"] = existing.get("consecutive_fails", 0) + 1
            existing["last_checked"] = time.time()
            total = existing["total_checks"]
            existing["health_score"] = (
                (existing.get("success_count", 0) / total) * 100 if total else 0
            )
            await pool.save_proxy(existing)

            if existing["consecutive_fails"] >= 3:
                await pool.remove_proxy(ip, port)
                logger.info(
                    json.dumps(
                        {
                            "event": "proxy_removed",
                            "proxy": f"{ip}:{port}",
                            "reason": "3_consecutive_fails",
                        }
                    )
                )
            else:
                await pool.mark_dead(ip, port)

        logger.debug(
            json.dumps({"event": "health_check_fail", "proxy": f"{ip}:{port}"})
        )
        return False


async def check_all_proxies(redis: aioredis.Redis) -> dict[str, int]:
    """Run health checks on every proxy in the pool. Returns stats."""
    pool = _PoolAdapter(redis)
    members = await pool.get_all_members()

    if not members:
        logger.info(json.dumps({"event": "health_check_skip", "reason": "empty_pool"}))
        return {"total": 0, "healthy": 0, "dead": 0}

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    proxies = []
    for m in members:
        parts = m.rsplit(":", 1)
        if len(parts) != 2:
            continue
        ip, port_str = parts
        p = await pool.get_proxy(ip, int(port_str))
        if p:
            proxies.append(p)

    logger.info(json.dumps({"event": "health_check_start", "count": len(proxies)}))

    tasks = [check_single_proxy(p, pool, semaphore) for p in proxies]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    healthy = sum(1 for r in results if r is True)
    dead = len(results) - healthy

    logger.info(
        json.dumps(
            {
                "event": "health_check_complete",
                "total": len(results),
                "healthy": healthy,
                "dead": dead,
            }
        )
    )

    return {"total": len(results), "healthy": healthy, "dead": dead}


async def run_periodic_checks(redis: aioredis.Redis) -> None:
    """Loop: re-check healthy proxies every HEALTH_CHECK_INTERVAL seconds."""
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)
        try:
            await check_all_proxies(redis)
        except Exception as exc:
            logger.error(
                json.dumps({"event": "periodic_check_error", "error": str(exc)})
            )
