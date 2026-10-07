# Portal revamp: "Brutal Command"

The single place for everything decided about the portal redesign: why it changed, what it looks like, where
each old page went, how the code is laid out, and what is left to do. Read this before touching
`scheduler/portal.py` or `scheduler/portal/`.

Status (7 Oct 2026): **v1 shipped on branch `claude/portal-design-revamp-l69rn1`.** `/` now opens the new portal; the
old one-page dashboard is unchanged at `/classic`.

---

## 1. How we got here

| Step | What happened |
|---|---|
| Brief | "Revamp the entire portal, revamp all pages and redirection, combine some pages. Hacking-and-trading vibe with live animation, like in movies and web series." Three candidate directions: Cyber-Brutalism, Neon-Noir Terminal, Agentic Command Center. |
| Round 1 | Three live one-page demos (design canvas, boards A, B and C) plus a page map turning 11 screens into 4 hubs. |
| Feedback 1 | Loved **A · Cyber-Brutalism** (but its orange was too bright) and **C · Agentic Command Center**. Asked for a mix. |
| Round 2 | A's orange swapped for violet. New board **D · Brutal Command** = A's structure + C's agent pipeline + movie-style motion. |
| Feedback 2 | D chosen, but the neon lime was too flashy: wanted a softer, user-friendly, creative palette that people like on first sight. |
| Round 3 | Four palettes for D: Champagne & Teal, **Sage & Lilac**, Ice & Peach, Classic lime. |
| Decision | **"Option 2 and all the 4 shades"**: Sage & Lilac is the default, and all four stay as a theme switcher in the header. Build it into the real portal. |

Design boards (live demos with simulated data) are kept in `design/revamp/`:

| File | Board |
|---|---|
| `Main.dc.html` | Page map: 11 screens → 4 hubs, redirect table |
| `CyberBrutal.dc.html` | A · Cyber-Brutalism (paper ground, violet instead of orange) |
| `NeonNoir.dc.html` | B · Neon-Noir Terminal (not chosen, kept for reference) |
| `CommandCenter.dc.html` | C · Agentic Command Center |
| `BrutalCommand.dc.html` | **D · Brutal Command, the chosen direction** (palette tweak, Sage & Lilac default) |
| `canvas.json` | Canvas layout for the boards above |

They open in the Design canvas artifact; the `.dc.html` format needs that runtime, they are not served by the engine.

---

## 2. The look

**Brutal Command**: brutalist structure on a black terminal ground, with an agentic pipeline and film-style motion.

- **Structure**: 3 px off-white borders (`#E8E6DF`), hard offset shadows in the accent colour (8 px 8 px 0), square corners,
  thick filled buttons that lift on hover and press on click.
- **Ground**: `#0A0A0A` with a faint 40 px grid; panels `#0F100E`; trace and stream panels `#050505`.
- **Type**: Archivo 900 for headings and big numbers, JetBrains Mono for data, labels and the terminal trace
  (Google Fonts, loaded in `portal.html`).
- **Colour roles** (CSS variables in `portal.css`):
  - `--acc`: brand accent (logo block, ticker band, active tab, shadows, active agent, radar, filter gauge when open)
  - `--up`: price up, long, profit, traded
  - `--dn`: price down, short, loss, skipped/blocked
  - `--ink` `#E8E6DF`, `--dim` `#A3A39A`, `--on-acc` `#0A0A0A` (text on accent fills)

### Palettes

| Key | Name | `--acc` | `--up` | `--dn` | Notes |
|---|---|---|---|---|---|
| `sage` | **Sage & Lilac (default)** | `#A9D3B6` | `#A9D3B6` | `#B9AAF2` | Calmest; the owner's pick |
| `champagne` | Champagne & Teal | `#E3C58D` | `#6CC9AE` | `#A99BF0` | Warm gold brand, teal/lilac market colours |
| `ice` | Ice & Peach | `#94C5F5` | `#94C5F5` | `#EDA982` | Blue/orange: easiest for colour-blind users |
| `lime` | Classic lime | `#C6F432` | `#C6F432` | `#9B8CFF` | The original neon, kept for comparison |

