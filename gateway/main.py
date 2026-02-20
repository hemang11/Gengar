"""
Gengar — Gateway: asyncio HTTP Proxy Listener.

Listens on port 8080 as an HTTP forward proxy. Supports both regular
HTTP methods and HTTP CONNECT tunneling. Handles up to 200 concurrent
connections with graceful shutdown on SIGTERM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time


import httpx
import redis.asyncio as aioredis

from handler import (
    handle_http_request,
    get_next_proxy,
    log_request_to_redis,
    mark_proxy_blocked,
)

# ── Logging ──────────────────────────────────────────────────

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("gateway")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "service": "gateway",
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            log_obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


for h in logging.root.handlers:
    h.setFormatter(JsonFormatter())


# ── Config ───────────────────────────────────────────────────

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 6969
MAX_CONNECTIONS = 200
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# ── Connection semaphore ─────────────────────────────────────

connection_semaphore = asyncio.Semaphore(MAX_CONNECTIONS)
redis_client: aioredis.Redis | None = None
rotation_client: httpx.AsyncClient | None = None
active_connections = 0
shutting_down = False


async def parse_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict, bytes]:
    """Parse an HTTP request from the stream.

    Returns (method, url, headers, body).
    """
    # Read the request line
    request_line = await reader.readline()
    if not request_line:
        raise ConnectionError("Empty request")

    request_line_str = request_line.decode("utf-8", errors="ignore").strip()
    parts = request_line_str.split(" ", 2)
    if len(parts) < 2:
        raise ValueError(f"Malformed request line: {request_line_str}")

    method = parts[0].upper()
    url = parts[1]
    # parts[2] is HTTP version, we don't need it

    # Read headers
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line or line == b"\r\n" or line == b"\n":
            break
        line_str = line.decode("utf-8", errors="ignore").strip()
        if ":" in line_str:
            key, value = line_str.split(":", 1)
            headers[key.strip().lower()] = value.strip()

    # Read body if content-length present
    body = b""
    content_length = headers.get("content-length")
    if content_length:
        try:
            body = await reader.readexactly(int(content_length))
        except Exception:
            pass

    return method, url, headers, body


async def handle_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    host: str,
    port: int,
) -> None:
    """Handle HTTP CONNECT method for tunneling (HTTPS proxying)."""
    for attempt in range(1, 4):
        try:
            proxy_info = await get_next_proxy(rotation_client, target_domain=host)
            if not proxy_info:
                break

            proxy_ip = proxy_info["ip"]
            proxy_port = proxy_info["port"]

            # Connect to the upstream proxy
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(proxy_ip, proxy_port),
                    timeout=10,
                )
            except Exception:
                await mark_proxy_blocked(rotation_client, proxy_ip, proxy_port, host)
                continue

            # Send CONNECT to upstream proxy
            upstream_writer.write(
                f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
            )
            await upstream_writer.drain()

            # Read upstream proxy response
            try:
                resp_line = await asyncio.wait_for(
                    upstream_reader.readline(), timeout=10
                )
                resp_str = resp_line.decode("utf-8", errors="ignore").strip()

                # Read until end of headers
                while True:
                    line = await asyncio.wait_for(
                        upstream_reader.readline(), timeout=10
                    )
                    if line == b"\r\n" or line == b"\n" or not line:
                        break
            except Exception:
                upstream_writer.close()
                await mark_proxy_blocked(rotation_client, proxy_ip, proxy_port, host)
                continue

            if "200" in resp_str:
                # Log the CONNECT request
                log_entry = {
                    "ts": time.time(),
                    "method": "CONNECT",
                    "url": f"{host}:{port}",
                    "target_domain": host,
                    "proxy_ip": f"{proxy_ip}:{proxy_port}",
                    "status": 200,
                    "latency_ms": 0,
                    "blocked": False,
                    "attempt": attempt,
                    "strategy": proxy_info.get("strategy", "unknown"),
                }
                asyncio.create_task(log_request_to_redis(redis_client, log_entry))

                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

                # Bidirectional relay
                await asyncio.gather(
                    _relay(reader, upstream_writer),
                    _relay(upstream_reader, writer),
                    return_exceptions=True,
                )
                try:
                    upstream_writer.close()
                except Exception:
                    pass
                return
            else:
                upstream_writer.close()
                await mark_proxy_blocked(rotation_client, proxy_ip, proxy_port, host)
                continue

        except Exception as exc:
            logger.info(
                json.dumps(
                    {
                        "event": "connect_error",
                        "proxy": f"{proxy_ip}:{proxy_port}",
                        "error": str(exc),
                        "attempt": attempt,
                    }
                )
            )
            continue

    try:
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await writer.drain()
    except Exception:
        pass


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Relay data between two streams."""
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), timeout=300)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Handle a single client connection."""
    global active_connections

    async with connection_semaphore:
        active_connections += 1
        try:
            method, url, headers, body = await asyncio.wait_for(
                parse_http_request(reader),
                timeout=30,
            )

            if method == "CONNECT":
                # CONNECT host:port
                if ":" in url:
                    host, port_str = url.rsplit(":", 1)
                    port = int(port_str)
                else:
                    host = url
                    port = 443
                await handle_connect(reader, writer, host, port)
                return

            # Health check endpoint
            if url == "/health" or url.endswith("/health"):
                response = json.dumps(
                    {
                        "status": "ok",
                        "service": "gateway",
                        "active_connections": active_connections,
                    }
                )
                writer.write(
                    f"HTTP/1.1 200 OK\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(response)}\r\n"
                    f"\r\n"
                    f"{response}".encode()
                )
                await writer.drain()
                return

            # Regular HTTP proxy request
            status_code, resp_headers, resp_body = await handle_http_request(
                method=method,
                url=url,
                headers=headers,
                body=body if body else None,
                rotation_client=rotation_client,
                redis=redis_client,
            )

            # Build response
            status_text = _status_text(status_code)
            header_lines = "".join(
                f"{k}: {v}\r\n"
                for k, v in resp_headers.items()
                if k.lower() not in ("transfer-encoding", "connection")
            )
            response_line = f"HTTP/1.1 {status_code} {status_text}\r\n"
            response = (
                f"{response_line}Content-Length: {len(resp_body)}\r\n{header_lines}\r\n"
            ).encode() + resp_body

            writer.write(response)
            await writer.drain()

        except Exception as exc:
            logger.debug(json.dumps({"event": "client_error", "error": str(exc)}))
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            active_connections -= 1
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def _status_text(code: int) -> str:
    texts = {
        200: "OK",
        201: "Created",
        204: "No Content",
        301: "Moved Permanently",
        302: "Found",
        304: "Not Modified",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        407: "Proxy Auth Required",
        429: "Too Many Requests",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
    }
    return texts.get(code, "Unknown")


async def main() -> None:
    global redis_client, rotation_client, shutting_down

    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    rotation_client = httpx.AsyncClient()

    server = await asyncio.start_server(
        handle_client,
        LISTEN_HOST,
        LISTEN_PORT,
    )

    logger.info(
        json.dumps(
            {
                "event": "gateway_started",
                "host": LISTEN_HOST,
                "port": LISTEN_PORT,
                "max_connections": MAX_CONNECTIONS,
            }
        )
    )

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        global shutting_down
        shutting_down = True
        logger.info(json.dumps({"event": "shutdown_signal_received"}))
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    async with server:
        await stop_event.wait()

    # Drain in-flight connections
    logger.info(
        json.dumps(
            {
                "event": "draining",
                "active_connections": active_connections,
            }
        )
    )
    # Wait up to 30 seconds for in-flight connections to finish
    for _ in range(60):
        if active_connections == 0:
            break
        await asyncio.sleep(0.5)

    await rotation_client.aclose()
    await redis_client.aclose()
    logger.info(json.dumps({"event": "gateway_stopped"}))


if __name__ == "__main__":
    asyncio.run(main())
