# Gengar — Self-Hosted Rotating Proxy Provider

Build a self-hosted rotating proxy provider with 6 Docker services: gateway (HTTP proxy on 8080), rotation-engine (FastAPI), proxy-scraper, api-server (FastAPI + WebSocket on 8000), React UI (on 3000), and Redis. All async Python (asyncio, httpx, aioredis). Full production code — no TODOs or placeholders.

## User Review Required

> [!IMPORTANT]
> This is a large project with 9 sequential build steps. I will implement each step fully, then report what was built so you can verify before I continue — as requested in the spec.

> [!NOTE]
> The spec references Webshare free tier as a fallback. I'll implement env-based config for it, but actual Webshare integration requires an API key you'd supply later.

---

## Proposed Changes

### Step 1 — Docker Infrastructure

#### [NEW] [docker-compose.yml](file:///Users/hshrimali-mbp/Desktop/Gengar/docker-compose.yml)
- 6 services: `gateway`, `rotation-engine`, `proxy-scraper`, `api-server`, `ui`, `redis`
- All on `gengar-net` bridge network; Redis internal only
- Ports: 8080 (gateway), 3000 (ui), 8000 (api-server)
- All depend on `redis` with healthcheck

#### [NEW] [.env.example](file:///Users/hshrimali-mbp/Desktop/Gengar/.env.example)
- All env vars from spec (rotation, pool, webshare, API, logging)

---

### Step 2 — Rotation Engine: Pool (Redis CRUD)

#### [NEW] [rotation-engine/pool.py](file:///Users/hshrimali-mbp/Desktop/Gengar/rotation-engine/pool.py)
- `add_proxy`, `remove_proxy`, `get_healthy_proxies`, `mark_dead`, `update_score`
- Proxy stored as JSON hash in Redis keyed by `ip:port`
- Health score formula: `(success_count / total_checks) * 100`

#### [NEW] [rotation-engine/main.py](file:///Users/hshrimali-mbp/Desktop/Gengar/rotation-engine/main.py)
- FastAPI internal service exposing pool + strategy endpoints
- `/health`, `/next-proxy`, `/mark-block`, `/strategy`, etc.

#### [NEW] [rotation-engine/Dockerfile](file:///Users/hshrimali-mbp/Desktop/Gengar/rotation-engine/Dockerfile)
#### [NEW] [rotation-engine/requirements.txt](file:///Users/hshrimali-mbp/Desktop/Gengar/rotation-engine/requirements.txt)

---

### Step 3 — Rotation Engine: Strategies

#### [NEW] [rotation-engine/strategies.py](file:///Users/hshrimali-mbp/Desktop/Gengar/rotation-engine/strategies.py)
- 5 strategies: `per-request`, `per-session`, `time-based`, `on-block`, `round-robin`
- Each is a class implementing `async select(pool, context) -> Proxy`
- Per-session uses `X-Session-ID` header, TTL in Redis
- Time-based tracks last rotation timestamp

#### [NEW] [rotation-engine/test_strategies.py](file:///Users/hshrimali-mbp/Desktop/Gengar/rotation-engine/test_strategies.py)
- Unit tests for all 5 strategies using pytest + pytest-asyncio
- Mock Redis pool, verify correct proxy selection behavior

---

### Step 4 — Proxy Scraper

#### [NEW] [proxy-scraper/main.py](file:///Users/hshrimali-mbp/Desktop/Gengar/proxy-scraper/main.py)
- Scrapes all 5 sources concurrently using httpx
- Parses into unified `{ ip, port, protocol, ... }` format
- Deduplicates by `ip:port` across all sources
- Stores merged list in Redis, triggers health check
- Runs on startup + every `POOL_REFRESH_INTERVAL` (30min)

#### [NEW] [proxy-scraper/Dockerfile](file:///Users/hshrimali-mbp/Desktop/Gengar/proxy-scraper/Dockerfile)
#### [NEW] [proxy-scraper/requirements.txt](file:///Users/hshrimali-mbp/Desktop/Gengar/proxy-scraper/requirements.txt)

---

### Step 5 — Health Checker

#### [NEW] [proxy-scraper/health_checker.py](file:///Users/hshrimali-mbp/Desktop/Gengar/proxy-scraper/health_checker.py)
- Semaphore-limited concurrent checking (`MAX_CONCURRENT_CHECKS`)
- Test: `GET http://httpbin.org/ip` via proxy, 8s timeout
- Pass → update latency + increment success_count
- Fail → increment fail_count; 3 consecutive failures → permanent removal
- Re-check healthy proxies every `HEALTH_CHECK_INTERVAL`

