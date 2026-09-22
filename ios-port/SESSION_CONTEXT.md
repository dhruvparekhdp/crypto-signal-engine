# Session context — everything up to the handoff

This is the full record for whoever (human or Claude) picks up the iOS port
from here. It exists because the work so far was done in a **remote Linux
sandbox with no macOS, no Xcode, no Swift compiler, and no access to the Mac
this project actually lives on** (`/Users/dhruv/personal/HelloWorld/dhruv`).
Everything below was written and reviewed carefully but **never built**. The
first job of whoever reads this is to close that gap.

If you are a fresh Claude Code session reading this cold: read this whole
file before touching anything. Then read `PORT_NOTES.md` (repo root) and
`ios-port/INTEGRATION.md`, in that order. Then act.

---

## 1. What this project is

`dhruvparekhdp/Tennis-bet` on GitHub. Started as a tennis/football betting
monitor, pivoted entirely to a crypto signal-and-forecast system. Python
(aiohttp) backend deployed on an AWS EC2 box at `http://52.62.37.4:8080`,
with a browser dashboard served from the same process. The owner (Dhruv)
wants a native iOS companion app, which is a separate existing Xcode project
at `~/personal/HelloWorld/dhruv` (bundle ID `dp.dhruv`, its own git repo,
**not** the same repo as Tennis-bet).

The Python backend and its web dashboard are mature — hundreds of commits of
debugging real trading logic (candle feeds, confluence voting, cost models,
a forecast engine, a live-desk order-execution flow that was later removed
in favour of a data-only approach). None of that history needs to be
re-litigated for the iOS work; what matters for the port is summarised
below and in `PORT_NOTES.md`.

## 2. What was decided, by the project owner, across this conversation

In order, each a direct answer to a question this session raised:

1. **Accept the notification-payload redesign.** The original plan risked
   shipping stale, actionable price levels through a 15-minute-lag
   background refresh. Resolved by NOT putting levels in push notifications
   at all — notify "a setup is live", recompute the actual entry/stop/target
   from the live price when the user opens the app. See PORT_NOTES.md §9 for
   the full reasoning (a 15-minute-old ETH signal's reward:risk degrades
   from 2.00 to 0.81; on SOL, to 0.60 — not degradation, inversion).
2. **Port Price Outlook first**, not the signal/alert engine. It's
   stateless, always has something to show (the signal engine's honest
   answer is usually "no trade right now," which would leave a signals
   screen empty most of the time), and has no cooldown/contradiction
   bookkeeping to replicate. See PORT_NOTES.md §10.4.
3. **Keep all eight watchlist coins** — no narrowing.
4. **Clone a local repo / start iOS development** — attempted, discovered
   this session has zero filesystem access to the Mac (confirmed
   exhaustively: checked `mount`, `df -h`, searched the whole sandbox
   filesystem for `HelloWorld`/`dhruv`/`personal`, found nothing — this is
   an architectural boundary of the remote sandbox, not a path that needed
   finding). Pivoted to: write the Swift source here, commit it to the
   Tennis-bet repo under `ios-port/` for version control, and hand it off
   via a downloadable zip **and** this git history for a session that
   actually has Mac + Xcode access to pick up.
5. **Create proper Python APIs with basic security.**
6. **Implement Face ID login.**

Items 5 and 6 were built together, deliberately — the iOS app's Face ID flow
exists specifically to gate the Keychain-stored token that authenticates its
calls to the newly-secured API.

## 3. Backend: what changed (already built, tested, merged to `main`)

New file: `scheduler/security.py`. New tests: `tests/test_api_security.py`
(28 tests, all passing, run via `python -m pytest tests/test_api_security.py -q`).

**The problem it fixes:** three endpoints had zero protection —
`POST /api/settings/toggle`, `POST /api/crypto/watchlist/add`,
`POST /api/crypto/watchlist/remove`. Anyone with the URL could toggle a data
collector or edit the watchlist.

