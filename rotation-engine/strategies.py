"""
Gengar — Rotation Strategies.

Five proxy-rotation strategies, all sharing the same interface:
    async select(context: dict) -> Optional[dict]

Strategies: per-request, per-session, time-based, on-block, round-robin.
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pool import ProxyPool


class RotationStrategy(ABC):
    """Base class for all rotation strategies."""

    name: str = ""

    def __init__(self, pool: ProxyPool) -> None:
        self.pool = pool

    @abstractmethod
    async def select(self, context: dict) -> Optional[dict]:
        """Pick the next proxy from the healthy pool.

        Args:
            context: dict with optional keys:
                - session_id (str)
                - target_domain (str)
                - session_ttl (int, seconds)
                - rotation_interval (int, seconds)
                - country (str, ISO-2 filter)

        Returns:
            Proxy dict or None if pool is empty.
        """


class PerRequestStrategy(RotationStrategy):
    """New random proxy for every single request (default)."""

    name = "per-request"

    async def select(self, context: dict) -> Optional[dict]:
        proxies = await self.pool.get_healthy_proxies()
        if not proxies:
            return None
        # Weighted random: prefer higher health scores
        weights = [max(p.get("health_score", 1), 1) for p in proxies]
        return random.choices(proxies, weights=weights, k=1)[0]


class PerSessionStrategy(RotationStrategy):
    """Sticky proxy per session ID. Rotates on expiry or block."""

    name = "per-session"

    async def select(self, context: dict) -> Optional[dict]:
        session_id = context.get("session_id")
        ttl = int(context.get("session_ttl", 300))

        if session_id:
            cached = await self.pool.get_session_proxy(session_id)
            if cached:
                # Verify it's still healthy
                addr = f"{cached['ip']}:{cached['port']}"
                dead = await self.pool.redis.sismember("gengar:pool:dead", addr)
                if not dead:
                    return cached

        # Assign a new proxy for this session
        proxies = await self.pool.get_healthy_proxies()
        if not proxies:
            return None
        proxy = random.choice(proxies)

        if session_id:
            await self.pool.set_session_proxy(session_id, proxy, ttl=ttl)
        return proxy


class TimeBasedStrategy(RotationStrategy):
    """Rotate every N seconds regardless of request count."""

    name = "time-based"
    _current_proxy: Optional[dict] = None
    _last_rotation: float = 0.0

    async def select(self, context: dict) -> Optional[dict]:
        interval = int(context.get("rotation_interval", 30))
        now = time.time()

        # Read last rotation time from Redis for persistence across restarts
        last_raw = await self.pool.get_config("time_based_last_rotation", 0)
        last_rotation = float(last_raw)

        current_raw = await self.pool.get_config("time_based_current_proxy")

        if current_raw and (now - last_rotation) < interval:
            # Verify still healthy
            addr = f"{current_raw['ip']}:{current_raw['port']}"
            dead = await self.pool.redis.sismember("gengar:pool:dead", addr)
            if not dead:
                return current_raw

        # Time to rotate
        proxies = await self.pool.get_healthy_proxies()
        if not proxies:
            return None
        proxy = random.choice(proxies)
        await self.pool.set_config("time_based_current_proxy", proxy)
        await self.pool.set_config("time_based_last_rotation", now)
        return proxy


class OnBlockStrategy(RotationStrategy):
    """Keep using the same proxy until a block is detected."""

    name = "on-block"

    async def select(self, context: dict) -> Optional[dict]:
        current = await self.pool.get_config("on_block_current_proxy")

        if current:
            addr = f"{current['ip']}:{current['port']}"
            dead = await self.pool.redis.sismember("gengar:pool:dead", addr)
            if not dead:
                return current

        # Current proxy was blocked or none assigned → pick new one
        proxies = await self.pool.get_healthy_proxies()
        if not proxies:
            return None
        proxy = proxies[0]  # Best health score first
        await self.pool.set_config("on_block_current_proxy", proxy)
        return proxy


class RoundRobinStrategy(RotationStrategy):
    """Cycle through pool in order, no randomness."""

    name = "round-robin"

    async def select(self, context: dict) -> Optional[dict]:
        proxies = await self.pool.get_healthy_proxies()
        if not proxies:
            return None

        idx = await self.pool.get_rr_index()
        if idx >= len(proxies):
            idx = 0

        proxy = proxies[idx]
        await self.pool.set_rr_index(idx + 1)
        return proxy