---

### Step 6 — Gateway (HTTP Proxy)

#### [NEW] [gateway/main.py](file:///Users/hshrimali-mbp/Desktop/Gengar/gateway/main.py)
- asyncio TCP-level HTTP proxy listener on port 8080
- Handles both HTTP CONNECT (tunneling) and regular HTTP methods
- Supports 200 concurrent connections

#### [NEW] [gateway/handler.py](file:///Users/hshrimali-mbp/Desktop/Gengar/gateway/handler.py)
- Calls rotation-engine `/next-proxy` for proxy assignment
- Block detection: status codes (403, 429, 503, 407) + body pattern matching
- On block: mark dead, retry with next proxy (max 3 retries)
- Logs all requests to Redis for live traffic stream

#### [NEW] [gateway/Dockerfile](file:///Users/hshrimali-mbp/Desktop/Gengar/gateway/Dockerfile)
#### [NEW] [gateway/requirements.txt](file:///Users/hshrimali-mbp/Desktop/Gengar/gateway/requirements.txt)

---

### Step 7 — API Server

#### [NEW] [api-server/main.py](file:///Users/hshrimali-mbp/Desktop/Gengar/api-server/main.py)
- FastAPI with all endpoints from spec (health, stats, pool, requests, rotation-rules, domain-overrides)
- WebSocket `/ws/live` streaming live request events from Redis pub/sub
- Bearer token auth via `API_SECRET`
- CORS enabled for UI

#### [NEW] [api-server/Dockerfile](file:///Users/hshrimali-mbp/Desktop/Gengar/api-server/Dockerfile)
#### [NEW] [api-server/requirements.txt](file:///Users/hshrimali-mbp/Desktop/Gengar/api-server/requirements.txt)

---

### Step 8 — Web UI (React + Vite)

#### [NEW] [ui/src/App.tsx](file:///Users/hshrimali-mbp/Desktop/Gengar/ui/src/App.tsx)
- Dark theme, purple accent `#7c3aed`, mitmproxy-inspired
- Sidebar with 3 tabs

#### [NEW] [ui/src/views/LiveTraffic.tsx](file:///Users/hshrimali-mbp/Desktop/Gengar/ui/src/views/LiveTraffic.tsx)
- Real-time WebSocket feed, color-coded rows, click-to-expand, filter bar

#### [NEW] [ui/src/views/PoolHealth.tsx](file:///Users/hshrimali-mbp/Desktop/Gengar/ui/src/views/PoolHealth.tsx)
- Stat cards, donut chart, paginated proxy table, action buttons

#### [NEW] [ui/src/views/RotationRules.tsx](file:///Users/hshrimali-mbp/Desktop/Gengar/ui/src/views/RotationRules.tsx)
- Strategy dropdown, dynamic config fields, domain overrides table

#### [NEW] [ui/src/components/](file:///Users/hshrimali-mbp/Desktop/Gengar/ui/src/components/)
- `StatCard.tsx`, `ProxyTable.tsx`, `RequestRow.tsx`, `StrategyForm.tsx`

#### [NEW] [ui/Dockerfile](file:///Users/hshrimali-mbp/Desktop/Gengar/ui/Dockerfile)
#### [NEW] [ui/package.json](file:///Users/hshrimali-mbp/Desktop/Gengar/ui/package.json)

---

### Step 9 — Documentation

#### [NEW] [README.md](file:///Users/hshrimali-mbp/Desktop/Gengar/README.md)
- Setup steps, .env configuration, how to point your scraper at Gengar, UI screenshot descriptions

---

## Verification Plan

### Automated Tests
- **Unit tests**: `cd rotation-engine && python -m pytest test_strategies.py -v` — tests all 5 rotation strategies
- **Docker build**: `docker compose build` — all services build without errors
- **Docker startup**: `docker compose up -d` — all 6 containers reach healthy state
- **Health endpoints**: `curl http://localhost:8000/health` and `curl http://localhost:8080/health` — return 200

### Manual Verification
1. **Redis connectivity**: `docker compose exec redis redis-cli PING` should return `PONG`
2. **Proxy scraping**: Watch `docker compose logs proxy-scraper` for successful scrape output
3. **Pool population**: `curl -H "Authorization: Bearer changeme" http://localhost:8000/api/pool` returns proxies
4. **UI loads**: Open `http://localhost:3000` in browser — dark-themed UI with 3 views
5. **Live proxy test**: Configure a simple scraper to use `http://localhost:8080` as proxy, verify requests are proxied