**What was added, deliberately scoped small** (one operator, not a
multi-user auth system):

- **Bearer token auth** (`check_bearer_auth`) on those three endpoints, plus
  a new `POST /api/auth/verify` used by clients to test a token before
  trusting it. Config: `API_AUTH_TOKEN` env var. **Fails closed** — empty
  token means those endpoints refuse everything (503), not "open by
  accident." Header only (`Authorization: Bearer <token>`), no query-string
  fallback (a token in a URL ends up in proxy logs and Referer headers).
- **Rate limiting**, two separate buckets: a general one for ordinary
  `/api/*` traffic (`API_RATE_LIMIT_REQUESTS` / `API_RATE_LIMIT_WINDOW_SECONDS`,
  default 120/60s), and a much tighter one scoped only to
  `/api/auth/verify` (`API_AUTH_RATE_LIMIT_REQUESTS` /
  `API_AUTH_RATE_LIMIT_WINDOW_SECONDS`, default 10/300s) — because that
  endpoint's whole job is accepting attempts at the shared secret. Reads
  client IP from `X-Forwarded-For` first (a reverse proxy would terminate TLS in front of this;
  `request.remote` alone would bucket every client together).
- **Security headers** on every response (nosniff, frame-deny, no-referrer,
  `Cache-Control: no-store` on JSON responses) via middleware, not
  per-handler.
- **Read-only endpoints stay open** — `/api/predict`, `/api/crypto/coins`,
  etc. carry no secret, and gating them would break the existing web
  dashboard for no security benefit. This is why the iOS Price Outlook
  screen needs no token to function; only the Settings/watchlist-editing
  path does.
- The existing dashboard JS was updated (not broken): a small `apiFetch()`
  wrapper in the shared theme snippet attaches a `localStorage`-cached
  token automatically and prompts once on a 401.

**Operator action required, not yet done as far as this session knows:**
set `API_AUTH_TOKEN` on the EC2 box. Pick a long random string.
Without it, the three protected endpoints stay refused (503) — which is
safe, but the watchlist-edit UI and the iOS Settings token flow won't work
until it's set.

## 4. iOS: what was written (never built — see §6)

Location: `ios-port/` in the Tennis-bet repo (also delivered as a zip via
the chat's file-send mechanism, `dhruv-ios-port.zip`). Structure mirrors the
target project's own layout so it drops straight into
`~/personal/HelloWorld/dhruv/`:

```
ios-port/
  dhruv/
    App/
      AppConfig.swift          — base URL (52.62.37.4:8080), Keychain key name
      RootView.swift           — AppLockView (Face ID gate + re-lock on backgrounding)
                                  + MainTabView (5 tabs)
    Models/
      APIError.swift           — MarketDataError enum: .transport .decode .unauthorized
                                  .rateLimited .server — deliberately NOT one generic case
      PriceOutlook.swift       — Codable structs matching GET /api/predict exactly
    Services/
      APIClient.swift          — MarketDataClient protocol + LiveMarketDataClient actor.
                                  Talks to the EXISTING Python backend, not Binance directly
                                  (see §5 for why). Retry w/ jitter, honours Retry-After.
      BiometricAuthService.swift — LAContext wrapper, .deviceOwnerAuthentication
                                  (Face ID/Touch ID WITH passcode fallback, not biometry-only)
      KeychainStore.swift       — plain + biometry-required (.userPresence) access tiers
    Features/
      Auth/LockScreenView.swift
      PriceOutlook/            — PriceOutlookView, PriceOutlookViewModel (polls every 20s,
                                  matches the web dashboard's own cadence), MarketOutlookCard,
                                  ForecastBandView (custom range-bar widget)
      Settings/APITokenSettingsView.swift  — enter token, verify via /api/auth/verify,
                                  store behind biometric Keychain
      ComingSoonView.swift     — placeholder for Signals/Watchlist/Accuracy tabs
  dhruvTests/
    PriceOutlookDecodingTests.swift   — decodes a REAL captured /api/predict response
                                        (not hand-typed), plus timestamp-parsing edge cases
    PriceOutlookViewModelTests.swift — pure logic against a scripted fake client
  INTEGRATION.md              — step-by-step merge/build instructions
```

~1,825 lines of Swift across 16 files. Every file was checked by hand for
brace/paren balance, protocol conformance, and control flow; the embedded
JSON test fixture was verified to parse standalone with Python's `json`
module. None of it has been through a Swift compiler.

## 5. The one real architecture decision made without the owner's Mac-side confirmation

**The iOS app is a client of the existing Python backend, not a
reimplementation of the forecast/signal logic in Swift talking to Binance
directly.** `LiveMarketDataClient` calls `GET /api/predict` and
`POST /api/auth/verify` on `52.62.37.4:8080`.

This departs from `PORT_NOTES.md`'s original Phase 0 mapping table, which
sketched a fuller on-device port (`collectors/binance_klines.py` →
`Services/MarketDataClient`, `analysis/confluence.py` → `Services/Confluence`,
etc.). The reasoning for the change: the confluence voting, the cost model,
and every hard-won fix in this project's git history (dead-candle bugs,
volume-reading bugs, the contradiction-guard fix, the archive-poisoning
fix — all real production incidents, all in the commit history) already
live server-side, tested, in one place. Reimplementing that a second time in
Swift creates two implementations that drift the moment either side changes.

