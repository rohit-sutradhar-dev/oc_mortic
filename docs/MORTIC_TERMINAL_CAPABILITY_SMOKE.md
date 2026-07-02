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

- `mortic.slash` with `slash: { name: "mortic" }`.
- `mortic.ptt.press` for `M` press in `mortic.sidepod` mode.
- `mortic.ptt.release` for `M` release in `mortic.sidepod` mode.

Smoke hooks emit structured diagnostics with the prefix `[mortic smoke]`. Each focus/key diagnostic includes `useKittyKeyboard`, current mode, event name, and key event type where available.

## Local Evidence

- `opencode --version` returned `1.17.11`.
- Installed plugin type surface exposes `TuiCommand.slash?: { name, aliases }`.
- `npm run check` verifies source and generated `dist/` include the slash and terminal smoke hooks.
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
| Ghostty | Hold-`M` | `true` | Pending | Verify press and release diagnostics. |
| Kitty | Hold-`M` | `true` | Pending | Verify press and release diagnostics. |
| WezTerm | Hold-`M` | `true` | Pending | Verify press and release diagnostics. |
| iTerm2 3.5+ | Hold-`M` | `true` | Pending | Kitty keyboard protocol must be enabled by terminal/app version. |
| Alacritty 0.13+ | Hold-`M` | `true` | Pending | Verify press and release diagnostics if available locally. |
| macOS Terminal.app | Tap fallback | `false` | Pending | Release diagnostics are not expected; M press should toggle tap mode. |

## Completion Rule

MOR-165 should remain In Progress until at least one Kitty-protocol terminal and macOS Terminal.app are run by a human and the matrix above is updated with observed results.
