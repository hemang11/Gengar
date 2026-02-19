# 👻 Gengar

A self-hosted rotating proxy provider named after the ghost Pokémon. Slippery, hard to catch, impossible to pin down.

Gengar handles automatic IP rotation through a pool of ~100 healthy free proxies. Your scrapers point to Gengar's gateway (port 6969), and it takes care of the rest: scraping, health checking, rotation strategies, and block detection.

## 🚀 Quick Start

1. **Clone the repo**
2. **Setup environment**
   ```bash
   cp .env.example .env
   ```
3. **Start with Docker Compose**
   ```bash
   docker compose up -d
   ```
4. **Access the UI**
   Open [http://localhost:3000](http://localhost:3000)

## 🛠 Usage

### Pointing your scraper
Point any HTTP scraper at Gengar on port 6969:
```python
import requests

proxies = {
    'http': 'http://localhost:6969',
    'https': 'http://localhost:6969',
}

# Optional: sticky session
headers = {'X-Session-ID': 'job-123'}

resp = requests.get('http://httpbin.org/ip', proxies=proxies, headers=headers)
print(resp.json())
```

### Rotation Strategies
- **Per-Request (Default):** Randomized proxy for every request.
- **Per-Session:** Sticky proxy mapped to `X-Session-ID`.
- **Time-Based:** Rotates global proxy every N seconds.
- **On-Block:** Rotates only when a block (403/429/pattern) is detected.
- **Round-Robin:** Deterministic cycling through the pool.

## 📦 Services
- **Gateway (6969):** High-performance HTTP/CONNECT proxy.
- **Rotation Engine:** Internal service managing selection logic and Redis pool state.
- **Proxy Scraper:** Periodically fetches proxies from 5 sources + optional Webshare fallback.
- **API Server (8000):** REST API and WebSocket for live traffic metrics.
- **Web UI (3000):** Dark-themed, mitmproxy-inspired dashboard.

## 📋 Config (.env)
| Key | Default | Description |
|---|---|---|
| `ROTATION_STRATEGY` | `per-request` | Global rotation method |
| `API_SECRET` | `changeme` | Token for REST API access |
| `TARGET_POOL_SIZE` | `100` | Target number of healthy proxies |
| `WEBSHARE_ENABLED` | `false` | Enable Webshare free tier fallback |

## 🧪 Development
Run rotation-engine tests:
```bash
docker compose exec rotation-engine python -m pytest test_strategies.py -v
```

---
*Built with ❤️ and asyncio.*
