"""
Gengar — Proxy Scraper: Main Service.

Scrapes proxies from 5 public sources, merges, deduplicates by ip:port,
stores in Redis, and triggers health checks. Runs on startup and every
POOL_REFRESH_INTERVAL seconds.

Also exposes a /health endpoint on port 8002 and an endpoint to trigger
refresh manually.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI

from health_checker import check_all_proxies, run_periodic_checks

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("proxy-scraper")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "service": "proxy-scraper",
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


for h in logging.root.handlers:
    h.setFormatter(JsonFormatter())

# ── Config ───────────────────────────────────────────────────

POOL_REFRESH_INTERVAL = int(os.getenv("POOL_REFRESH_INTERVAL", "1800"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
]

# Webshare fallback (optional)
WEBSHARE_ENABLED = os.getenv("WEBSHARE_ENABLED", "false").lower() == "true"
WEBSHARE_API_KEY = os.getenv("WEBSHARE_API_KEY", "")
MIN_POOL_SIZE = int(os.getenv("MIN_POOL_SIZE", "20"))

# ── Proxy key helpers ────────────────────────────────────────

PROXY_KEY_PREFIX = "gengar:proxy:"
POOL_INDEX_KEY = "gengar:pool:index"

IP_PORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})$")


def parse_proxy_line(line: str, source: str) -> dict | None:
    """Parse a single line like '1.2.3.4:8080' into proxy dict."""
    line = line.strip()
    m = IP_PORT_RE.match(line)
    if not m:
        return None
    ip, port_str = m.group(1), m.group(2)
    port = int(port_str)
    if port < 1 or port > 65535:
        return None
    return {
        "ip": ip,
        "port": port,
        "protocol": "http",
        "country": "",
        "latency_ms": 0,
        "health_score": 0.0,
        "last_checked": 0,
        "source": source,
        "fail_count": 0,
        "success_count": 0,
        "total_checks": 0,
        "consecutive_fails": 0,
        "created_at": time.time(),
    }


async def fetch_source(client: httpx.AsyncClient, url: str) -> list[dict]:
    """Fetch proxies from a single source URL."""
    source_name = url.split("/")[2]  # domain as source identifier
    try:
        resp = await client.get(url, timeout=30)
        resp.raise_for_status()
        text = resp.text
        proxies = []
        for line in text.splitlines():
            p = parse_proxy_line(line, source_name)
            if p:
                proxies.append(p)
        logger.info(
            json.dumps(
                {
                    "event": "source_fetched",
                    "source": source_name,
                    "count": len(proxies),
                }
            )
        )
        return proxies
    except Exception as exc:
        logger.error(
            json.dumps(
                {
                    "event": "source_error",
                    "source": source_name,
                    "error": str(exc),
                }
            )
        )
        return []


async def fetch_webshare_proxies(client: httpx.AsyncClient) -> list[dict]:
    """Fetch proxies from Webshare free tier API (fallback)."""
    if not WEBSHARE_ENABLED or not WEBSHARE_API_KEY:
        return []
    try:
        resp = await client.get(
            "https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=25",
            headers={"Authorization": f"Token {WEBSHARE_API_KEY}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        proxies = []
        for item in data.get("results", []):
            proxies.append(
                {
                    "ip": item["proxy_address"],
                    "port": item["port"],
                    "protocol": "http",
                    "country": item.get("country_code", ""),
                    "latency_ms": 0,
                    "health_score": 50.0,
                    "last_checked": 0,
                    "source": "webshare",
                    "fail_count": 0,
                    "success_count": 0,
                    "total_checks": 0,
                    "consecutive_fails": 0,
                    "created_at": time.time(),
                }
            )
        logger.info(json.dumps({"event": "webshare_fetched", "count": len(proxies)}))
        return proxies
    except Exception as exc:
        logger.error(json.dumps({"event": "webshare_error", "error": str(exc)}))
        return []


async def scrape_and_store(redis: aioredis.Redis) -> dict:
    """Scrape all sources, merge, deduplicate, store in Redis, run health checks."""
    async with httpx.AsyncClient() as client:
        tasks = [fetch_source(client, url) for url in PROXY_SOURCES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    all_proxies: list[dict] = []
    for r in results:
        if isinstance(r, list):
            all_proxies.extend(r)

    # Deduplicate by ip:port (keep first occurrence)
    seen: set[str] = set()
    unique: list[dict] = []
    for p in all_proxies:
        key = f"{p['ip']}:{p['port']}"
        if key not in seen:
            seen.add(key)
            unique.append(p)

    logger.info(
        json.dumps(
            {
                "event": "scrape_complete",
                "total_raw": len(all_proxies),
                "unique": len(unique),
            }
        )
    )

    # Store in Redis
    pipe = redis.pipeline()
    for p in unique:
        addr = f"{p['ip']}:{p['port']}"
        redis_key = f"{PROXY_KEY_PREFIX}{addr}"
        # Only add if not already in pool (preserve existing stats)
        pipe.setnx(redis_key, json.dumps(p))
        pipe.sadd(POOL_INDEX_KEY, addr)
    await pipe.execute()

    # Run health checks on all
    stats = await check_all_proxies(redis)

    # Webshare fallback check
    healthy_count = stats.get("healthy", 0)
    if healthy_count < MIN_POOL_SIZE:
        logger.info(
            json.dumps(
                {
                    "event": "webshare_fallback_triggered",
                    "healthy": healthy_count,
                    "min": MIN_POOL_SIZE,
                }
            )
        )
        async with httpx.AsyncClient() as client:
            ws_proxies = await fetch_webshare_proxies(client)
        if ws_proxies:
            pipe = redis.pipeline()
            for p in ws_proxies:
                addr = f"{p['ip']}:{p['port']}"
                pipe.set(f"{PROXY_KEY_PREFIX}{addr}", json.dumps(p))
                pipe.sadd(POOL_INDEX_KEY, addr)
            await pipe.execute()

    return {
        "scraped": len(unique),
        "healthy": stats.get("healthy", 0),
        "dead": stats.get("dead", 0),
    }


# ── FastAPI app ──────────────────────────────────────────────

redis_client: aioredis.Redis | None = None
refresh_task: asyncio.Task | None = None
health_task: asyncio.Task | None = None


async def _periodic_scrape(redis: aioredis.Redis) -> None:
    """Periodically re-scrape and refresh pool."""
    while True:
        await asyncio.sleep(POOL_REFRESH_INTERVAL)
        try:
            await scrape_and_store(redis)
        except Exception as exc:
            logger.error(
                json.dumps({"event": "periodic_scrape_error", "error": str(exc)})
            )


@asynccontextmanager
async def lifespan(application: FastAPI):
    global redis_client, refresh_task, health_task

    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    logger.info(json.dumps({"event": "startup", "msg": "Proxy scraper started"}))

    # Initial scrape
    try:
        stats = await scrape_and_store(redis_client)
        logger.info(json.dumps({"event": "initial_scrape_done", **stats}))
    except Exception as exc:
        logger.error(json.dumps({"event": "initial_scrape_error", "error": str(exc)}))

    # Background tasks
    refresh_task = asyncio.create_task(_periodic_scrape(redis_client))
    health_task = asyncio.create_task(run_periodic_checks(redis_client))

    yield

    # Shutdown
    logger.info(json.dumps({"event": "shutdown"}))
    if refresh_task:
        refresh_task.cancel()
    if health_task:
        health_task.cancel()
    await redis_client.aclose()


app = FastAPI(title="Gengar Proxy Scraper", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "proxy-scraper"}


@app.post("/refresh")
async def trigger_refresh():
    """Manually trigger a pool refresh."""
    if redis_client is None:
        return {"error": "not ready"}, 503
    stats = await scrape_and_store(redis_client)
    return {"status": "refresh_complete", **stats}
