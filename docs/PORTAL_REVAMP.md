# Portal revamp: "Brutal Command"

The single place for everything decided about the portal redesign: why it changed, what it looks like, where
each old page went, how the code is laid out, and what is left to do. Read this before touching
`scheduler/portal.py` or `scheduler/portal/`.

Status (7 Oct 2026): **v2 (all phases) built on branch `claude/portal-design-revamp-l69rn1`, not yet deployed.** The owner
reviews and tests everything on the branch, then deploys at once.

- v1: the four-hub portal shell and the Command hub on live data.
- v2 (this round): every old page clubbed into a hub; one navigation on every page (no more pages with only a
  "← back" link); retired addresses redirected; the front end split into reusable components and per-hub micro UI
  modules; versioned, long-cached assets.

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
| Phase 2 brief | "Build all phases on this branch only. Club some pages and give proper redirection: some pages have all options in the nav bar and some have one option to go back. Create multiple reusable components as micro UI services, in an optimised way." |

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

## 3. Information architecture: every page in one of 4 hubs

The single source of truth is `scheduler/portal/nav.js`. Both the portal shell and every server-rendered tool page
draw their header from it, so there is exactly one navigation and it is the same everywhere: the four hubs, then
the pages of the current hub. Pages marked ↗ are **tools**: their own server page, shown under the same header.

| Hub | Pages (native) | Tools (own page, same header) |
|---|---|---|
| 01 Command `/command` | **Overview**, **Signals** `/command/signals`, **Watchlist** `/command/watchlist` | Price outlook `/predict`, Market moves `/moves`, Chart `/chart` |
| 02 Book `/book` | **Swing book**, **Paper cycle** `/book/paper`, **Session guard** `/book/guard` | Simulator `/classic#simulator`, Journal `/journal` |
| 03 Evidence `/evidence` | **Strategies**, **Accuracy** `/evidence/accuracy`, **History** `/evidence/history`, **Research** `/evidence/research` | Audit `/audit`, Mirror `/classic#mirror`, Pipeline `/pipeline`, v2 shadow `/v2` |
| 04 System `/system` | **Health**, **Data** `/system/data`, **Diagnostics** `/system/diagnostics` | Settings `/settings`, Keys `/keys`, API list `/api-docs` |

### What was clubbed

| Old page(s) | Now |
|---|---|
| Dashboard (`/#dashboard`) + Signals (`/#crypto`) | Command › Signals: one 7-day table with coin/side/status filters, sort, search, and "why refused" |
| Watchlist (`/#watchlist`) + the add box on Signals | Command › Watchlist: the list (sortable, remove) + Binance search and add |
| Paper Trading (`/#paper`) | Book › Paper cycle: cycle tiles, open positions (all modes), filterable history with totals |
| Session Guard (`/#guard`) | Book › Session guard: same flags and drift table, from the paper book |
| Accuracy (`/#accuracy`) | Evidence › Accuracy: calibration, move-size histogram, accuracy by setup |
| Historic Data (`/#historic`) | Evidence › History: archive tiles + filterable archive table |
| Research report + v2 shadow summary | Evidence › Research (full v2 page stays a tool) |
| Database dump (`/data`) | System › Data: every table, expandable, newest rows |
| Debug feed + perf | System › Diagnostics: feed state per coin, route/job speed, probe links |
| Swing scan, AI budget, news pauses, engine facts | System › Health |

### Every old page and where it goes now

| Old address | Behaviour |
|---|---|
| `/` | 302 → `/command` |
| `/classic` (no hash) | → `/command` |
| `/classic#dashboard`, `#crypto` | → `/command/signals` |
| `/classic#watchlist` | → `/command/watchlist` |
| `/classic#paper` / `#guard` | → `/book/paper` / `/book/guard` |
| `/classic#accuracy` / `#historic` | → `/evidence/accuracy` / `/evidence/history` |
| `/classic#mirror`, `/classic#simulator` | Stay (tools) under the new header; the old sidebar is hidden |
| `/data` | 301 → `/system/data` |
| `/<hub>/<tool page>` (e.g. `/book/simulator`) | Forwarded to the tool's address |
| `/predict` `/moves` `/chart` `/audit` `/settings` `/settings/classic` `/keys` `/journal` `/v2` `/pipeline` `/api-docs` | Same address, now with the portal header (hub + its pages + theme + paper/live badge) and the new skin. Their old "← Dashboard" links, nav rows, sidebar and theme pickers are hidden |

