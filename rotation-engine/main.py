"""
Gengar — Rotation Engine: FastAPI internal service.

Provides endpoints for proxy selection, pool management, and strategy
configuration. Consumed internally by the gateway and api-server.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import json
from contextlib import asynccontextmanager
from typing import Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from pool import ProxyPool, DEAD_SET_KEY
from strategies import (
    OnBlockStrategy,
    PerRequestStrategy,
    PerSessionStrategy,
    RoundRobinStrategy,
    TimeBasedStrategy,
    RotationStrategy,
)

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("rotation-engine")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "service": "rotation-engine",
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


for h in logging.root.handlers:
    h.setFormatter(JsonFormatter())


# ── Models ───────────────────────────────────────────────────


class NextProxyRequest(BaseModel):
    session_id: Optional[str] = None
    target_domain: Optional[str] = None


class MarkBlockRequest(BaseModel):
    ip: str
    port: int
    target_domain: Optional[str] = None


class StrategyUpdate(BaseModel):
    strategy: str
    session_ttl: Optional[int] = None
    rotation_interval: Optional[int] = None


# ── App ──────────────────────────────────────────────────────

STRATEGY_MAP: dict[str, type[RotationStrategy]] = {
    "per-request": PerRequestStrategy,
    "per-session": PerSessionStrategy,
    "time-based": TimeBasedStrategy,
    "on-block": OnBlockStrategy,
    "round-robin": RoundRobinStrategy,
}

redis_client: aioredis.Redis | None = None
pool: ProxyPool | None = None


@asynccontextmanager
async def lifespan(application: FastAPI):
    global redis_client, pool
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    redis_client = aioredis.from_url(redis_url, decode_responses=True)
    pool = ProxyPool(redis_client)

    # Seed default strategy config if not present
    current = await pool.get_config("rotation_strategy")
    if current is None:
        await pool.set_config(
            "rotation_strategy", os.getenv("ROTATION_STRATEGY", "per-request")
        )
        await pool.set_config("session_ttl", int(os.getenv("SESSION_TTL", "300")))
        await pool.set_config(
            "rotation_interval", int(os.getenv("ROTATION_INTERVAL", "30"))
        )

    logger.info("Rotation engine started — Redis connected")
    yield

    # Graceful shutdown
    logger.info("Shutting down rotation engine…")
    await redis_client.aclose()


app = FastAPI(title="Gengar Rotation Engine", lifespan=lifespan)


def _get_pool() -> ProxyPool:
    assert pool is not None
    return pool


async def _get_strategy() -> RotationStrategy:
    p = _get_pool()
    name = await p.get_config("rotation_strategy", "per-request")
    cls = STRATEGY_MAP.get(name, PerRequestStrategy)
    return cls(p)


# ── Endpoints ────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok", "service": "rotation-engine"}


@app.post("/next-proxy")
async def next_proxy(req: NextProxyRequest):
    """Select the next proxy based on current rotation strategy."""
    p = _get_pool()
    strategy = await _get_strategy()

    # Check for domain-specific override
    if req.target_domain:
        override = await p.get_domain_override(req.target_domain)
        if override:
            override_name = override.get("strategy", "per-request")
            cls = STRATEGY_MAP.get(override_name, PerRequestStrategy)
            strategy = cls(p)

    context = {
        "session_id": req.session_id,
        "target_domain": req.target_domain,
        "session_ttl": await p.get_config("session_ttl", 300),
        "rotation_interval": await p.get_config("rotation_interval", 30),
    }

    proxy = await strategy.select(context)
    if not proxy:
        raise HTTPException(status_code=503, detail="No healthy proxies available")

    return {
        "ip": proxy["ip"],
        "port": proxy["port"],
        "protocol": proxy.get("protocol", "http"),
        "health_score": proxy.get("health_score", 0),
        "latency_ms": proxy.get("latency_ms", 0),
    }


@app.post("/mark-block")
async def mark_block(req: MarkBlockRequest):
    """Mark a proxy as blocked (increment fail, remove from pool)."""
    p = _get_pool()
    await p.record_failure(req.ip, req.port)
    await p.mark_dead(req.ip, req.port)
    await p.incr_stat("blocks")
    logger.info(
        json.dumps(
            {
                "event": "proxy_blocked",
                "proxy": f"{req.ip}:{req.port}",
                "domain": req.target_domain,
            }
        )
    )
    return {"status": "marked_dead"}


@app.get("/strategy")
async def get_strategy():
    p = _get_pool()
    return {
        "strategy": await p.get_config("rotation_strategy", "per-request"),
        "session_ttl": await p.get_config("session_ttl", 300),
        "rotation_interval": await p.get_config("rotation_interval", 30),
    }


@app.post("/strategy")
async def update_strategy(req: StrategyUpdate):
    if req.strategy not in STRATEGY_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {req.strategy}")
    p = _get_pool()
    await p.set_config("rotation_strategy", req.strategy)
    if req.session_ttl is not None:
        await p.set_config("session_ttl", req.session_ttl)
    if req.rotation_interval is not None:
        await p.set_config("rotation_interval", req.rotation_interval)
    logger.info(json.dumps({"event": "strategy_updated", "strategy": req.strategy}))
    return {"status": "updated"}


@app.get("/pool/stats")
async def pool_stats():
    p = _get_pool()
    stats = await p.get_stats()
    healthy = await p.healthy_count()
    dead = await p.dead_count()
    total = await p.pool_size()
    return {
        "total": total,
        "healthy": healthy,
        "dead": dead,
        "requests": stats.get("requests", 0),
        "blocks": stats.get("blocks", 0),
    }


@app.get("/pool/list")
async def pool_list(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None, pattern="^(healthy|dead|all)$"),
):
    p = _get_pool()
    all_proxies = await p.get_all_proxies()

    dead_addrs = await p.redis.smembers(DEAD_SET_KEY) if p.redis else set()

    for proxy in all_proxies:
        addr = f"{proxy['ip']}:{proxy['port']}"
        proxy["status"] = "dead" if addr in dead_addrs else "healthy"

    if status and status != "all":
        all_proxies = [px for px in all_proxies if px["status"] == status]

    all_proxies.sort(
        key=lambda x: (-x.get("health_score", 0), x.get("latency_ms", 9999))
    )
    total = len(all_proxies)
    start = (page - 1) * per_page
    end = start + per_page
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "proxies": all_proxies[start:end],
    }
