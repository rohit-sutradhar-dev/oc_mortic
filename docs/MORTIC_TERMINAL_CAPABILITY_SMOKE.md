# Mortic Terminal Capability Smoke

Status: MOR-165 complete
Date: 2026-07-03
OpenCode checked locally: 1.17.13
Source: `docs/MORTIC_LAUNCH_READINESS_REVIEW.md` platform capabilities section

## Purpose

This smoke confirms the native sidepod entrypoint and key handling behavior that later Platform tickets depend on:

- `/mortic` focuses the sidepod and is not sent as a model prompt.
- Mortic focus mode prevents printable-key leakage into the OpenCode prompt.
- `M` push-to-talk works reliably as a plain toggle in every terminal.

## Instrumented Build

The sidepod source registers:

- `mortic.focus` with a flat `slashName: "mortic"` on an unpinned palette layer (also bound to `ctrl+x v`).
- `mortic.ptt.press` for `M` in `mortic.sidepod` mode — a plain toggle, no release handling.

Smoke diagnostics (prefix `[mortic smoke]`) are emitted only when `MORTIC_SMOKE_LOG=<path>` is set; they append durably to that file — console output in a raw TUI is painted over by screen redraws and never reaches `opencode.log`.

## Slash Registration Root Cause (2026-07-02)

`/mortic` initially showed `No matching items` in live OpenCode 1.17.13. Two stacked causes, both fixed and locked in by a package regression test:

1. The prompt slash menu only lists layer commands carrying a flat `slashName` string. The nested `slash: { name }` shape is honored only by the deprecated `api.command` adapter, not by `api.keymap.registerLayer`.
2. The slash menu queries `getCommandEntries({ visibility: "reachable", namespace: "palette" })`, and a layer pinned to `mode: "base"` is not reachable from the prompt. Internal OpenCode plugins register palette commands on unpinned layers; the sidepod now does the same. Only the focus-mode layer pins `mode: "mortic.sidepod"`.

## Typing Lock Root Cause (2026-07-02)

