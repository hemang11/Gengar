"""
Gengar — API Server: FastAPI REST API + WebSocket Live Stream.

Provides all REST endpoints for the Web UI and external consumers.
WebSocket endpoint streams live request events from Redis pub/sub.
All non-health endpoints require Bearer token auth via API_SECRET.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import redis.asyncio as aioredis
from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("api-server")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "service": "api-server",
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


for h in logging.root.handlers:
    h.setFormatter(JsonFormatter())


# ── Config ───────────────────────────────────────────────────

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
API_SECRET = os.getenv("API_SECRET", "changeme")

# ── Models ───────────────────────────────────────────────────


class RotationRulesUpdate(BaseModel):
    strategy: str
    session_ttl: Optional[int] = None
    rotation_interval: Optional[int] = None


class DomainOverride(BaseModel):
    domain: str
    strategy: str
    country: Optional[str] = None


# ── App ──────────────────────────────────────────────────────

redis_client: aioredis.Redis | None = None
rotation_http: httpx.AsyncClient | None = None
security = HTTPBearer()


@asynccontextmanager
async def lifespan(application: FastAPI):
    global redis_client, rotation_http
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    rotation_http = httpx.AsyncClient(
        base_url="http://rotation-engine:8001", timeout=10
    )
    logger.info(json.dumps({"event": "startup", "service": "api-server"}))
    yield
    logger.info(json.dumps({"event": "shutdown", "service": "api-server"}))
    await rotation_http.aclose()
    await redis_client.aclose()


app = FastAPI(title="Gengar API Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth ─────────────────────────────────────────────────────


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != API_SECRET:
        raise HTTPException(status_code=401, detail="Invalid API secret")
    return credentials


# ── Health ───────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok", "service": "api-server"}


# ── Stats ────────────────────────────────────────────────────


@app.get("/api/stats", dependencies=[Depends(verify_token)])
async def get_stats():
    """Aggregate metrics snapshot."""
    try:
        resp = await rotation_http.get("/pool/stats")
        pool_stats = resp.json()
    except Exception:
        pool_stats = {}

    # Calculate req/sec and block rate from recent requests
    recent = await redis_client.lrange("gengar:request_log", 0, 99)
    requests_list = [json.loads(r) for r in recent] if recent else []

    total_reqs = pool_stats.get("requests", len(requests_list))
    total_blocks = pool_stats.get("blocks", 0)
    block_rate = round((total_blocks / total_reqs) * 100, 1) if total_reqs > 0 else 0.0

    # Avg latency from recent requests
    latencies = [r.get("latency_ms", 0) for r in requests_list if r.get("latency_ms")]
    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else 0.0

    # Req/sec from last minute
    now = time.time()
    recent_minute = [r for r in requests_list if (now - r.get("ts", 0)) < 60]
    req_per_sec = round(len(recent_minute) / 60, 2) if recent_minute else 0.0

    return {
        "total_proxies": pool_stats.get("total", 0),
        "healthy": pool_stats.get("healthy", 0),
        "dead": pool_stats.get("dead", 0),
        "req_per_sec": req_per_sec,
        "block_rate": block_rate,
        "avg_latency_ms": avg_latency,
        "total_requests": total_reqs,
        "total_blocks": total_blocks,
    }


# ── Pool ─────────────────────────────────────────────────────


@app.get("/api/pool", dependencies=[Depends(verify_token)])
async def get_pool(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None, pattern="^(healthy|dead|all)$"),
):
    """Proxy list (paginated, filterable)."""
    try:
        params = {"page": page, "per_page": per_page}
        if status:
            params["status"] = status
        resp = await rotation_http.get("/pool/list", params=params)
        return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/pool/flush", dependencies=[Depends(verify_token)])
async def flush_dead():
    """Remove all dead proxies from Redis."""
    dead_set = await redis_client.smembers("gengar:pool:dead")
    if not dead_set:
        return {"status": "ok", "flushed": 0}

    pipe = redis_client.pipeline()
    for addr in dead_set:
        pipe.delete(f"gengar:proxy:{addr}")
        pipe.srem("gengar:pool:index", addr)
    pipe.delete("gengar:pool:dead")
    await pipe.execute()
    return {"status": "ok", "flushed": len(dead_set)}


@app.post("/api/pool/refresh", dependencies=[Depends(verify_token)])
async def refresh_pool():
    """Trigger immediate pool refresh via proxy-scraper."""
    try:
        resp = await httpx.AsyncClient().post(
            "http://proxy-scraper:8002/refresh",
            timeout=120,
        )
        return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── Requests ─────────────────────────────────────────────────


@app.get("/api/requests", dependencies=[Depends(verify_token)])
async def get_requests(count: int = Query(100, ge=1, le=500)):
    """Recent request log."""
    raw = await redis_client.lrange("gengar:request_log", 0, count - 1)
    return {"requests": [json.loads(r) for r in raw] if raw else []}


# ── Rotation Rules ───────────────────────────────────────────


@app.get("/api/rotation-rules", dependencies=[Depends(verify_token)])
async def get_rotation_rules():
    """Current strategy config."""
    try:
        resp = await rotation_http.get("/strategy")
        return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/rotation-rules", dependencies=[Depends(verify_token)])
async def update_rotation_rules(body: RotationRulesUpdate):
    """Update strategy + params."""
    try:
        resp = await rotation_http.post(
            "/strategy",
            json=body.model_dump(exclude_none=True),
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ── Domain Overrides ─────────────────────────────────────────


@app.get("/api/domain-overrides", dependencies=[Depends(verify_token)])
async def get_domain_overrides():
    """Per-domain rule list."""
    raw = await redis_client.hgetall("gengar:domain_overrides")
    overrides = []
    for domain, config_str in (raw or {}).items():
        config = json.loads(config_str)
        config["domain"] = domain
        overrides.append(config)
    return {"overrides": overrides}


@app.post("/api/domain-overrides", dependencies=[Depends(verify_token)])
async def add_domain_override(body: DomainOverride):
    """Add domain override."""
    config = {"strategy": body.strategy}
    if body.country:
        config["country"] = body.country
    await redis_client.hset("gengar:domain_overrides", body.domain, json.dumps(config))
    return {"status": "added", "domain": body.domain}


@app.delete("/api/domain-overrides/{domain}", dependencies=[Depends(verify_token)])
async def delete_domain_override(domain: str):
    """Remove override."""
    await redis_client.hdel("gengar:domain_overrides", domain)
    return {"status": "deleted", "domain": domain}


# ── WebSocket: Live Traffic Stream ───────────────────────────


@app.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    """Stream live request events to the UI via Redis pub/sub."""
    await ws.accept()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("gengar:live_requests")

    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if message and message["type"] == "message":
                await ws.send_text(message["data"])
            else:
                # Send a ping every second to keep the connection alive
                await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug(json.dumps({"event": "ws_error", "error": str(exc)}))
    finally:
        await pubsub.unsubscribe("gengar:live_requests")
        await pubsub.aclose()
