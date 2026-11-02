# UI rebuild plan: Angular app, speed, design system, and every page

Owner, 1 Nov 2026: "plan every thing, i have too many cursor token pending to expire, let's use it properly with all
the detail thing from angular to every thin". Cursor builds, Claude reviews every PR, the owner approves each phase.

This file is the single source of truth for the rebuild. Each phase ends with a ready-to-paste Cursor prompt (§12).

---

## 0. Ground rules (copy into every Cursor task)

1. Work on a `feat/ui-*` branch and open a PR. **Never push `main`**: main auto-deploys to production.
2. **Nothing in this plan touches trading.** No code that places, sizes, cancels or changes live orders; no reads or
   writes of API keys; no secrets in the repo (it is public). The Python engine's trading paths stay untouched.
3. Every change has a test that fails before and passes after. Full `pytest -q` green (Python) **and**
   `npm run test && npm run e2e` green (web) before asking for review.
4. Production DB is read-only. The new app only calls the existing HTTP API.
5. The old pages keep working until the new page that replaces each one is accepted (§10). No big-bang cutover.
6. Phone-first: every page works at 390 px wide, no sideways scroll, touch targets ≥ 44 px.
7. Plain English in the UI (owner's rule): say what something means, not how it's built.
8. Small PRs: one page or one shared piece per PR, under ~800 changed lines where possible.

9. **Logic lives in the backend (owner, 2 Nov 2026: "core logic should be in backend, very less exposure at
   frontend").** The Angular app only renders what the API returns: no R, P&L, sizing, rule, score or signal maths in
   TypeScript. If a page needs a derived number, add it to the API response (with a Python test). The frontend never
   holds keys, never calls third-party APIs directly, and ships no strategy code.
10. **Look: A, Orbital glass** (owner, 2 Nov 2026: "i actually like orbit glass world globe"). The other looks stay as
    optional themes behind the switch; A is the default everywhere.

---

## 1. Where we are (measured 31 Oct – 1 Nov 2026)

- **Frontend:** vanilla JS portal (`scheduler/portal/`: core.js store + 4 hub files, ~700 KB of JS/CSS) plus
  server-rendered tool pages inside `scheduler/health.py` (8,300 lines: `/moves`, `/predict`, `/chart`, `/classic`,
  `/settings`, `/keys`, `/v2`, `/audit`, `/pipeline`, `/api-docs`) and `scheduler/world_page.py` (`/world`).
- **Style collisions:** tool pages load `portal.css`, whose bare classes (`.coin`, `.pos`, `.hist`, `.gauge`) broke
  Market moves on 31 Oct (fixed with overrides in `command-tools.css`). The new app must scope all styles.
- **Why it feels slow** (from the owner's side in India, server in Sydney, plain HTTP):

| Request | Time to first byte | Total | Size |
|---|---|---|---|
| any page or API, even 1 ms server work | ~0.9 s | | |
| `/api/moves` | 0.95 s | 3.2 s | 536 KB, not compressed |
| `/api/signals/history?days=7&limit=3000` | 1.0 s | 3.1 s | 530 KB, not compressed |
| `portal.css` | 0.9 s | 1.4 s | re-downloaded every visit (no cache) |

  Server-side work is 1–70 ms. The time is network round trips, no compression, no caching, oversized payloads,
  and 22 polling stores (coins every 5 s). **A framework alone fixes none of this → Phase 0 first.**

---

## 2. Phase 0: speed (before any Angular work)

**0a. Server (Claude does this over SSH, owner approves; not a Cursor task):**
- nginx `gzip on` for `application/json text/css application/javascript text/html image/svg+xml`
  (`gzip_min_length 1024; gzip_comp_level 5`). Expected: 536 KB → ~60 KB.
- nginx: `Cache-Control: public, max-age=31536000, immutable` for `/portal/static/*?v=` and the future `/app/*.js|css`
  (file names carry a hash); `no-store` stays on `/api/*`.
- HTTP keep-alive between nginx and aiohttp (`proxy_http_version 1.1; proxy_set_header Connection ""`).
- **Done 2 Nov 2026** (backup `~/backups/nginx_site_20261102_0735`): gzip for proxied JSON/CSS/JS, keep-alive to the
  engine, a year's cache on versioned `/portal/static/*`. Measured: /api/moves 536 KB → 72 KB (3.2 s → 1.7 s),
  signal history 530 KB → 68 KB (3.1 s → 1.8 s).
- **HTTPS:** the owner's domain is on Netlify DNS. Owner adds an `A` record `engine` → the server's IP; Claude then
  adds an nginx `server_name engine.dhruvparekh.in` block and a Let's Encrypt certificate (certbot, auto-renew),
  HTTP → HTTPS redirect, HTTP/2. Check first that the server's IP is an Elastic IP (it must not change on reboot).
- **Distance:** opening a connection from the owner's network to Sydney takes ~0.4 s (measured). Moving the
  engine to Mumbai (ap-south-1) is the remaining big win; plan it as a separate, owner-approved migration.
- **Exposure:** once on the domain, every page and `/api/*` requires sign-in (one owner login, session cookie,
  rate-limited), except a health ping. Today reads are public on the bare IP.

**0b. API payloads (Cursor):**
- `/api/moves`: return only what the page renders; history list without full bodies (`?detail=<id>` for one).
  Target < 80 KB uncompressed.
- `/api/signals/history`: real pagination (`limit` default 200, `cursor`), plus `fields=` to drop unused columns.
- `ETag` / `If-None-Match` on the heavy GET endpoints (aiohttp middleware): unchanged data = 304, no body.
- `/api/crypto/coins` polling 5 s → 15 s in core.js; pause all polling when the tab is hidden
  (`document.visibilityState`).

**Acceptance:** from the owner's network, `/moves` loads fully in < 1.5 s (was ~4.5 s), `/api/moves` < 100 KB on the
wire, static files come from cache on second visit. Measured with the same curl table as §1.

---

## 3. Target architecture

```
repo/
  web/                      ← NEW Angular workspace (Cursor owns)
    src/app/
      core/                 api client, polling, cache, auth, theme, error handling
      shared/ui/            design-system components (no business logic)
      shared/charts/        price chart, equity curve, sparkline, globe wrappers
      features/command/     overview, signals, watchlist, outlook, moves, chart, world, events, calendar
      features/book/        swing, paper, live, guard, simulator, journal
      features/evidence/    strategies, accuracy, history, research, news-study, audit, mirror, pipeline, v2
      features/system/      golive, health, ai, data, diagnostics, settings, keys, api-docs
    e2e/                    Playwright specs + recorded API fixtures
  scheduler/                Python, unchanged except new/slimmed endpoints + serving /app
```

- **Angular 18**, standalone components, **signals** for state, `ChangeDetectionStrategy.OnPush` everywhere,
  new control flow (`@if`, `@for` with `track`), `@defer` for heavy widgets (charts, globe).
- **Routing:** `/app/<hub>/<tab>` mirroring `scheduler/portal/nav.js` exactly, lazy-loaded per hub.
- **Served by** aiohttp static route `/app/` (build output `web/dist/browser`), `index.html` fallback for deep links.
  Later, the bare paths (`/command/...`) switch to the new app page by page (§10).
- **Same origin** → no CORS, the existing admin cookie session keeps working for Settings/Keys.
- **Libraries (exact versions pinned in package.json):**
  - Tailwind CSS 3.4 (tokens as CSS variables, see §4), Angular CDK (overlay, dialog, a11y, virtual scroll)
  - `lightweight-charts` 4.2 (prices, already used on /chart), `echarts` 5 + `ngx-echarts` (all other charts),
    `globe.gl` 2.27 (World), `date-fns` (time), no moment.js
  - Testing: Jest or Vitest (unit), Playwright (e2e + screenshots), `@axe-core/playwright` (accessibility)
  - Lint/format: ESLint (angular-eslint), Prettier
- **Bundle budget:** initial JS < 250 KB gzipped; each hub chunk < 150 KB; globe/echarts only in their routes.

### 3.1 Data layer (`core/`)
- `ApiClient`: typed GET/POST, base `/api`, 15 s timeout, retries 2× on network errors (not on 4xx), maps errors to
  plain-English messages ("The server didn't answer. Retrying in 15 s.").
- `Resource<T>` (signal-based store per endpoint): `data()`, `loading()`, `error()`, `updatedAt()`,
  stale-while-revalidate, shared between components, **one poller per endpoint** no matter how many widgets use it,
  polling paused when the tab is hidden, interval per endpoint from one table (port of `Store.def` in core.js).
- Types: `web/src/app/core/api-types.ts` written from real responses (Cursor captures fixtures with
  `scripts/capture_api_fixtures.py`, read-only GETs only). A test fails if a fixture no longer matches its type.
- Later (Phase 5): Server-Sent Events `/api/stream` for prices + live book changes, replacing the fast polls.

### 3.2 Auth
- After the HTTPS move every route needs the owner's sign-in (session cookie, HttpOnly, Secure, SameSite=Strict).
  The app shows a sign-in screen on 401. Settings/Keys keep their extra admin step. No tokens in localStorage.

---

## 4. Design system

**Owner chooses the look** (open question, see §13). Two candidates are ready:
- the three World looks already live on `/world` (A Orbital glass, B Holo neon, C Ops terminal), and
- the new wireframe concepts from 31 Oct: **Orbit cockpit** (live trades as planets around the wallet, distance = R),
  **Time river** (one timeline: world, price, signals, trades, with a "now" line), **Holo deck** (command bar +
  tiles that expand into focus).

Whatever is chosen, it is built as **tokens + themes**, so A/B/C (and the current brutal skin) stay switchable:

- `shared/ui/tokens.css`: `--surface-0..3`, `--ink`, `--dim`, `--accent-1/2`, `--up`, `--down`, `--warn`,
  `--glass`, `--edge`, `--radius-*`, `--space-*`, `--font-display/body/data`. Themes set them on `html[data-look]`.
- Fonts: Chakra Petch (display), IBM Plex Sans (body), JetBrains Mono (numbers/data) — Google Fonts, `display=swap`.
- Numbers always `font-variant-numeric: tabular-nums`; up/down colours also differ in lightness (colour-blind safe);
  never rely on red/green alone (add ▲▼ or +/−).
- Motion: one entrance per page, subtle; respect `prefers-reduced-motion`.

**Components (`shared/ui/`), each with a Storybook-style demo page at `/app/_kit` and a unit test:**
`Panel` (glass card, loading/error/empty states built in), `Stat` (label + big number + delta), `Pill`/`Badge`
(levels L1–L5, long/short, won/lost), `DataTable` (sortable, sticky header, phone → card list, virtual scroll > 200
rows), `Tabs`, `Segmented`, `Dialog`, `Toast`, `Tooltip`, `EmptyState`, `ErrorState` (with Try again),
`Skeleton`, `Sparkline`, `RBar` (stop · entry · now · target on one bar), `LevelChip`, `TimeAgo`, `Money`
(₹ with lakh grouping), `Ticker`, `CommandBar` (⌘K / tap search: jump to any page or coin).

---

## 5. Page inventory (33 pages) → new routes, data, priority

Priority: **P1** = rebuild first (used daily), P2 = next, P3 = rarely used (keep old page until last).

| Old page | New route | Data (existing API) | Priority | Notes |
|---|---|---|---|---|
| Live ₹ (book/live) | /app/book/live | /api/delta/live (admin), /api/delta, /api/golive | **P1** | the most important page: positions with RBar (stop/entry/now/target), exchange orders, floor, capital rules (test mode), deposits |
| Swing book | /app/book/swing | /api/swing, /api/paper | **P1** | open trades, closed trades, equity curve |
| Command overview | /app/command/overview | coins, swing, signals, meta, status | **P1** | cockpit summary |
| Signals | /app/command/signals | /api/portal/signals, history7 | **P1** | filters won/lost/traded |
| Market moves | /app/command/moves | /api/moves (slimmed) | **P1** | coin cards, drivers, history |
| World | /app/command/world | /api/portal/world | **P1** | port of /world with 3D globe and A/B/C |
| Chart | /app/command/chart | /api/chart/* | P2 | wrap existing lightweight-charts code, keep indicators/drawings |
| Watchlist | /app/command/watchlist | coins | P2 | |
| Price outlook | /app/command/outlook | /api/predict | P2 | |
| Events | /app/command/events | /api/events, /api/portal/news | P2 | merge into World later? |
| Calendar | /app/command/calendar | /api/portal/news (calendar) | P2 | needs FMP key to fill |
| Paper | /app/book/paper | /api/paper, /api/candidates, /api/bracket, /api/vol | P2 | candidate books (swing_2h, impulse) |
| Guard | /app/book/guard | /api/parity, status | P2 | |
| Simulator | /app/book/simulator | /api/sim/live-book | P2 | form + results chart |
| Journal | /app/book/journal | journal API | P3 | |
| Strategies | /app/evidence/strategies | /api/research, registry | P2 | |
| Accuracy | /app/evidence/accuracy | /api/signals/accuracy | P3 | |
| History | /app/evidence/history | /api/signals/history (paged) | P3 | |
| Research | /app/evidence/research | /api/research | P3 | |
| News study | /app/evidence/news-study | news study API | P3 | |
| Audit | /app/evidence/audit | /audit data | P3 | |
| Mirror | /app/evidence/mirror | classic mirror data | P3 | |
| Pipeline | /app/evidence/pipeline | pipeline data | P3 | |
| v2 shadow | /app/evidence/v2 | /api/v2/shadow | P3 | |
| Go-live | /app/system/golive | /api/golive | P2 | |
| Health | /app/system/health | /api/status, /api/debug | P2 | |
| AI | /app/system/ai | /api/llm/budget, ai ledger | P3 | |
| Data | /app/system/data | /api/tables | P3 | |
| Diagnostics | /app/system/diagnostics | /api/debug, /api/debug/perf | P3 | |
| Settings | /app/system/settings | /api/settings (admin) | P2 | real-money fields keep their warnings; no new fields |
| Keys | /app/system/keys | /keys API (admin) | P3 | never shows a key value |
| API docs | /app/system/api-docs | api_docs list | P3 | |
| Classic | — | — | retire | after all P1–P3 accepted |

Each page PR must: list every number it shows and the API field it comes from (in the PR text), handle loading /
empty / error, work at 390 px, and include a Playwright spec against recorded fixtures + one desktop and one phone
screenshot.

---

## 6. Backend changes the new app needs (Cursor, Python, small PRs)

1. Serve `/app/` from `web/dist/browser` with `index.html` fallback; long cache for hashed files (test).
2. Slim `/api/moves` and paginate `/api/signals/history` (§2 0b) — keep old params working.
3. ETag middleware for GET `/api/*` (test: second call with `If-None-Match` → 304).
4. `/api/stream` (SSE): `prices` every 5 s, `live` on any live-book change, `news` on new L3+ events. Heartbeat 15 s.
   Read-only. (Phase 5.)
5. `scripts/capture_api_fixtures.py`: read-only GETs → `web/e2e/fixtures/*.json`, scrubbing anything that looks like a
   key or token.

---

## 7. Build, CI and deploy

- `.github/workflows/deploy.yml`: add a `web` job — `npm ci`, `npm run lint`, `npm run test`, `npm run build`,
  `npx playwright install --with-deps chromium && npm run e2e` — before the existing Python tests; the deploy step
  copies `web/dist/browser` to the server with the code. Deploy stays blocked if any job fails.
- Node 20 LTS pinned (`.nvmrc`), `package-lock.json` committed, Renovate/Dependabot off for now (no surprise bumps).
- The server never runs `npm` — it only receives built files (2 GB RAM box).

---

## 8. Quality gates (every PR)

- Unit tests for every component and resource store; e2e for every page (fixtures, no network).
- Accessibility: axe has no serious/critical issues; keyboard reachable; visible focus.
- Performance: Lighthouse (mobile) ≥ 85 performance on the P1 pages after Phase 0; bundle budgets enforced by
  `angular.json` budgets (build fails when exceeded).
- Visual: phone 390×844 and desktop 1440×900 screenshots attached to the PR.
- Claude's review checks: numbers match the old page for the same moment (side-by-side), no trading calls, no
  secrets, styles scoped, budgets met.

---

## 9. Phases and order

| Phase | Content | Size | Gate |
|---|---|---|---|
| 0 | Speed: nginx (Claude), payload slimming + ETag + polling (Cursor) | 1–2 days | §2 acceptance |
| 1 | Angular workspace, core data layer, theme tokens (A/B/C + brutal), UI kit, `/app/_kit`, `/app/` serving, CI job | 3–5 days | kit page + CI green |
| 2 | P1 pages: Live ₹, Swing book, Overview, Signals, Market moves, World | 1–2 weeks | owner uses them for 3 days, numbers match old pages |
| 3 | P2 pages | 1–2 weeks | same |
| 4 | P3 pages + retire old portal and `/classic` | 1 week | all routes moved, old files deleted in one PR |
| 5 | SSE live stream, command bar (⌘K), Orbit/River/Deck concept view if chosen, PWA (install to phone home screen, offline shell) | 1 week | live updates < 2 s |

Parallel tracks Cursor can run on separate branches while Phase 1–2 are in review (§11).

---

### 9.1 One-go mode (owner, 2 Nov 2026: "plan entire change in one go, instead of phases")

Cursor builds everything in §3–§8 and every page in §5 in one continuous task on **one branch,
`feat/ui-rebuild`**, in this order: foundation → UI kit → P1 pages → P2 pages → P3 pages → SSE stream, ⌘K,
PWA. Rules for one-go mode:
- **One commit per finished unit** (foundation, kit, then one commit per page), each with its own tests passing, so
  Claude can review commit by commit and any page can be reverted alone.
- Push after every commit; open **one draft PR** after the first commit and keep pushing to it. Mark it ready only
  when everything is done.
- The old portal and every old route stay untouched; the new app lives only under `/app/`. Cutover (§10) is a
  separate, later PR after the owner has used `/app` for 3 days.
- If a page needs an API change, it goes in the same commit as the page, with a Python test, and must not change
  what the old pages receive.
- At the end: a PR description with the page-by-page table (every number, its API field, old-page value at the
  same moment), screenshots of every page at 390 px and 1440 px in look A, bundle sizes, Lighthouse scores.

## 10. Cutover

- Both apps run side by side. A "Try the new app" link sits in the old header; the new app has "Back to old".
- A page moves when: the owner has used the new one for 3 days, numbers match, no open bugs. Then the bare route
  (e.g. `/book/live`) redirects to `/app/book/live`; the old code is deleted in a separate PR a week later.

---

## 11. Other Cursor work worth the tokens (separate branches, all default-off where they touch the engine)

1. **News engine, rest of the plan:**
   - GDELT 15-minute article parsing on the Mac (`scripts/news_backfill.py` currently prints a message) and a small
     server-side reader of the latest export's headlines (limit memory < 100 MB).
   - FMP economic calendar ingestion with consensus/actual (needs the owner's free key) → Calendar page.
   - Sentiment indices (W9) as labels, impulse ↔ event links (W18) stored in `news_impulse_links`, event reactions
     (W10: BTC/ETH/gold move 15 m / 1 h / 4 h after each L3+ event).
   - Separate the monitor/news-engine ids so `news_engine_feeds_monitor` can be switched on safely (see runner).
   - News study page with the event-study results (Evidence > News study).
2. **ML + RAG plan** (Claude hands Cursor the phase prompt when this track starts): Phase 0 dataset and Phase 1
   evaluation harness, offline only, leakage tests.
3. **Test hygiene:** no test may write inside `audit/` or `data/` (add a guard fixture that fails on writes there).
4. **Docs:** `docs/learn/` one-pagers for each page explaining the numbers in plain English (owner learning).

---

## 12. Ready-to-paste Cursor prompts

**Prompt P0 (Cursor part of Phase 0):**
> Read docs/plans/UI_REBUILD_PLAN.md §0, §1, §2 (0b) and §6 items 2–3. On branch feat/ui-p0-speed: slim /api/moves
> (only fields the /moves page renders; history entries without bodies, `?detail=<id>` for one), paginate
> /api/signals/history (limit default 200, cursor, fields=), add an ETag middleware for GET /api/*, change the coins
> poll to 15 s and pause every Store poll while the tab is hidden. Keep old query params working. Tests for each
> (fail before, pass after), full pytest green, report before/after payload sizes in the PR. Open a PR, do not merge.

**Prompt P1 (foundation):**
> Read docs/plans/UI_REBUILD_PLAN.md fully. On branch feat/ui-p1-foundation create the Angular 18 workspace in web/
> exactly as §3 (standalone, signals, OnPush, lazy hubs, routes mirroring scheduler/portal/nav.js), the core data
> layer (§3.1, port Store.def intervals), theme tokens with looks A/B/C copied from scheduler/world_page.py plus the
> current brutal skin (§4), every UI kit component in §4 with unit tests and the /app/_kit demo page, aiohttp serving
> of /app/ (§6.1) and the CI web job (§7). No feature pages yet. Budgets in angular.json. Open a PR with screenshots
> of /app/_kit at 390 px and 1440 px in all looks. Do not merge.

**Prompt P2 (one PR per page, in this order):** Live ₹ → Swing book → Overview → Signals → Market moves → World
> Read docs/plans/UI_REBUILD_PLAN.md §5 row "<PAGE>". On branch feat/ui-p2-<page>, build /app/<route> using only the
> UI kit and Resource stores. Capture fixtures with scripts/capture_api_fixtures.py, Playwright spec on fixtures,
> phone + desktop screenshots in every look, and a PR table listing every number on the page with its API field and
> the same number on the old page at the same moment. Loading/empty/error states. Do not merge.

**Prompt P3 / P4:** same as P2 for each remaining row of §5, then one PR that switches bare routes and one that deletes
the old portal after a week.

**Prompt P5:** §6.4 SSE stream + the app subscribing to it (fallback to polling), ⌘K command bar, PWA manifest and
service worker (app shell only, never cache /api), and the chosen concept view (Orbit / River / Deck).

**Prompt News (parallel):** §11.1 items, each its own PR, default-off flags, fixtures, no network in tests.

---

## 13. Decisions the owner still makes

1. ~~Look~~ decided 2 Nov: **A, Orbital glass**.
2. HTTPS on engine.dhruvparekh.in (owner adds the Netlify DNS record), and whether to move the server to Mumbai.
3. Free API keys for FMP (calendar) and FRED (fiscal data), entered only on the Keys page.
4. When the old portal is switched off (after Phase 4 acceptance).
