# Mortic Terminal Capability Smoke

Status: MOR-165 instrumentation prepared; human terminal matrix pending
Date: 2026-07-02
OpenCode checked locally: 1.17.11
Source: `docs/MORTIC_LAUNCH_READINESS_REVIEW.md` platform capabilities section

## Purpose

This smoke confirms the native sidepod entrypoint and key handling behavior that later Platform tickets depend on:

- `/mortic` focuses the sidepod and is not sent as a model prompt.
- Mortic focus mode prevents printable-key leakage into the OpenCode prompt.
- Hold-`M` works in Kitty-protocol terminals when `renderer.useKittyKeyboard` is true.
- macOS Terminal.app degrades to tap-to-arm/tap-to-stop when key release is unavailable.

## Instrumented Build

The sidepod source now registers:

- `mortic.slash` with a flat `slashName: "mortic"` on an unpinned palette layer.
- `mortic.ptt.press` for `M` press in `mortic.sidepod` mode.
- `mortic.ptt.release` for `M` release in `mortic.sidepod` mode.

Smoke hooks emit structured diagnostics with the prefix `[mortic smoke]`. Each focus/key diagnostic includes `useKittyKeyboard`, current mode, event name, and key event type where available.

## Slash Registration Root Cause (2026-07-02 debug session)

`/mortic` initially showed `No matching items` in live OpenCode 1.17.13. Two stacked causes, both fixed and locked in by a package regression test:

1. The prompt slash menu only lists layer commands carrying a flat `slashName` string. The nested `slash: { name }` shape is honored only by the deprecated `api.command` adapter, not by `api.keymap.registerLayer`.
2. The slash menu queries `getCommandEntries({ visibility: "reachable", namespace: "palette" })`, and a layer pinned to `mode: "base"` is not reachable from the prompt. Internal OpenCode plugins register palette commands on unpinned layers; the sidepod now does the same. Only the focus-mode layer pins `mode: "mortic.sidepod"`.

## Typing Lock And M Release Root Cause (2026-07-02 live run)

The first human run found typing leaking into the OpenCode prompt during focus mode, and hold-M never releasing. Causes and fixes, verified in a PTY-driven live session with emulated Kitty key events:

