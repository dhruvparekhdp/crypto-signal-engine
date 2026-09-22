# Integrating this into your Xcode project

**Written and reviewed by hand — not build-verified.** This was produced in a
Linux cloud sandbox with no macOS, no Xcode, no Swift toolchain, and no
access to `~/personal/HelloWorld/dhruv` on your Mac (that path only exists on
your machine). I could not run `xcodebuild` or the tests. Every file was
checked carefully for correctness — types, protocol conformances, control
flow, async/await usage, the exact JSON shape it decodes — but "carefully
reviewed" is not the same guarantee as "compiles". Run the build command at
the bottom before trusting any of this, and if anything doesn't compile,
that's expected friction, not a sign the whole approach is wrong — most
likely a small naming or availability mismatch.

## What this is

Phase 1 (Foundation) + a Face ID lock screen + Phase 0's first recommended
feature (Price Outlook — see `PORT_NOTES.md` §10.4 for why that one first).
Not the full app: Signals, Watchlist, Accuracy and Paper Trading are present
as tab stubs that say "coming soon" and point at the web equivalent, so the
tab bar is honest about what's real.

**Architecture decision made here, worth knowing:** the iOS app is a client
of your existing Python backend (`GET /api/predict`, `POST /api/auth/verify`)
rather than a from-scratch reimplementation of the forecast maths in Swift.
The confluence voting, the cost model, and every hard-won fix in this
project's history already live server-side, tested, in one place —
duplicating that in Swift would create two implementations that drift apart
the first time either one changes. If you'd rather the app compute forecasts
on-device and talk to Binance directly (matching the original Phase 0 mapping
table), that's a real fork in the design — say so and the port continues down
that path instead.

## 1. Copy the files in

Unzip and drop the `dhruv/` folder's contents into your project's `dhruv/`
folder (merge, don't replace — it should only ADD new files: `Models/`,
`Services/`, `Features/`, `App/`). Because the target uses
`PBXFileSystemSynchronizedRootGroup`, Xcode picks up every new `.swift` file
under `dhruv/` automatically — no `.pbxproj` editing, nothing to hand-add in
the file inspector.

## 2. Wire it into your existing entry point

I don't have your current `ContentView.swift` or `dhruvApp.swift` — I never
overwrite files I haven't seen. Wherever your `WindowGroup` currently shows
`ContentView()` (in `dhruvApp.swift`, typically), change the root view to
`AppLockView()`:

```diff
 var body: some Scene {
     WindowGroup {
-        ContentView()
+        AppLockView()
     }
 }
```

If you'd rather keep `ContentView` as the entry point and have IT show the
lock screen, that works too — just make `ContentView`'s body `AppLockView()`
instead. Either is fine; pick whichever fits how the rest of the file is
structured.

## 3. Add the Face ID usage string — REQUIRED, or Face ID crashes the app

iOS requires an `NSFaceIDUsageDescription` before any app may call Face ID —
without it, the very first `evaluatePolicy` call crashes at runtime, not at
build time, so this is easy to miss until you actually test on a device.

Per the plan's `INFOPLIST_KEY_UIBackgroundModes` pattern, your project
generates its Info.plist from build settings rather than a physical file, so
add the matching key there instead of hand-editing `project.pbxproj`:

**Xcode → target `dhruv` → Build Settings → search "Face ID"** → set
**"Privacy - Face ID Usage Description"** to something like:

> Face ID keeps your watchlist and API token locked when you're not using the app.

## 4. Point it at your server

`dhruv/App/AppConfig.swift` hardcodes `http://52.62.37.4:8080`
as the backend — that's the server URL referenced throughout this project's
history. Confirm it's still correct; change the one line if not.

## 5. (Optional, recommended by the plan's own Phase 5) Set up the test target

`dhruvTests/` has two Swift Testing files — one decodes a REAL captured
`/api/predict` response (guards against the exact "parser silently accepts
the wrong shape" bug class that hurt this project twice on the Python side),
the other exercises `PriceOutlookViewModel`'s pure logic against a fake
client, no network involved. They will not run — Xcode won't even see them
— until a Unit Testing Bundle target exists:

**File → New → Target → Unit Testing Bundle**, name it `dhruvTests`, make
sure it targets `dhruv` as the host application. Then add
`dhruvTests/*.swift` to that new target (drag them in, or move the folder
under the new target's synchronized group if Xcode created one).

## 6. Set the API token (needed for anything beyond reading)

On your EC2 deployment, set the `API_AUTH_TOKEN` environment variable if
you haven't already (see the backend commit that added `scheduler/security.py`
for what it protects: settings toggles and watchlist edits — Price Outlook
itself needs no token). Enter that same value once in the app's Settings tab;
it's verified against `/api/auth/verify` before being stored, then held in
Keychain behind Face ID.

## 7. Build

```bash
cd ~/personal/HelloWorld/dhruv
xcodebuild -project dhruv.xcodeproj -scheme dhruv \
  -destination 'generic/platform=iOS' -configuration Debug build
```

Per the plan's own working agreement, this is the actual finish line — not
my say-so from a sandbox that has never run this compiler.

## What I could not verify from here (be extra alert for these)

- Whether your Swift module name really is `dhruv` (assumed from the target
  name for the `@testable import dhruv` in the test files — Xcode defaults
  the module name to the target name, but if yours was overridden, fix that
  one import line).
- Whether `@Observable`, `Task.sleep(for:)`, the two-parameter `.onChange`,
  and `switch`-as-expression are all available at your actual configured
  Swift language version. They're standard as of Swift 5.9+/iOS 17+, which
  your iOS 26.2 deployment target comfortably exceeds — but I could not
  compile a single line to confirm it against your exact toolchain.
- SF Symbol names used (`faceid`, `touchid`, `chart.line.uptrend.xyaxis`,
  `waveform.path.ecg`, `chart.line.flattrend.xyaxis`, `wifi.exclamationmark`,
  `checkmark.seal`) — all real symbols as of recent SF Symbols versions, not
  checked against your specific SDK's symbol catalogue.

If the build fails, paste the exact error back and it gets fixed — that's a
much more useful next step than guessing further from a sandbox that can't
run the compiler.
