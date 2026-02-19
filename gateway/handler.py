"""
Gengar — Gateway: Request Handler + Block Detection.

Routes proxied HTTP requests through a selected proxy from the rotation
engine, detects blocks (403/429/503, body patterns), retries on failure,
and logs all requests to Redis for the live traffic feed.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

import httpx

logger = logging.getLogger("gateway")

# ── Block Detection ──────────────────────────────────────────

BLOCK_STATUS_CODES = {403, 429, 503, 407}

BLOCK_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"cloudflare",
        r"captcha",
        r"access denied",
        r"blocked",
        r"unusual traffic",
        r"rate limit",
        r"banned",
        r"forbidden",
    ]
]

CHALLENGE_URL_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"/cdn-cgi/challenge",
        r"/challenge",
        r"captcha",
        r"recaptcha",
    ]
]

MAX_RETRIES = 3


def is_blocked(status_code: int, body: str, redirect_url: str | None = None) -> bool:
    """Determine if a response indicates the proxy was blocked."""
    if status_code in BLOCK_STATUS_CODES:
        return True
    for pattern in BLOCK_PATTERNS:
        if pattern.search(body[:5000]):  # Only check first 5KB
            return True
    if redirect_url:
        for pattern in CHALLENGE_URL_PATTERNS:
            if pattern.search(redirect_url):
                return True
    return False


# ── Request Handling ─────────────────────────────────────────


async def get_next_proxy(
    rotation_client: httpx.AsyncClient,
    session_id: str | None = None,
    target_domain: str | None = None,
) -> dict | None:
    """Ask the rotation-engine for the next proxy."""
    try:
        resp = await rotation_client.post(
            "http://rotation-engine:8001/next-proxy",
            json={
                "session_id": session_id,
                "target_domain": target_domain,
            },
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as exc:
        logger.error(json.dumps({"event": "rotation_engine_error", "error": str(exc)}))
        return None


async def mark_proxy_blocked(
    rotation_client: httpx.AsyncClient,
    ip: str,
    port: int,
    target_domain: str | None = None,
) -> None:
    """Tell the rotation-engine a proxy was blocked."""
    try:
        await rotation_client.post(
            "http://rotation-engine:8001/mark-block",
            json={"ip": ip, "port": port, "target_domain": target_domain},
            timeout=5,
        )
    except Exception:
        pass


async def log_request_to_redis(redis, entry: dict) -> None:
    """Push a request log entry to Redis and publish for WebSocket."""
    await redis.lpush("gengar:request_log", json.dumps(entry))
    await redis.ltrim("gengar:request_log", 0, 499)
    await redis.publish("gengar:live_requests", json.dumps(entry))


async def handle_http_request(
    method: str,
    url: str,
    headers: dict,
    body: bytes | None,
    rotation_client: httpx.AsyncClient,
    redis,
) -> tuple[int, dict, bytes]:
    """
    Handle an incoming HTTP request by proxying it through a selected proxy.
    Returns (status_code, response_headers, response_body).
    """
    session_id = headers.get("x-session-id")
    target_domain = _extract_domain(url)
    strategy_used = "unknown"

    for attempt in range(1, MAX_RETRIES + 1):
        proxy_info = await get_next_proxy(rotation_client, session_id, target_domain)
        if not proxy_info:
            return 502, {}, b'{"error": "no healthy proxies available"}'

        proxy_ip = proxy_info["ip"]
        proxy_port = proxy_info["port"]
        proxy_url = f"http://{proxy_ip}:{proxy_port}"
        strategy_used = proxy_info.get("strategy", "unknown")

        start = time.monotonic()
        status_code = 502
        resp_headers: dict = {}
        resp_body = b""
        blocked = False
        error_msg = ""

        try:
            async with httpx.AsyncClient(
                proxy=proxy_url,
                timeout=30,
                follow_redirects=False,
            ) as client:
                # Build the request
                req_headers = {
                    k: v
                    for k, v in headers.items()
                    if k.lower()
                    not in (
                        "host",
                        "proxy-authorization",
                        "proxy-connection",
                        "x-session-id",
                    )
                }
                resp = await client.request(
                    method=method,
                    url=url,
                    headers=req_headers,
                    content=body,
                )
                status_code = resp.status_code
                resp_headers = dict(resp.headers)
                resp_body = resp.content

                # Check for redirect to challenge
                redirect_url = resp_headers.get("location")

                body_text = ""
                try:
                    body_text = resp_body.decode("utf-8", errors="ignore")
                except Exception:
                    pass

                blocked = is_blocked(status_code, body_text, redirect_url)

        except httpx.TimeoutException:
            error_msg = "timeout"
            blocked = True
        except Exception as exc:
            error_msg = str(exc)
            blocked = True

        elapsed_ms = round((time.monotonic() - start) * 1000, 1)

        # Log the request
        log_entry = {
            "ts": time.time(),
            "method": method,
            "url": url,
            "target_domain": target_domain,
            "proxy_ip": f"{proxy_ip}:{proxy_port}",
            "status": status_code,
            "latency_ms": elapsed_ms,
            "blocked": blocked,
            "attempt": attempt,
            "strategy": strategy_used,
            "error": error_msg,
            "response_headers": {k: v for k, v in list(resp_headers.items())[:20]},
        }

        try:
            await log_request_to_redis(redis, log_entry)
        except Exception:
            pass

        if blocked:
            logger.info(
                json.dumps(
                    {
                        "event": "block_detected",
                        "proxy": f"{proxy_ip}:{proxy_port}",
                        "domain": target_domain,
                        "status": status_code,
                        "attempt": attempt,
                    }
                )
            )
            await mark_proxy_blocked(
                rotation_client, proxy_ip, proxy_port, target_domain
            )

            if attempt < MAX_RETRIES:
                continue  # Retry with next proxy
            return status_code, resp_headers, resp_body

        # Success
        return status_code, resp_headers, resp_body

    return 502, {}, b'{"error": "all retries exhausted"}'


def _extract_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        return parsed.hostname or ""
    except Exception:
        return ""