1. **Typing leak**: pushing the `mortic.sidepod` mode scopes keymap bindings but the prompt input keeps renderable focus, so unbound printables still landed in it. Fixed two ways: `focusMortic` parks `renderer.currentFocusedRenderable` (blur, restore on exit), and a prepended global `keypress` guard calls `preventDefault`/`stopPropagation` on unbound, unmodified keys while focus mode is active (renderable handlers skip defaultPrevented events; ctrl/meta chords and Mortic's own keys pass through).
2. **M release never fired**: keymap bindings only dispatch on key press — the `{ key: "m", eventType: "release" }` binding was dead code. Release now comes from `api.renderer.keyInput.on("keyrelease", ...)`, which OpenTUI emits when the terminal reports Kitty event types.
3. **Escape hatch**: if the terminal claims Kitty support but never delivers release events, PTT would stick armed. A second M press now always stops PTT.

## Real-Terminal Tap Result Root Cause (2026-07-02 second live run)

The human matrix run recorded `Tap` in all three Kitty-protocol terminals tested (Ghostty, WezTerm, iTerm2 3.5+) — not the expected `Hold`. A temporary file-based diagnostic sink (not committed; console output in a raw TUI is painted over by screen redraws and never reaches `opencode.log`) confirmed the actual mechanism in Ghostty: `useKittyKeyboard: true`, repeated `raw.keypress` events for `m`, and **zero `raw.keyrelease` events**.

Traced into `@opentui/core`'s renderer (`node_modules/@opentui/core/index-6xr3rbbe.js`): the boolean setter `renderer.useKittyKeyboard = true`, which OpenCode's host calls at startup, only requests Kitty flags `DISAMBIGUATE(1) | ALTERNATE_KEYS(4)`. It never requests `EVENT_TYPES(2)` — the bit that makes a terminal distinguish press from release at all for a plain unmodified key. This was true regardless of terminal; every Kitty-capable terminal was starved of a request it never received, which is why the matrix showed `Tap` across the board rather than a per-terminal split.

Fix: `focusMortic` now calls `renderer.enableKittyKeyboard(DISAMBIGUATE | EVENT_TYPES | ALTERNATE_KEYS)` (flags `= 7`) itself when base Kitty support is present, and `blurMortic` restores the host default (`useKittyKeyboard = true`, flags `= 5`) so unrelated OpenCode keymaps outside Mortic focus mode are unaffected. The double-press escape hatch stays as a safety net for terminals that accept the flag request but still don't honor it.

This was verified against the mechanism (renderer API, flag semantics) and against the previously-passing emulated-Kitty PTY scenario, but **not yet against a real terminal** — the emulator can't fabricate what a real terminal would send in response to the escalated flag request. The terminal matrix below needs to be re-run once more against this build.

## Local Evidence

- `opencode --version` returned `1.17.13`.
- PTY-driven live TUI probes (machine-run, emulated Kitty CSI-u key events):
  - `/mortic` appears in the slash menu and runs without sending a model prompt.
  - Typing lock: printable probe text does not reach the prompt in focus mode; typing works again after `Esc`.
  - M press arms (`Hold-M push-to-talk is active.`), Kitty release stops (`Push-to-talk released.`, deck `OFF`), re-arm works, and a second press without release stops (`Push-to-talk stopped.`).
- `npm run check` (8 tests) locks the slash reachability rules, the keyrelease-listener mechanism, the typing-lock guards, and the Kitty `EVENT_TYPES` flag escalation in both `src/` and generated `dist/`.
- `uv run pytest` remains the repo-wide gate after the sidepod package check.

## 10-Minute Human Checklist

1. From `opencode_mercury_sidepod/`, run `npm run check`.
2. Install the package with `opencode plugin "file:/absolute/path/to/opencode_mercury_sidepod" --global --force`.
3. Start OpenCode in the terminal being tested.
4. Type `/mortic`.
5. Confirm the Mortic sidepod focuses, the command is not sent as a model prompt, and `[mortic smoke]` logs a `focus` event with `source: "slash"`.
6. While Mortic focus mode is active, type printable probe text such as `abcxyz`.
7. Confirm the probe text does not appear in the OpenCode prompt. If it leaks, record the terminal and mitigation as `prompt blur required`.
8. Hold `M`, then release it.
9. Confirm `[mortic smoke]` logs `ptt.key` with `eventType: "press"` and, in Kitty-protocol terminals, `eventType: "release"`.
10. Press `Esc` and confirm focus returns to the OpenCode prompt.

## Terminal Matrix

| Terminal | Expected Mode | Expected `useKittyKeyboard` | Human Result | Notes |
| --- | --- | --- | --- | --- |
| PTY probe (emulated Kitty, machine) | Hold-`M` | `true` | Pass 2026-07-02 | Press arms, release stops, double-press escape hatch stops. Re-verified after the flag-escalation fix. |
| Ghostty | Hold-`M` | `true` | Pending re-test | Was `Tap` pre-fix (flags never requested EVENT_TYPES). Re-run against the flag-escalation build. |
| Kitty | Hold-`M` | `true` | Pending | Verify press and release diagnostics. |
| WezTerm | Hold-`M` | `true` | Pending re-test | Was `Tap` pre-fix. Re-run against the flag-escalation build. |
| iTerm2 3.5+ | Hold-`M` | `true` | Pending re-test | Was `Tap` pre-fix. Re-run against the flag-escalation build. |
| Alacritty 0.13+ | Hold-`M` | `true` | Pending | Verify press and release diagnostics if available locally. |
| macOS Terminal.app | Tap fallback | `false` | Tap | Confirmed pre-fix; Terminal.app has no Kitty support so this result is unaffected by the fix. |

## Completion Rule

MOR-165 should remain In Progress until at least one Kitty-protocol terminal shows `Hold` against the flag-escalation build (Ghostty is the cheapest re-test since it was already exercised once) and macOS Terminal.app is confirmed `Tap`.
