# Gengar — Self-Hosted Rotating Proxy Provider

## Build Steps (per CLAUDE.md)

- [x] **Step 1**: `docker-compose.yml` + `.env.example` — wire all services, Redis connectivity
- [x] **Step 2**: `pool.py` (rotation-engine) — Redis pool CRUD
- [x] **Step 3**: `strategies.py` (rotation-engine) — 5 rotation strategies + unit tests
- [x] **Step 4**: `proxy-scraper/main.py` — scrape, merge, deduplicate
- [x] **Step 5**: `health_checker.py` — concurrent health checking + scoring
- [x] **Step 6**: `gateway/main.py` + `handler.py` — HTTP proxy + block detection + retries
- [x] **Step 7**: `api-server/main.py` — REST endpoints + WebSocket
- [x] **Step 8**: Web UI — 3 views (LiveTraffic, PoolHealth, RotationRules)
- [x] **Step 9**: `README.md` — full documentation