Hash anchors never reach the server, so the `/classic#…` forwards run in `chrome.js` before the old page loads its
data (also on a hash change while on `/classic`).

## 4. Code: micro UI architecture

```
scheduler/portal.py              routes, redirects, versioned assets, chrome_snippet(), 3 small APIs
scheduler/portal/
  nav.js                         the navigation map (hubs → pages → tools → aliases)            ~4 KB
  core.js                        store · component kit · widget runtime · shell (router, strip, theme)
  hubs/command.js                Command widgets + views     (loaded on first visit to the hub)
  hubs/book.js                   Book widgets + views
  hubs/evidence.js               Evidence widgets + views
  hubs/system.js                 System widgets + views
  portal.html / portal.css       the shell page and its styles (palettes, components, motion)
  chrome.js / chrome.css         header + brutal skin for the server-rendered tool pages
tests/test_portal.py             routes, redirects, assets/caching, tool-page header, nav ↔ views ↔ widgets wiring
design/revamp/                   the design boards
```

### The pieces

- **Store** (`UI.Store`): one entry per endpoint (`Store.def(key, url, everyMs)`). Widgets subscribe with
  `Store.use(key, fn)`; an endpoint polls only while something on screen uses it, every subscriber shares one
  request, and polling pauses while the browser tab is hidden (stale entries refresh when it comes back). The coin
  feed, swing book, meta and signals are shared by the shell (ticker, status strip, toast) and the widgets.
- **Widgets** (`UI.widget(name, def)`): self-contained units. `def.uses` lists store keys; `mount` builds static DOM;
  `render(el, D, ctx)` draws from the data snapshot `D`; `on: {act: fn}` handles any `[data-act]` element inside it
  (click, input and change are delegated once for the whole page); `ctx.s` is the widget's own state;
  `ctx.every()` timers are cleared on unmount. A widget that throws shows "This panel failed to draw" instead of
  breaking the page. `title` gives it the standard panel header; `bare` renders without a panel.
- **Views** (`UI.view("hub/page", rows)`): a page is rows of cells; a cell is a widget name, `{w, size}` with size
  `grow` / `side` / `full`, or `{stack: [...]}`. Rows wrap, so every page stacks on a phone.
- **Components** (`UI.C`, pure functions returning escaped HTML): `panel` (via widget title), `tiles`, `table`
  (sortable headers via `C.sortState`), `select`, `search`, `chip`, `tag`, `side`, `signed`, `meter`, `rbar`, `bars`,
  `hist`, `flag`, `links`, `pre`, `empty`, `loading`, `state` (standard loading / error / admin-login line for a store
  key).
- **Shared facts** (`UI.F`): `coin`, `regime` (market filter), `openSwing` (positions in R), `riskPct`, `budgetShare`,
  `live`. **Helpers** (`UI.U`): `esc`, price/percent/R/₹ formatting, `ago`, `stamp`, `hours`, `median`.
- **Network** (`UI.Net`): `get`, and `post` with the same API-token flow as the classic pages (localStorage
  `api_token`, asked once on a 401) for watchlist add/remove.

### Adding a page

1. Add `{ id, label, native: true }` to the hub in `nav.js` (or `url: "/x"` for a tool).
2. In `hubs/<hub>.js`: `UI.widget(...)` for each panel, then `UI.view("<hub>/<id>", rows)`.
3. A new endpoint: `UI.Store.def("key", "/api/...", everyMs)` in `core.js`.
`tests/test_portal.py` fails if a native page has no view, a view names a missing widget, or a widget uses an
undefined store key.

### Performance

- Hub modules load only when their hub is opened (Command alone on first visit).
- Assets are served as `/portal/static/<name>?v=<content hash>` with `Cache-Control: immutable, max-age=1y`; the
  shell and tool pages always reference the current hash, so a deploy is picked up at once and nothing is
  downloaded twice. Text responses over 1 KB are gzipped by the existing middleware.
- Only the open page's widgets are mounted; leaving a page unsubscribes its endpoints and clears its timers.
- One request per endpoint however many widgets read it; no polling in a hidden tab.

### Routes (`scheduler/portal.py`)

