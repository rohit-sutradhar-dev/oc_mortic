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

STT always runs on Deepgram Flux. TTS defaults to Deepgram but can be switched
to Cartesia with `--tts-provider cartesia` (needs `CARTESIA_API_KEY` set too):

```bash
export CARTESIA_API_KEY="..."  # only needed when --tts-provider cartesia
```

## Run The Helper

```bash
source .venv/bin/activate
mortic-helper --doctor            # step 1: gate the install before a session
mortic-helper --managed-opencode
```

`--doctor` diagnoses the install end-to-end and exits: OpenCode reachable, the
`voice-build` agent present on that server, a real model round-trip (the "pong"
test), and the LLM/STT/TTS keys with their source. A server missing
`voice-build` (the `--no-managed` plugin path against a plain `opencode serve`)
accepts turns then silently hangs — the doctor turns that into a loud FAIL. The
same reachable + agent check runs warn-only at every helper start.

`--managed-opencode` starts a clean `opencode serve` process with a runtime config overlay for the current voice model. If a running OpenCode server is detected, managed mode borrows that server's project directory so the clean server can still see the same threads. The managed Bun process receives its IPv4 preference through Bun's standalone-executable `BUN_OPTIONS` channel (not OpenCode CLI arguments), so an advertised-but-broken IPv6 route cannot silently stall Mercury; Python provider sockets use Happy Eyeballs and still retain IPv6 fallback. The helper pins Uvicorn to standard asyncio because uvloop does not accept Python's Happy Eyeballs connection options. Managed-child output is forwarded to the helper log (`MORTIC_HELPER_LOG`, default `/tmp/mortic-helper-plugin.log`) for startup diagnosis.

Useful options:

```bash
mortic-helper --help
mortic-helper --managed-opencode --opencode-dir "/path/to/project"
mortic-helper --context-threshold 70000 --model-variant low
mortic-helper --tts-provider cartesia     # switch TTS off Deepgram (STT stays on Flux)
mortic-helper --voice-duplex half          # explicit safety fallback / push-to-interrupt
mortic-helper --device-sample-rate 48000  # native device/AEC clock
mortic-helper --tts-sample-rate 16000     # provider PCM clock (resampled to device rate)
mortic-helper --event-completion-grace-sec 0.6  # wait for trailing text before polling; 0 disables
```

The orb in the sidepod shows Mortic's live activity — `listening` / `thinking` /
`speaking` / `muted` — while the caption and prompt annex show mic and connection
state; the two never report the same thing.

## Echo Protection

The native lane uses one persistent, synchronized 48 kHz duplex device stream.
Every 10 ms device tick feeds the exact rendered frame to WebRTC AEC before its
paired capture frame, including timed silence during pause/underflow. Provider
TTS is resampled independently; Flux remains fixed at 16 kHz in exact 80 ms
network packets.
`--voice-duplex` controls the behavior: `auto` (default) uses the echo
canceller and explicitly degrades to a half-duplex silence gate if synchronized
duplex cannot open, `full` passes raw mic audio (headphone users), and `half`
forces the gate (manual interruption remains available).

An episode-owned interruption controller keeps the loop stable on open
speakers. Candidate speech ducks output by 18 dB over 20 ms without stopping
the device clock. At 500 ms, render correlation at or above 0.75 and the narrow
backchannels `uh-huh`, `mm-hmm`, and `mhm` are suppressed; other speech commits
the interruption. The suppressed episode remains owned through final EOT plus
a 500 ms restart guard, so a playback edge cannot re-arm the same echo.
`TurnResumed` is compatibility-only and never flushes playback or aborts a
turn. Eager EOT is disabled until Mortic has an isolated speculative lane.

The former overlap, fuzzy-sequence, confidence, and text-length rules now run
as shadow telemetry only. They no longer decide whether real user speech is
discarded.

Turns stream from OpenCode's `/event` feed scoped to the fork's directory. If
the model produces no delta for three seconds, low-rate polling hedges the live
event reader from an independently timed task; a stuck poll never blocks SSE,
and message-ID deduplication prevents repeated text or speech.

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
