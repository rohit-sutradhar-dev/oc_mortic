# Mortic OpenCode Voice

Mortic is an OpenCode voice sidepod prototype. The repo now focuses on:

- `opencode_voice/`: Python/FastAPI voice bridge for OpenCode, Mercury/Inception, and Deepgram.
- `opencode_mercury_sidepod/`: native OpenCode TUI sidepod proof.
- `docs/MORTIC_OPENCODE_SIDEPOD_PRD.md`: current product requirements draft.
- `docs/MORTIC_CURRENT_CODE_INVENTORY.md`: shared inventory of reusable code, reference-only pieces, ownership mapping, and latency-sensitive paths.
- `MORTIC_PLUGIN_HANDOFF.md`: implementation handoff notes connecting the bridge and sidepod.


## Setup

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Required local environment variables for the current voice bridge:

```bash
export INCEPTION_API_KEY="..."
export DEEPGRAM_API_KEY="..."
```

## Run The Helper

```bash
source .venv/bin/activate
mortic-helper --managed-opencode
```

`--managed-opencode` starts a clean `opencode serve` process with a runtime config overlay for the current voice model. If a running OpenCode server is detected, managed mode borrows that server's project directory so the clean server can still see the same threads.

Useful options:

```bash
mortic-helper --help
mortic-helper --managed-opencode --opencode-dir "/path/to/project"
mortic-helper --context-threshold 70000 --model-variant low
mortic-helper --eager-eot-threshold 0.7   # default 0.6; pass 0 to disable
mortic-helper --voice-duplex full          # headphones: skip echo protection
mortic-helper --barge-in-confirm-sec 2.0  # pause window while a mid-playback voice confirms
mortic-helper --barge-in-min-chars 4      # shorter transcripts resume playback instead
mortic-helper --playback-mute-sec 0.6     # STT deaf window at each playback start; 0 disables
```

## Echo Protection

The native lane echo-cancels the microphone the way a browser does: WebRTC's
audio processing module (prebuilt in the `livekit` wheel) runs on the capture
path with TTS playback fed in as the render reference, so the assistant never
hears itself and voice barge-in stays usable on open speakers.
`--voice-duplex` controls the behavior: `auto` (default) uses the echo
canceller and degrades to a half-duplex silence gate if the native module is
unavailable, `full` passes raw mic audio (headphone users), `half` forces the
gate (mute key interrupts while the assistant speaks).

Two behaviors keep the loop stable on open speakers. A voice detected while
the assistant is audible **pauses** playback rather than killing the turn;
a real transcript within `--barge-in-confirm-sec` commits the interruption,
anything shorter than `--barge-in-min-chars` (echo residue, stray noise)
resumes playback where it left off. And when Flux fires an eager end-of-turn
followed by the confirming final one for the same words, the final confirms
the already-running turn instead of restarting it.

Turns stream from OpenCode's `/event` feed scoped to the fork's directory
(forks inherit the source thread's directory; an unscoped subscription never
sees their events and every turn would pay the poll-fallback timeout).

## Helper Distribution Contract

The v1 helper distribution target is the `mortic-helper` Python package. The owner publishes to PyPI; local verification uses a built wheel or sdist.

```bash
uvx --from dist/mortic_helper-0.1.0-py3-none-any.whl mortic-helper --help
python -m venv /tmp/mortic-helper-venv
/tmp/mortic-helper-venv/bin/pip install dist/mortic_helper-0.1.0-py3-none-any.whl
/tmp/mortic-helper-venv/bin/mortic-helper --help
```

Platform should launch or discover the helper on `127.0.0.1:8765` unless overridden (`MORTIC_HELPER_URL`). Readiness is `GET /api/health`; a ready helper returns `ready: true`, otherwise it reports `Voice Bridge Issue` details. Nothing else in the health payload gates readiness — externally started helpers are first-class. The sidepod v0 control/event transport is `ws://127.0.0.1:8765/ws/sidepod` and starts with a `start` command carrying `protocolVersion: "mortic.sidepod.v0"`. The helper responds with `ready` carrying the same protocol version before the sidepod treats the lane as connected.

The shipped plugin launcher (2026-07-04) resolves the helper in this order:

1. `MORTIC_HELPER_CMD` — explicit command override (quote paths containing spaces).
2. `<repo>/.venv/bin/mortic-helper` — repo-checkout dev install (spawned from the repo root so `.env` BYOK loading and `runs/` behave).
3. `uv run --project <repo> mortic-helper` — repo checkout without a `.venv`.
4. `uvx mortic-helper` — the published package.

Plugin-spawned helpers receive `--no-managed` and the focused thread's OpenCode server URL via `OPENCODE_VOICE_OPENCODE_URL` (recorded from the plugin host's `serverUrl` as `MORTIC_OPENCODE_SERVER_URL`; an explicit user `OPENCODE_VOICE_OPENCODE_URL` wins). `start` additionally carries `opencodeUrl` so the engine forks on the server that owns the thread. With `--no-managed` and no reachable server the helper exits instead of starting a shadow OpenCode server. `MORTIC_HELPER_LOG` captures spawned-helper output; `MORTIC_HELPER_EVENT_LOG` mirrors the engine's redacted event log for diagnostics.

No PyInstaller or app bundle is part of v1; the terminal process that launches `mortic-helper` keeps macOS mic permission attribution clear. Local keys stay in environment variables or `.env`; packages must not contain secrets.

## Native Sidepod Reference

Install the local sidepod plugin into OpenCode:

```bash
opencode plugin "file:/absolute/path/to/opencode_mercury_sidepod" --global --force
```

The current sidepod is a native TUI proof. The PRD describes the intended packaged product direction: `/mortic` focuses the sidebar, the command deck exposes PTT/Live/Refresh/C Config/Transcript/Handoff, and an invisible local helper owns mic capture, STT, TTS, OpenCode fork turns, barge-in, and speech filtering.

## Tests

```bash
uv run pytest
```

## Repo Notes

- `.env`, `.venv/`, run logs, local data, `node_modules/`, and `_not_needed_for_push/` are ignored.
- The source OpenCode thread should remain untouched; voice work happens in ephemeral forks.
- The current PRD is the source of truth for sidepod behavior before the next implementation pass.