| Route | What |
|---|---|
| `GET /` | 302 → `/command` |
| `GET /<hub>`, `/<hub>/<page>` | The shell (page must be a slug, else 404); the router picks the view, forwards tools, and normalises unknown pages to the hub |
| `GET /data` | 301 → `/system/data` |
| `GET /portal/static/<asset>` | Whitelisted assets only (`ASSETS`); `?v=` matching the content hash → cached for a year, otherwise `no-cache` |
| `GET /api/portal/candles?symbol=&tf=4h&limit=48` | Closed bars via `analysis.swing_book.fetch_bars`, cached 60 s per symbol+tf; bad input → 400, exchange error → 502 |
| `GET /api/portal/signals?days=14&limit=40` | Swing signals with `status` TRADED / NOT TRADED / FILTER BLOCK, the human skip reason and the regime tag |
| `GET /api/portal/meta` | `live_trading_mode`, `paper_trading_enabled`, `swing_enabled`, `regime_filter`, `vol_rank_max` |

The tool pages get the header through `chrome_snippet()`, appended to the shared head snippet in
`scheduler/health.py` (and to `/keys`, which does not use it). The old theme system now always applies the
`brutal` theme in the palette picked in the portal (`localStorage` `portal-palette`); `chrome.css` maps the old
pages' colour variables onto the four palettes.

### Data the page reads, and how often

| Store key | Endpoint | Every | Used by |
|---|---|---|---|
| `coins` | `/api/crypto/coins` | 5 s | ticker, strip, chart, radar, stream, watchlist, positions in R |
| `swing` | `/api/swing` | 30 s | strip, pipeline, gate, swing book, strategies, scan, engine |
| `signals` | `/api/portal/signals` | 30 s | feed, trace, toast, Ask the desk |
| `meta` | `/api/portal/meta` | 5 min | paper/live badge, modes |
| `status` | `/api/status` | 30 s | pipeline, engine |
| `budget` | `/api/llm/budget` | 60 s | pipeline, AI budget |
| `paper` | `/api/paper` | 20 s | paper cycle, session guard |
| `history7` | `/api/signals/history?days=7` | 60 s | Signals |
| `archive` | `/api/signals/history?days=365&before=7` | 5 min | History |
| `accuracy` | `/api/signals/accuracy` | 5 min | Accuracy, Signals (cost floor) |
| `research` / `v2` | `/api/research`, `/api/v2/shadow` | 5 min | Research |
| `events` | `/api/events` | 2 min | Health |
| `debug` / `perf` | `/api/debug`, `/api/debug/perf` | 30 s / 60 s | Diagnostics (perf needs admin login) |
| `tables` | `/api/tables?limit=50` | on open + Reload | Data |
| `candles:<SYM>` | `/api/portal/candles` | 2 min | chart (one key per coin viewed) |

### Conventions

- Escape every string that goes into HTML (`UI.U.esc`; the components already do).
- Colours only through the CSS variables; never hard-code a palette colour in JS.
- Up = `--up`, down/short/skip = `--dn`. Text on an accent fill uses `--on-acc`.
- Touch targets ≥ 44 px; layout stacks at phone width with no horizontal page scroll (wide tables scroll in their box).
- Money from the paper book is INR (`UI.U.inr`); prices stay in USDT.

## 5. Verifying

- `python -m pytest -q`: 1,544 passed, 3 skipped (7 Oct 2026), including `tests/test_portal.py` (routes, redirects,
  asset caching, header on every tool page, nav ↔ views ↔ widgets ↔ store keys).
- Browser smoke test (headless Chromium against the real app with canned API data): all 13 native pages render with
  no console errors and no failed panels; all 12 tool pages show the header with the right hub and page highlighted;
  the `/classic#…`, `/classic`, `/data` and `/<hub>/<tool>` redirects land where the table above says; filters and
  back/forward work in-app; no horizontal scroll at 1440 px or 390 px.

## 6. Review checklist before deploying

1. Open `/command`, then click through every hub and every page; check the tools (↗) open with the same header.
2. Try two old bookmarks: `/#paper` and `/data`.
3. Switch the four palettes in the header; open `/settings` and `/audit` to see the tool pages follow.
4. Add and remove a pair on Command › Watchlist (needs the API token, same as before).
5. Deploy = merge to `main`.

Later, optional: rebuild the remaining tools (Audit, Chart, Settings, Keys, Simulator, Mirror) as native widgets one
by one; each then only needs its nav entry switched from `url` to `native` and an alias for the old address.