**This has not been explicitly confirmed by the owner as the permanent
direction** — it was flagged as a reversible choice in `INTEGRATION.md`. If
the owner wants the fuller on-device port after all (offline capability,
no dependence on the EC2 box staying up), that's a real fork:
say so before building further on the current client-of-backend design.

## 6. What is verified vs. not — be precise about this

**Verified (tests pass, this session ran them):**
- Backend: `python -m pytest tests/ -q` — 586 passed, including the new 28
  security tests. Run from the Tennis-bet repo root.
- The JSON fixture embedded in `PriceOutlookDecodingTests.swift` parses as
  valid JSON standalone (checked with Python, not Swift).
- Every Swift file has balanced braces and parens (a crude but real check).

**NOT verified — no Swift compiler was ever available:**
- Whether any of the 16 Swift files actually compiles.
- Whether the Swift module name really is `dhruv` (assumed from the target
  name — the `@testable import dhruv` in both test files depends on this).
- Whether `@Observable`, `Task.sleep(for:)`, the two-parameter `.onChange`,
  switch-as-expression, and the async `LAContext.evaluatePolicy` are all
  available and behave as expected at the project's actual configured Swift
  language version (should be fine for iOS 26.2 / recent Xcode, but
  unconfirmed against the real toolchain).
- SF Symbol names used (`faceid`, `touchid`, `chart.line.uptrend.xyaxis`,
  `waveform.path.ecg`, `chart.line.flattrend.xyaxis`, `wifi.exclamationmark`,
  `checkmark.seal`) against the actual SDK's symbol catalogue.
- The `dhruvTests` files won't even be picked up by Xcode until a Unit
  Testing Bundle target is created (see INTEGRATION.md §5) — until then
  they're inert source.

**The actual finish line, per the project's own working agreement (from
`claude/xcode-26-setup-intel-mac.md`, referenced at the top of the original
plan): `xcodebuild -project dhruv.xcodeproj -scheme dhruv -destination
'generic/platform=iOS' -configuration Debug build` exits 0.** Nothing before
that should be reported as "done."

## 7. Required manual steps before that build can succeed

These are things no amount of Swift-writing from a sandbox can do —
they're either GUI-only Xcode settings or decisions only the project owner
can make:

1. **Merge `ios-port/dhruv/` and `ios-port/dhruvTests/` into the real
   project** at `~/personal/HelloWorld/dhruv/`. The target uses
   `PBXFileSystemSynchronizedRootGroup`, so new `.swift` files under
   `dhruv/` are picked up automatically — no `.pbxproj` editing needed for
   the app target. `dhruvTests/` is a new folder; see step 4.