The swatches in the header set `data-palette` on `<html>` and save it in `localStorage` (`portal-palette`), so the
choice sticks per browser. An inline script in `<head>` applies it before first paint (no flash).
To add a palette: one CSS rule in `portal.css` and one swatch button in `portal.html`.

### Motion (decoration only, never data)

| Effect | Where |
|---|---|
| Glitching hub name (violet/accent slices every ~3 s) | Logo |
| Scanline sweeping down the screen | Whole page |
| Scrolling price ticker | Top band |
| Agent pipeline: a "working" card hands off every 1.6 s, striped progress bar | Command |
| Radar sweep; each coin's dot flashes as the beam passes | Command |
| Decision trace types itself out like a terminal | Command |
| Market stream: real coin snapshot scrolling in two columns | Command |
| New feed rows slide in; full-width "SIGNAL LOCKED / FILTER BLOCK / NOT TRADED" toast on a new signal | Command |
| Animated bars (R progress, gauge, budgets) | All hubs |

`prefers-reduced-motion` turns all of it off.

**Rule: nothing on the real portal is simulated.** The design boards use a fake feed; the portal shows only engine
data. Motion that is not data (the pipeline hand-off cycle, sweeps, glitches) never carries a number.

---

## 3. Information architecture: 11 screens → 4 hubs

| Hub | Route | Question it answers | Absorbs |
|---|---|---|---|
| 01 Command | `/command` | What is the market doing and what did the engine just decide? | Dashboard, Signals, Watchlist (prices) |
| 02 Book | `/book` | What am I holding and how is each trade doing? | Paper Trading (swing book) |
| 03 Evidence | `/evidence` | Can I trust these strategies? | Accuracy, Historic Data, Explorer, Lab results (links to deep tools) |
| 04 System | `/system` | Is the machine healthy? | Diagnostics, Guard, Settings (links), AI budget |

**Always visible on every hub** (status strip): market filter open/closed, open swing trades, worst case if every
stop hits (open trades × risk per trade), feed freshness, and "LIVE TRADING: OFF · PAPER ONLY" (from settings).
The header also carries the UTC clock, a countdown to the next 4h bar close, and the theme switcher.

### What each hub shows (v1)

**Command**
- Agent pipeline: 8 cards (Market feed → Detectors → Market filter → Swing scanner → Risk sizer → Paper exec → Outcome
  replay → AI analyst), each with a live metric. Market filter turns "BLOCKING" when the filter is closed.
- Chart: 4h candles for the selected coin + a forming candle from the live price, Keltner channel (EMA 20, ATR 10,
  k 2.5) computed in the browser. Coin buttons switch the chart.
- Market radar (24h change per coin) and the market-filter gauge (BTC 30-day vol percentile vs threshold).
- Decision trace for the newest swing signal: scanner → filter → risk → exec → replay.
- Market stream (price, 24h change, RSI, volume ratio), signal feed (last 14 days), Ask the desk (4 questions answered
  from the data on the page).

**Book**: open swing positions in R on a −1R…+3R bar with stop, target, entry, mark and time left; closed swing
trades (win rate, avg R, total R, last 15); taken vs skipped by the market filter.

**Evidence**: expected numbers from the 5-year backtest; live results per strategy (trades, wins, avg R, total R);
armed strategies; links to Audit, Accuracy, Chart, Historic data, Market moves, Price outlook, Pipeline, v2 shadow,
Journal, Mirror.

**System**: last swing scan per coin@timeframe; AI budget per model (tokens or calls vs daily limit, cooldowns);
engine facts (uptime, symbols, paper/swing/filter/live modes, wallet, excluded coins); links to Settings, Keys,
Watchlist, Session guard, Simulator, Data, API list, Diagnostics, Classic dashboard.

### Redirects and old links

