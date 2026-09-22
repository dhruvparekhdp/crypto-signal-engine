# Paste this into the Claude session running on the Mac

Everything below the line is meant to be copied as one message into a Claude
Code session that has real filesystem and Xcode access — i.e. one running
locally, not a remote sandbox. It was written by a prior session that had
neither, so nothing in this project's iOS side has been build-verified yet.

---

I'm continuing an iOS port that was prepared in a remote session with no
Xcode access. Your job is to get it from "written but unverified" to
"builds and runs." Full background is in two files — **read both before
doing anything else**:

1. `ios-port/SESSION_CONTEXT.md` in the `dhruvparekhdp/Tennis-bet` GitHub
   repo (branch `main`) — the full record of what was decided, what was
   built, and exactly what is and isn't verified.
2. `ios-port/INTEGRATION.md` in the same location — step-by-step merge and
   build instructions.

Also skim `PORT_NOTES.md` at the repo root — the original Phase 0 inventory
of the Python backend this app talks to, including why certain design
choices were made (particularly §9 on the notification design, and §10.4 on
why Price Outlook was ported before the signal engine).

## What to actually do, in order

1. **Get the source.** Either `git clone` (or `git pull` if it's already
   present somewhere) `https://github.com/dhruvparekhdp/Tennis-bet` to read
   the `ios-port/` folder, or unzip `dhruv-ios-port.zip` if it's sitting in
   Downloads — same contents either way. `ios-port/` contains a `dhruv/`
   folder and a `dhruvTests/` folder that mirror the Xcode project's own
   layout.

2. **Confirm the target project.** The actual Xcode project is at
   `~/personal/HelloWorld/dhruv` — a separate git repo from Tennis-bet, its
   own history, do not confuse the two. Bundle ID `dp.dhruv`, target
   `dhruv`, deployment target iOS 26.2. Run `git status` in that repo before
   changing anything, so any of the owner's in-progress work is visible
   before you touch it.

3. **Merge the new source in.** Copy `ios-port/dhruv/*` into
   `~/personal/HelloWorld/dhruv/dhruv/` and `ios-port/dhruvTests/*` into
   `~/personal/HelloWorld/dhruv/dhruvTests/` (new folder). This should be
   purely additive — new files under `Models/`, `Services/`, `Features/`,
   `App/`. If any filename collides with something already in the project,
   stop and check with the owner before overwriting; this session never saw
   the existing project and can't know if that's expected.

4. **Wire the entry point.** Find wherever the app's `WindowGroup` currently
   shows `ContentView()` (likely in `dhruvApp.swift`) and change it to
   `AppLockView()`. Read that file first — don't guess blind.

5. **Add the Face ID usage string.** In Xcode, target `dhruv` → Build
   Settings → search "Face ID" → set "Privacy - Face ID Usage Description".
   Skipping this makes the app crash at runtime (not build time) the moment
   Face ID is invoked, which is easy to miss.

6. **Build it:**
   ```
   cd ~/personal/HelloWorld/dhruv
   xcodebuild -project dhruv.xcodeproj -scheme dhruv \
     -destination 'generic/platform=iOS' -configuration Debug build
   ```
   Fix whatever it reports. Expect real iteration here — every file was
   reviewed by hand, none of it has been through a Swift compiler. Common
   likely issues to check first if something fails: the assumed Swift
   module name is `dhruv` (used in `@testable import dhruv` in the test
   files — fix that import if the real module name differs), and SF Symbol
   names that may not exist in the exact SDK version.

7. **Once it builds:** run it on a simulator, exercise the Face ID lock
   screen (the simulator has a Face ID menu to simulate success/failure),
   and confirm the Price Outlook tab loads real data from
   `http://52.62.37.4:8080/api/predict` (no auth needed for
   that call — it's a public read endpoint). Try the Settings tab's token
   field too, but note it needs `API_AUTH_TOKEN` set on the EC2
   deployment first, which may not have happened yet — check with the owner
   if it 401s or 503s.

8. **Set up the test target if you have time** — File → New → Target → Unit
   Testing Bundle named `dhruvTests`, host app `dhruv`, add the two files
   under `dhruvTests/` to it, run `xcodebuild test` with the same
   destination as step 6.

## Ground rules, carried over from the Python side of this project

- Never commit without being asked first.
- When you do commit, create new commits rather than amending, and never
  force-push.
- Comments in this codebase explain WHY, never WHAT — no restating what a
  line obviously does. Match that style in anything you add or change.
- If you find yourself wanting to change the core design (e.g. reverting to
  an on-device forecast engine instead of calling the Python backend — see
  `SESSION_CONTEXT.md` §5 for why it's built the way it is), say so and ask
  first rather than silently reworking it — that's a real architectural
  fork, not a bug fix.

Start by reading the two files named at the top. Then work through the
numbered list. Report back what actually happened at each step — especially
the exact `xcodebuild` output if it fails — rather than summarising it as
"mostly working."
