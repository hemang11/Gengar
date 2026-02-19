Build "Gengar" — a self-hosted rotating proxy provider named after
the ghost Pokémon. Slippery, hard to catch, impossible to pin down.

My scraper points to a single proxy URL and Gengar handles all IP
rotation automatically using a pool of ~100 free proxies.

=== CORE CONCEPT ===
1. On startup, scrape free proxy lists from multiple public sources
2. Health check all scraped proxies concurrently, keep only working ones
3. Maintain a live pool of ~100 healthy proxies in Redis
4. Scraper connects to Gengar on port 6969 (HTTP proxy)
5. Gengar rotates through the pool based on the configured strategy
6. If a proxy returns 403/429 or a block pattern → mark dead, remove it
7. Auto-refresh pool every 30 minutes to replace dead proxies
8. Fallback to Webshare free tier (10 proxies via env config) if pool
   drops below 20 healthy proxies

=== PROXY SOURCES (free, no auth required) ===
Scrape and merge proxies from all of these on startup and every 30min:
- https://api.proxyscrape.com/v2/?request=getproxies&protocol=http
- https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt
- https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt
- https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt
- https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt

Parse all into a unified format:
  { ip, port, protocol, country, latency_ms, health_score, last_checked,
    source, fail_count, success_count }

=== HEALTH CHECKING ===
- On scrape: concurrently test all proxies (max 200 concurrent workers)
- Test method: GET http://httpbin.org/ip via the proxy, timeout 8s
- Pass: response 200 + contains an IP in JSON → mark healthy
- Fail: timeout, error, or wrong response → mark dead, exclude
- Ongoing: re-check every healthy proxy every 10 minutes
- After 3 consecutive failures → permanently remove from pool
- Track latency_ms for every check, use it in scoring
- Health score formula: (success_count / total_checks) * 100

=== ROTATION STRATEGIES ===
All configurable from the UI, stored in Redis:

1. per-request (default): new proxy every single request
2. per-session: sticky proxy per X-Session-ID header, rotate on
   expiry or block. Session TTL configurable (default 5min)
3. time-based: rotate every N seconds regardless of requests
4. on-block: only rotate when a block is detected
5. round-robin: cycle through pool in order, no randomness

=== BLOCK DETECTION ===
After every proxied response check:
- Status codes: 403, 429, 503, 407
- Body patterns (case insensitive): "cloudflare", "captcha",
  "access denied", "blocked", "unusual traffic", "rate limit",
  "banned", "forbidden"
- Redirect to challenge page (detect by URL pattern)

On block detected:
- Increment proxy fail_count
- Remove from active pool immediately
- Retry same request with next healthy proxy (max 3 retries)
- Log block event with target domain + proxy that was blocked

=== FILE STRUCTURE ===
gengar/
├── docker-compose.yml
├── .env.example
├── README.md
├── gateway/
│   ├── Dockerfile
│   ├── main.py           # asyncio HTTP proxy listener
│   ├── handler.py        # request routing + block detection
│   └── requirements.txt
├── rotation-engine/
│   ├── Dockerfile
│   ├── main.py           # FastAPI internal service
│   ├── strategies.py     # all 5 rotation strategy implementations
│   ├── pool.py           # Redis pool CRUD operations
│   └── requirements.txt
├── proxy-scraper/
│   ├── Dockerfile
│   ├── main.py           # scrapes all 5 sources, merges, deduplicates
│   ├── health_checker.py # concurrent health checking worker
│   └── requirements.txt
├── api-server/
│   ├── Dockerfile
│   ├── main.py           # FastAPI REST API + WebSocket
│   └── requirements.txt
└── ui/
    ├── Dockerfile
    ├── package.json
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── views/
        │   ├── LiveTraffic.tsx      # real-time request feed
        │   ├── PoolHealth.tsx       # proxy pool dashboard
        │   └── RotationRules.tsx    # strategy config
        └── components/
            ├── StatCard.tsx
            ├── ProxyTable.tsx
            ├── RequestRow.tsx
            └── StrategyForm.tsx

=== DOCKER COMPOSE ===
Services: gateway, rotation-engine, proxy-scraper, api-server, ui, redis

Exposed to host:
  6969 → HTTP proxy (scraper points here)
  3000 → Web UI
  8000 → API server (for debugging)

Redis is internal only, never exposed.

All services on the same Docker bridge network "gengar-net".
All services depend_on redis with healthcheck.

=== ENV CONFIG (.env.example) ===
# Rotation
ROTATION_STRATEGY=per-request
SESSION_TTL=300
ROTATION_INTERVAL=30

