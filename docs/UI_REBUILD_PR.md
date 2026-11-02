# UI rebuild (`feat/ui-rebuild` → `main`)

## Compare / open PR

https://github.com/dhruvparekhdp/crypto-signal-engine/compare/main...feat/ui-rebuild

**Stack:** merge **`feat/ui-p0-speed`** first (or this branch already includes P0 via merge commit).

## What’s in this PR

| Area | Notes |
|------|--------|
| **Angular 18** | Standalone app under `web/`, served at `/app/` via `scheduler/web_static.py` |
| **Design system** | `web/src/app/shared/ui/*`, tokens A/B/C/brutal, `/app/_kit` |
| **Pages** | P1: Live, Overview, Signals, Moves, World (+ globe.gl lazy). P2/P3: `GenericApiPage` + `page-meta.ts` for all nav tabs |
| **Shell** | Hub nav, ⌘K command bar, theme picker |
| **Backend** | `GET /api/stream` SSE heartbeat (`scheduler/api_stream.py`) |
| **Fixtures** | `scripts/capture_api_fixtures.py` → `web/e2e/fixtures/` |
| **PWA** | `manifest.webmanifest` + `sw.js` (no `/api` cache) |
| **CI** | Deploy workflow: `npm ci`, `test:ci`, `build:prod`, Playwright e2e |

## Test plan

```bash
# Python
rm -f crypto_engine.db && pytest -q tests/test_api_stream.py tests/test_web_app_static.py

# Web
cd web && npm ci && npm run test:ci && npm run build:prod && npm run e2e:install && npm run e2e
```

## Screenshots (capture after deploy / local)

1. `/app/command/overview` — desktop + 390px
2. `/app/book/live` — positions + R-bar
3. `/app/command/world` — globe + event list
4. `/app/_kit` — theme switch

## Out of scope (unchanged)

- No trading / order placement / API keys in repo
- Legacy HTML portal untouched except prior P0 `core.js` polling on its branch