The first human run found typing leaking into the OpenCode prompt during focus mode. Pushing the `mortic.sidepod` mode scopes keymap bindings, but the prompt input keeps renderable focus, so unbound printables still landed in it. Fixed two ways: `focusMortic` parks `renderer.currentFocusedRenderable` (blur, restore on exit), and a prepended global `keypress` guard calls `preventDefault`/`stopPropagation` on unbound, unmodified keys while focus mode is active (renderable handlers skip defaultPrevented events; ctrl/meta chords and Mortic's own keys pass through). This remains unchanged through every later PTT iteration.

## PTT Interaction History (2026-07-02 to 2026-07-03)

Several PTT models were built and machine-verified in sequence before landing on the shipped one:

1. **Hold-to-talk via keymap `eventType: "release"` binding** — dead code. Keymap bindings only ever dispatch on key press.
2. **Hold-to-talk via `renderer.keyInput` `keyrelease` event** — worked against an emulated Kitty PTY, but a live run showed real terminals (Ghostty, then confirmed in iTerm2) never actually deliver a release for a plain unmodified key.
3. **Root cause traced into `@opentui/core`**: OpenCode's own `renderer.useKittyKeyboard = true` only requests Kitty flags `DISAMBIGUATE(1) | ALTERNATE_KEYS(4)`, never `EVENT_TYPES(2)` — the bit that makes a terminal report press vs. release at all. A fix that made the plugin request `EVENT_TYPES` itself (`enableKittyKeyboard(1|2|4)` on focus, restored on blur) was built and verified against the mechanism, but a live iTerm2 re-test showed iTerm2 accepts the flag push (`ESC[>7u`, captured directly in a PTY) and still never sends a release for plain keys — a terminal limitation, not a bug.
4. **Adaptive hold-with-tap-fallback**, auto-detected per session from the first observed key release, with a 1500ms repeat-debounce window. Machine-verified in both terminal classes. Rolled back by product decision: too much machinery for the value delivered.
5. **Tap-to-talk / tap-to-stop with repeat debounce** — still too much (a timing window, an implicit distinction between "repeat" and "deliberate second press").
6. **Shipped: plain `M` toggle.** No key-release handling, no Kitty flag changes, no repeat debounce, no timing windows, no event-type logic. Every `M` press flips `m armed` ↔ `m stopped`. Known, accepted trade-off: physically holding `M` down lets OS key repeat toggle the state repeatedly — the intended gesture is a tap, not a hold.

Machine-verified in a live session (PTY, `MORTIC_SMOKE_LOG` sink): `m-arm → m-stop → m-arm → m-stop`, one flip per press, identical in every terminal since there is no terminal-dependent logic left.

**PTT interaction decision for MOR-92/93/94**: plain `M` press toggle. UI copy: `Push-to-talk on. Tap M again to stop.` Hold-to-talk and repeat debouncing are documented above as options if ever revisited, along with why each was rolled back.

## Local Evidence

- `opencode --version` returned `1.17.13`.
- PTY-driven live TUI probes (machine-run):
  - `/mortic` appears in the slash menu and runs without sending a model prompt.
  - Typing lock: printable probe text does not reach the prompt in focus mode; typing works again after `Esc`.
  - `M` toggles cleanly: `m-arm → m-stop → m-arm → m-stop`, one state flip per press, no repeat flicker in the toggle logic itself.
- Live human diagnostic in iTerm2 (`MORTIC_SMOKE_LOG` sink) confirmed `useKittyKeyboard: true`, zero release events for plain keys, and key repeat arriving as plain presses — the evidence behind rolling back every release-dependent model above.
- `npm run check` (7 tests) locks the slash reachability rules, the typing-lock guards, and the absence of release/Kitty-flag/debounce machinery in `src/` (the package ships `src/` directly; there is no build step or `dist/`).
- `uv run pytest` (27 tests) remains the repo-wide gate after the sidepod package check.

## 10-Minute Human Checklist

1. From `opencode_mercury_sidepod/`, run `npm run check`.
2. Install the package with `opencode plugin "file:/absolute/path/to/opencode_mercury_sidepod" --global --force`.
3. Start OpenCode in the terminal being tested with the smoke sink enabled, from inside a session (`opencode --continue`):

   ```bash
   MORTIC_SMOKE_LOG=$HOME/mortic-smoke.log opencode --continue
   ```

   Inspect afterwards with `cat ~/mortic-smoke.log`.
4. Type `/mortic`.
5. Confirm the Mortic sidepod focuses, the command is not sent as a model prompt, and the smoke log records a `focus` event.
6. While Mortic focus mode is active, type printable probe text such as `abcxyz`.
7. Confirm the probe text does not appear in the OpenCode prompt. If it leaks, record the terminal and details here.
8. Tap `M`, confirm the command deck shows `ARMED`. Tap `M` again, confirm it shows `OFF`.
9. Press `Esc` and confirm focus returns to the OpenCode prompt.

## Terminal Matrix

PTT is a plain toggle with no terminal-dependent logic, so the matrix is a record of what led to that decision rather than a per-terminal gate.

| Terminal | Result | Notes |
| --- | --- | --- |
| PTY probe (machine) | Pass, 2026-07-03 | `m-arm → m-stop → m-arm → m-stop`. |
| iTerm2 3.5+ | Confirmed live, 2026-07-02/03 | Root-cause terminal for the release investigation: accepts Kitty flag pushes but never sends release events for plain keys. |
| macOS Terminal.app | Confirmed live, 2026-07-02 | No Kitty protocol support at all. |
| Alacritty / Ghostty / Kitty / WezTerm | Not separately tested | Behavior is identical by design — there is no per-terminal branch in the code. |

## Completion Rule

The three product-critical capabilities are confirmed: `/mortic` slash entry (machine-verified), typing lock (machine-verified), and the plain `M` toggle (machine-verified, and matches what a human confirmed live in iTerm2 and Terminal.app). MOR-165 is complete.
