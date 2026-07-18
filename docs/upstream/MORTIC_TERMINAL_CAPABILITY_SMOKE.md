# Mortic Terminal Capability Smoke

Status: MOR-165 complete
Date: 2026-07-03
OpenCode checked locally: 1.17.13
Source: `docs/MORTIC_LAUNCH_READINESS_REVIEW.md` platform capabilities section

## Purpose

This smoke confirms the native sidepod entrypoint and key handling behavior that later Platform tickets depend on:

- `/mortic` focuses the sidepod and is not sent as a model prompt.
- Mortic focus mode prevents printable-key leakage into the OpenCode prompt.
- `M` mutes/unmutes the mic reliably as a plain toggle in every terminal.

## Instrumented Build

The sidepod source registers:

- `mortic.focus` with a flat `slashName: "mortic"` on an unpinned palette layer (also bound to `ctrl+x v`). It refuses to engage off the session route (see "Focus Refusal Without a Session" below).
- `mortic.mic.toggle` for `M` in `mortic.sidepod` mode — a plain mute/unmute toggle, no release handling.

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
6. **Plain `M` toggle (shipped 2026-07-03, then superseded same day).** No key-release handling, no Kitty flag changes, no repeat debounce, no timing windows, no event-type logic. Every `M` press flipped `m armed` ↔ `m stopped`. Known, accepted trade-off: physically holding `M` down lets OS key repeat toggle the state repeatedly — the intended gesture is a tap, not a hold.
7. **Superseded: PTT retired, merged with Live into a single mic mute/unmute (owner decision 2026-07-03).** Live human testing showed the tap-toggle PTT model had degenerated into "toggle listening" — functionally identical to the separate Live control, just with different labels. Both were collapsed into one state bit: mic muted or mic live. Turn segmentation (where a spoken turn starts/stops) is left to the engine's own end-of-turn detection; the UI's only job is gating whether the mic may listen at all. `M` is the sole control. The tap-toggle mechanics carry over unchanged (still just flipping one boolean per press); only the framing, the emitted protocol event (`live.set` instead of `ptt.start`/`ptt.stop`), and the deck copy changed. Hold-to-talk PTT stays documented above as an option if ever revisited.

Machine-verified in a live session (PTY, `MORTIC_SMOKE_LOG` sink): unmute → mute → unmute → mute, one flip per press, identical in every terminal since there is no terminal-dependent logic left.

**Mic interaction decision for MOR-92/93/94**: plain `M` press toggle, mute by default on focus. UI copy: `Mic is live. Speak normally.` / `Mic is muted. Tap M to talk.` Hold-to-talk and repeat debouncing are documented above as options if ever revisited, along with why each was rolled back. `ptt.start`/`ptt.stop` remain defined in `docs/MORTIC_PROTOCOL_V0.md` for that future option; the UI no longer emits them.

## Focus Refusal Without a Session (2026-07-03)

Live testing found `/mortic` silently locking the keyboard when triggered before any OpenCode session was open: `sidebar_content` only mounts on the session route (it requires a `session_id`), so focus mode engaged — mode push, prompt blur, typing-lock guard armed — against a sidepod that never rendered, with zero explanation. `focusMortic` now checks `api.route.current.name === "session"` first. Off that route it refuses entirely (no mode push, no lock) and shows an `api.ui.toast` explaining why; a `focus.blocked` smoke event records it.

## Typing-Lock Visibility (2026-07-03)

A second live-testing finding: even inside a session, the typing lock gave no indication it was active — worse if the sidebar panel is collapsed, since the plugin has no API to detect or force panel visibility (`TuiApp` exposes only `{version}`). First fix was a pair of `api.ui.toast` calls (one on focus, one on the first swallowed keystroke per session). Owner asked whether the indication could instead live in the prompt row itself, persistent while focused and cleared on exit. Verified live via PTY that OpenCode's `session_prompt_right` host slot renders plugin content directly beside the prompt row (next to the model/agent status text) — so the sidepod now registers a `session_prompt_right` slot (`renderPromptAnnex`) built from the same state used by `sidebar_content`:

- Focused: `MORTIC · MIC MUTED — Esc exit` or `MORTIC · MIC LIVE — Esc exit`, colored by mic state.
- Unfocused: empty.

This replaced both toasts — a reactive slot is simpler than a toast plus a one-shot-notice flag, and it satisfies "immutable until exit, then flushed" for free: the slot re-renders off the same `focused`/`micLive` signals, with no timer and no manual clear code. The hero caption also stays state-true instead of a static label: unfocused shows `/MORTIC TO FOCUS`; focused shows `MIC MUTED · M TO TALK` or `MIC LIVE · M TO MUTE`.

## Local Evidence

- `opencode --version` returned `1.17.13`.
- PTY-driven live TUI probes (machine-run):
  - `/mortic` appears in the slash menu and runs without sending a model prompt.
  - Typing lock: printable probe text does not reach the prompt in focus mode; typing works again after `Esc`.
  - `/mortic` with no session open: focus refused, `focus.blocked` smoke event recorded, probe text still reaches the prompt (no lock engaged).
  - `M` toggles cleanly: unmute → mute → unmute → mute, one state flip per press, no repeat flicker in the toggle logic itself; each press emits a `live.set` protocol send.
  - The prompt-row annex is absent before focus, reads `MORTIC · MIC MUTED — Esc exit` right after focus, flips to `MIC LIVE` on unmute and back on mute, and disappears the instant the session ends.
- Live human diagnostic in iTerm2 (`MORTIC_SMOKE_LOG` sink) confirmed `useKittyKeyboard: true`, zero release events for plain keys, and key repeat arriving as plain presses — the evidence behind rolling back every release-dependent model above.
- `npm run check` (12 tests) locks the slash reachability rules, the typing-lock guards, the mic-toggle protocol contract, and the absence of release/Kitty-flag/debounce/PTT machinery in `src/` (the package ships `src/` directly; there is no build step or `dist/`).
- `uv run pytest` (37 tests) remains the repo-wide gate after the sidepod package check.

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
8. Tap `M`, confirm the command deck shows `LIVE`. Tap `M` again, confirm it shows `MUTED`.
9. Press `Esc` and confirm focus returns to the OpenCode prompt.

## Terminal Matrix

The mic control is a plain toggle with no terminal-dependent logic, so the matrix is a record of what led to that decision rather than a per-terminal gate.

| Terminal | Result | Notes |
| --- | --- | --- |
| PTY probe (machine) | Pass, 2026-07-03 | Unmute → mute → unmute → mute. |
| iTerm2 3.5+ | Confirmed live, 2026-07-02/03 | Root-cause terminal for the release investigation: accepts Kitty flag pushes but never sends release events for plain keys. |
| macOS Terminal.app | Confirmed live, 2026-07-02 | No Kitty protocol support at all. |
| Alacritty / Ghostty / Kitty / WezTerm | Not separately tested | Behavior is identical by design — there is no per-terminal branch in the code. |

## Completion Rule

The three product-critical capabilities are confirmed: `/mortic` slash entry (machine-verified, including refusal without a session), typing lock (machine-verified, now with on-screen and toast indication), and the plain `M` mic toggle (machine-verified, and matches what a human confirmed live in iTerm2 and Terminal.app). MOR-165 is complete.
