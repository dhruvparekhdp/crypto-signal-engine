# System Context & Engine Memory

This document is the definitive technical memory and operational architecture record for the **Crypto Signal Engine** project (`dhruvparekhdp/crypto-signal-engine`).

---

## 1. System Overview & Architecture

The **Crypto Signal Engine** is a high-performance, real-time market intelligence and paper-trading simulator running 24/7. It ingests tick-by-tick and 1-minute OHLC candle data across major cryptocurrency perpetuals and commodities, evaluates setups across 6 independent indicator families with multi-horizon predictive analysis, verifies setups against resting order book walls and AI sentiment, and simulates leveraged executions with trailing stops and laddered profit exits.

```
┌────────────────────────────┐    ┌───────────────────────────────┐    ┌───────────────────────────────┐
│     Live Market Data       │    │      Engine Pipeline          │    │    Execution & Storage        │
├────────────────────────────┤    ├───────────────────────────────┤    ├───────────────────────────────┤
│ • Binance Klines (1m OHLC) │───►│ • Multi-Horizon Trend (15m-1d)│───►│ • Paper Simulator (Leverage) │
│ • CoinDCX REST / Tickers   │    │ • 6 Indicator Confluence Gate │    │ • Trailing Stop & Profit Rung │
│ • CoinGecko Fallback Feed  │    │ • Orderbook Wall Veto (Depth) │    │ • Aiven.io PostgreSQL Cluster │
│ • TwelveData (Gold/Oil)    │    │ • Groq AI Sentinel Review     │    │ • Telegram Instant Alerts     │
│ • CryptoPanic Sentiment    │    │ • Cooldown / Contradiction Clr│    │ • Web Dashboard (Port 8080/80)│
└────────────────────────────┘    └───────────────────────────────┘    └───────────────────────────────┘
```

---

## 2. Infrastructure & Deployment Environment

| Component | Specification | Details |
|---|---|---|
| **Compute Host** | AWS EC2 (`t3.small`, Ubuntu 24.04 LTS) | Hosted at IP `52.62.37.4` |
| **Service Manager** | Linux `systemd` (`crypto-engine.service`) | Auto-restarts on failure, runs under `ubuntu` user |
| **Database** | Aiven.io PostgreSQL (Cloud Cluster) | Managed cloud PostgreSQL with custom SSL certificates |
| **Port Routing** | Port `8080` (native) & Port `80` (iptables) | Inbound rule open for 8080; `iptables` routes 80 → 8080 |
| **CI/CD Pipeline** | GitHub Actions (`.github/workflows/deploy.yml`) | Auto-deploys on `git push origin main` with Telegram notifications |
| **Test Coverage** | `pytest` | **606 passing unit and integration tests** |

---

## 3. Key Architectural Decisions & Pivots

### A. Decoupled Sports Betting Subsystem
- The original build contained sports collectors and Markov-chain models for tennis/football.
- All sports collectors (`Flashscore`, `ESPN`, `Sofascore`, `TheSportsDB`, `OddsApi`, `BetsAPI`, `Sportradar`, `SportsData`, `ApiTennis`) and associated poll routines (~790 lines of dead code) were decoupled from runtime startup.
- CPU and RAM on the EC2 instance are dedicated 100% to crypto order flow, candle processing, and simulator execution.

### B. Database Migration from Neon to Aiven.io
- **Reason**: Neon's free tier introduced quota locks and connection drops that caused downtime.
- **SSL Resolution**: Aiven uses a self-signed root CA for cluster certificates. In `storage/database.py`, `_make_url` configures Python's `asyncpg` with an `ssl.SSLContext` (`ssl.CERT_NONE`, `check_hostname=False`), enabling TLS in transit while avoiding `CERTIFICATE_VERIFY_FAILED` errors.
- **Storage Hygiene**: Snapshot retention is limited to 3 days, and cleanup runs every 6 hours to maintain database footprint under 40 MB permanently (well within Aiven's 1 GB free limit).

### C. Zero-Downtime Admin Settings & Paper Trading Control
- **Database Persistence**: All operational toggles, paper trading levers, and strategy parameters are stored in singleton tables in PostgreSQL (`paper_trading_config` and `strategy_config`).
- **Master Switch**: Added `enabled: Mapped[bool] = mapped_column(Boolean, default=True)` to `PaperTradingConfig`.
- **Dynamic Groq Model Selector**: Replaced `.env`-based AI model selection with an interactive dropdown in the `/settings` UI (`qwen/qwen3.8-27b`, `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, `mixtral-8x7b-32768`, `deepseek-r1-distill-llama-70b`).
- Changes take effect instantly without restarting the service or redeploying code.

### D. Instant Boot Web Server
- `main.py` starts `start_health_server(runner, port=8080)` immediately on process startup within the first 50ms.
- Port 8080 opens and accepts incoming HTTP traffic before background database migrations or historical data checks run, preventing browser timeouts.

---

## 4. Environment Configuration (`.env`)

The system uses an ultra-minimal `.env` configuration file located at `/home/ubuntu/crypto-signal-engine/.env`:

```ini
# Core Web Server
PORT=8080
PYTHONUNBUFFERED=1

# PostgreSQL Database (Aiven.io)
DATABASE_URL=postgresql://avnadmin:PASSWORD@HOST:PORT/defaultdb?sslmode=require

# Web Dashboard Admin Password (used to unlock /settings and execute manual trades)
ADMIN_PASSWORD=your_secure_password_here

# Telegram Notifications (@BotFather & @userinfobot)
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# Groq AI Sentinel
GROQ_API_KEY=your_groq_api_key_here

# Optional: Twelve Data Commodities Key (Gold: XAU/USD, Silver: XAG/USD, Crude Oil: WTI/USD)
TWELVEDATA_API_KEY=
```

---

## 5. Operational Runbook

### Service Management
```bash
# Check status
sudo systemctl status crypto-engine

# Restart service
sudo systemctl restart crypto-engine

# Stop service
sudo systemctl stop crypto-engine

# Stream live system logs
sudo journalctl -u crypto-engine -f

# View last 50 log lines
sudo journalctl -u crypto-engine -n 50 --no-pager
```

### Database Backup & Restore
```bash
# Create automated backup
.venv/bin/python scripts/backup_neon.py

# Restore from backup (.zip, .json.gz, or .json)
.venv/bin/python scripts/restore_from_backup.py --backup backups/neon_backup_latest.json.gz
```

### Port 80 Redirection
```bash
sudo iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8080
```

### Running Test Suite
```bash
.venv/bin/pytest tests/
```

---

## 6. Web Dashboard Endpoints

- **`/`**: Real-time multi-coin dashboard, conviction scores, open paper positions, and live charts.
- **`/settings`**: Admin control panel (password protected). Toggle Paper Trading simulator, choose Groq AI model, configure leverage and risk, and adjust coin watchlist.
- **`/health`**: Health status check returning system uptime, active jobs, and memory stats.
- **`/data`**: Historical trade explorer and market data review.
- **`/predict`**: Multi-horizon forecast engine and predictive analytics.
- **`/audit`**: Performance audit, method catalogue, and win-rate attribution.