# Pool
MIN_POOL_SIZE=20
TARGET_POOL_SIZE=100
POOL_REFRESH_INTERVAL=1800
HEALTH_CHECK_INTERVAL=600
HEALTH_CHECK_TIMEOUT=8
MAX_CONCURRENT_CHECKS=200

# Fallback (Webshare free tier - optional)
WEBSHARE_ENABLED=false
WEBSHARE_API_KEY=

# API
API_SECRET=changeme

# Logging
LOG_LEVEL=INFO

=== WEB UI — DARK THEME, MITMPROXY-INSPIRED ===
Purple/ghost color accent (#7c3aed) to match Gengar's vibe.
Three views accessible via sidebar tabs:

── VIEW 1: Live Traffic ──────────────────────────────────────
Real-time request table, updated via WebSocket (/ws/live)
Columns: time | target domain | proxy IP | status | latency | blocked
Color rows: green=2xx, yellow=slow(>2s), red=blocked/error
Click any row → slide-in panel with full request details:
  target URL, proxy used, all response headers, retry count, strategy used
Filter bar: by domain, status code, blocked only toggle

── VIEW 2: Pool Health Dashboard ─────────────────────────────
Top stat cards (large numbers):
  Total Proxies | Healthy | Dead | Req/sec | Block Rate % | Avg Latency

Provider breakdown donut chart:
  shows share of requests handled by each proxy source

Proxy table (paginated, 20 per page):
  ip:port | country | source | health score | latency | 
  success rate | last checked | status badge
  
Action buttons:
  "Refresh Pool Now" — triggers immediate re-scrape + health check
  "Flush Dead Proxies" — removes all dead proxies from Redis

── VIEW 3: Rotation Rules ─────────────────────────────────────
Strategy dropdown (per-request / per-session / time-based / 
  on-block / round-robin)
Dynamic config fields based on selected strategy:
  - per-session: Session TTL (seconds) input
  - time-based: Rotation interval (seconds) input
  - others: no extra fields

Per-domain overrides table:
  Add a domain → assign it a specific strategy + optional country filter
  Example: "amazon.com" → on-block strategy, US proxies only
  Deletable rows

Save button → POST to API → applies immediately, shows confirmation toast

=== API ENDPOINTS ===
GET  /health                    → service health
GET  /api/stats                 → aggregate metrics snapshot
GET  /api/pool                  → proxy list (paginated, filterable)
POST /api/pool/flush            → remove dead proxies
POST /api/pool/refresh          → trigger immediate pool refresh
GET  /api/requests              → recent request log (last 100)
GET  /api/rotation-rules        → current strategy config
POST /api/rotation-rules        → update strategy + params
GET  /api/domain-overrides      → per-domain rule list
POST /api/domain-overrides      → add domain override
DELETE /api/domain-overrides/{domain} → remove override
WS   /ws/live                   → streams live request events to UI

All non-health endpoints require Authorization: Bearer {API_SECRET}

=== CODING RULES ===
- Full production-ready code. Zero TODOs, zero placeholders.
- Async Python everywhere: asyncio, httpx, aioredis
- Structured JSON logging on all services (stdout)
- Every service exposes GET /health
- Graceful shutdown on SIGTERM (drain in-flight requests)
- Gateway must handle 200 concurrent proxy connections
- Proxy scraper must deduplicate across all 5 sources by ip:port
- Health checker must never test more than MAX_CONCURRENT_CHECKS at once
- Unit tests for all 5 rotation strategy implementations in strategies.py
- README.md at root with: setup steps, how to configure .env,
  how to point your scraper at Gengar, screenshot description of UI

=== BUILD ORDER ===
Build and verify each step before moving to the next:

Step 1: docker-compose.yml + .env.example
        Wire all services, verify they all start and redis connects

Step 2: pool.py (rotation-engine)
        Redis pool CRUD: add, remove, get_healthy, mark_dead, score

Step 3: strategies.py (rotation-engine)
        Implement all 5 strategies with unit tests

Step 4: proxy-scraper/main.py
        Scrape all 5 sources, merge, deduplicate, store in Redis pool

Step 5: health_checker.py
        Concurrent health checking, scoring, dead proxy removal

Step 6: gateway/main.py + handler.py
        HTTP proxy listener (port 6969), calls rotation-engine for IP assignment,
        block detection, retry logic

Step 7: api-server/main.py
        All REST endpoints + WebSocket live stream

Step 8: Web UI
        Build all 3 views against the real API server

Step 9: README.md
        Full setup and usage documentation

After each step tell me what was built and what to verify before
continuing. Ask if anything in the spec is unclear.