| Old address | Now |
|---|---|
| `/` | 302 → `/command` |
| `/#dashboard`, `/#paper`, `/#crypto`, `/#mirror`, `/#guard`, `/#accuracy`, `/#historic`, `/#watchlist`, `/#simulator` | The browser keeps the hash through the redirect; the portal script sends any of these to `/classic#…`, so every old bookmark still lands on the same view |
| Old one-page dashboard | `/classic` (unchanged; its page-group strip now points at `/classic#…`) |
| `/audit` `/chart` `/moves` `/predict` `/pipeline` `/v2` `/journal` `/data` `/settings` `/keys` `/api-docs` | Unchanged, linked from Evidence or System. Their "← Dashboard" links go to `/`, i.e. the new portal |

Phase 2 (not done): rebuild those deep pages in the new style inside their hub, then turn their routes into
redirects to `/<hub>?tab=…`.

---

## 4. Code

```
scheduler/portal.py            routes + 3 small APIs, registered from health.make_app
scheduler/portal/portal.html   page shell, all four hubs (sections toggled by path)
scheduler/portal/portal.css    palettes, layout, motion
scheduler/portal/portal.js     polling, rendering, routing (no framework, no build)
tests/test_portal.py           routes, redirect, asset whitelist, candles, signals
design/revamp/                 the design boards
```

### Routes (`scheduler/portal.py`)

| Route | What |
|---|---|
| `GET /` | 302 → `/command` |
| `GET /command`, `/book`, `/evidence`, `/system` | The same HTML shell; the script picks the hub from the path and uses `history.pushState` to switch |
| `GET /portal/static/{name}` | `portal.css` / `portal.js` only (fixed whitelist, anything else is 404) |
| `GET /api/portal/candles?symbol=&tf=4h&limit=48` | Closed bars via `analysis.swing_book.fetch_bars` (the same Binance futures klines call the swing scan uses), cached 60 s per symbol+tf; bad input → 400, exchange error → 502 |
| `GET /api/portal/signals?days=14&limit=40` | Swing signals newest first with `status` = TRADED / NOT TRADED / FILTER BLOCK (same rule as the classic Signals tab: no skip reason = traded), the human skip reason, and the regime tag (`btc_vol_rank`, `filter_skip`) |
| `GET /api/portal/meta` | `live_trading_mode`, `paper_trading_enabled`, `swing_enabled`, `regime_filter`, `vol_rank_max` |

All three APIs are listed on `/api-docs` (group "Portal").

### Data the page reads, and how often

| Source | Every | Used for |
|---|---|---|
| `/api/crypto/coins` | 5 s | Ticker, radar, chart price and forming candle, stream, feed freshness |
| `/api/swing` | 30 s | Filter state, open/closed swing trades, scan, rules, expected numbers, take-vs-skip |
| `/api/portal/signals` | 30 s | Feed, decision trace, toast on a new signal |
| `/api/status` | 30 s | Uptime, symbols tracked |
| `/api/llm/budget` | 60 s | AI budget |
| `/api/portal/meta` | 5 min | Paper/live badge, modes |
| `/api/portal/candles` | on coin change + 2 min | Chart |

Polling pauses while the tab is hidden.

### Conventions

- Escape every string that goes into `innerHTML` (`esc()` in `portal.js`).
- Colours only through the CSS variables; never hard-code a palette colour in JS.
- Up = `--up`, down/short/skip = `--dn`. Text on an accent fill uses `--on-acc`.
- Touch targets ≥ 44 px; layout stacks at phone width with no horizontal page scroll (wide tables scroll in their box).

---

## 5. Verifying

- `python -m pytest -q`: 1,537 passed, 3 skipped (7 Oct 2026), including `tests/test_portal.py`.
- Browser smoke test (headless Chromium against the real app with canned API data): all four hubs render, no console
  errors, theme switch and Ask the desk work, no horizontal scroll at 1440 px or at 390 px (phone).

## 6. Next steps

1. Owner reviews `/command` on production after deploy (deploy = push to `main`).
2. Phase 2: rebuild Audit, Accuracy, Historic and Chart inside Evidence; Settings and Keys inside System; then redirect
   the old routes.
3. Optional: per-hub deep links (`/command?coin=SOLUSDT`), a "what changed since you last looked" digest on Command.