2. **Wire the entry point.** Wherever the existing `WindowGroup` shows
   `ContentView()` (in `dhruvApp.swift`, typically), change it to
   `AppLockView()`. This session never saw the existing `ContentView.swift`
   or `dhruvApp.swift` and deliberately did not guess at overwriting them.
3. **Add `NSFaceIDUsageDescription`.** iOS requires this before any
   `LAContext.evaluatePolicy` call involving biometrics — without it, the
   app **crashes at runtime** (not build time) the instant Face ID is
   invoked, which makes it easy to miss until real device testing. Xcode →
   target `dhruv` → Build Settings → search "Face ID" → set "Privacy - Face
   ID Usage Description" to something like "Face ID keeps your watchlist and
   API token locked when you're not using the app." (The project generates
   Info.plist from build settings, matching the `INFOPLIST_KEY_*` pattern
   already used for `UIBackgroundModes` per the original plan — so this goes
   in Build Settings, not a hand-edited plist file.)
4. **(Recommended, matches the plan's own Phase 5) Create a Unit Testing
   Bundle target** — File → New → Target → Unit Testing Bundle, name it
   `dhruvTests`, host application `dhruv`. Then add the two files under
   `dhruvTests/` to that target.
5. **Set `API_AUTH_TOKEN` on the EC2 box** if not already done (§3) — needed for
   the Settings tab's token-verify flow to succeed, though Price Outlook
   itself works without it.
6. **Confirm `AppConfig.baseURL`** (`http://52.62.37.4:8080`)
   is still the correct, current server URL.

## 8. Standing conventions from the Tennis-bet side of this project, worth carrying over

These aren't strictly iOS concerns but shaped everything written above and
are worth knowing if this session also touches the Python backend:

- **"Always merge main"** — a standing instruction from the project owner
  earlier in this history: after committing to a feature branch, merge to
  `main` and push both, so `main` never drifts behind. The backend commits
  referenced in §3 were done this way (branch `claude/debug-previous-session-4wNw3`
  merged into `main`, both pushed).
- **Comments explain WHY, never WHAT** — every file in this project
  (Python and now Swift) follows this house style: no comment restating
  what a line of code obviously does; comments exist only for non-obvious
  constraints, a specific bug a piece of code prevents, or a trade-off that
  isn't visible from the code alone. Match this style in any further work.
- **Never commit without being asked**, and prefer new commits over
  amending. Standard git discipline, stated explicitly in this project's
  working instructions.
- **Test with real captured data, not hand-typed guesses at a shape.** This
  project has been bitten twice by parsers that silently accepted the wrong
  shape (a rolling 24h volume read as a 1-minute figure; 15-minute candles
  loaded into a 1-minute series — both "worked" and were wrong). The iOS
  decode test follows this same discipline with a real captured
  `/api/predict` response rather than a hand-typed JSON literal.

## 9. Suggested next actions, roughly in order

1. Confirm this session has access to `~/personal/HelloWorld/dhruv` and to
   the `dhruvparekhdp/Tennis-bet` repo (clone/pull it somewhere to read
   `ios-port/` if the zip isn't handy, or if it's stale).
2. Do the merge described in §7.1.
3. Do the two manual Xcode steps (§7.2, §7.3) — these block any successful
   build regardless of Swift correctness.
4. Run the `xcodebuild` command from §6. Fix whatever it reports. This is
   expected to take real iteration — the Swift was reviewed, not compiled.
5. Once it builds, run on a simulator or device, exercise Face ID (or the
   simulator's Face ID menu simulation), confirm Price Outlook renders
   against the live backend.
6. Set up the test target (§7.4) and run `xcodebuild test` if time allows.
7. Report back (or continue independently) — either way, update this file
   or leave a note of what was fixed, so the record stays accurate for
   whoever's next.
