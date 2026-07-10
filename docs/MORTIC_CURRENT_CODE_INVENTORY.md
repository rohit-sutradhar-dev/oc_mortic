# Mortic Current Code And Reference Inventory

Status: MOR-133 working inventory
Date: 2026-07-02
Source tickets: MOR-133, MOR-88, MOR-107, MOR-129, MOR-135, MOR-136

## Purpose

This inventory gives Platform and Engine a shared factual map of the current repository before the native sidepod implementation work starts. The product direction in `docs/MORTIC_OPENCODE_SIDEPOD_PRD.md` remains the source of truth when this inventory describes current behavior that should not ship as-is.

## Repository Shape

- `opencode_voice/` is the current Python/FastAPI browser-backed voice bridge. It owns OpenCode session/fork calls, Deepgram Flux STT, Deepgram Speak/Aura TTS, Mercury/OpenCode turn execution, speech filtering, compaction, and the browser mic/audio UI.
- `opencode_mercury_sidepod/` is a native OpenCode TUI sidebar proof. It owns a terminal-rendered Mortic panel, sprite, command deck, focus-mode experiment, transcript popup, and handoff popup. It does not yet talk to the voice bridge.
- `docs/` contains the current PRD and execution plan.
- `MORTIC_PLUGIN_HANDOFF.md` records the earlier bridge-plus-sidepod integration proposal. Some recommendations there are now superseded by the PRD, especially visible browser UI and packaged typed fallback.
- `tests/test_opencode_voice.py` covers the Python bridge helpers, protocol parsers, event trackers, context estimation, Deepgram URL/message parsing, TTS chunking, and speech filtering.

## Reusable Code

### Engine-Reusable

- `opencode_voice/opencode_client.py`
  - Reusable OpenCode API client for health, sessions, fork/delete, summarize, abort, synchronous prompt, `prompt_async`, `/event` SSE parsing, and message fetch.
  - `SSEParser` already has focused unit coverage for multiline and malformed frames.
- `opencode_voice/server.py`
  - `VoiceConnection` has reusable voice-lane orchestration: source session tracking, fork creation, fork cleanup, turn ids, barge-in, event-first turn execution, polling fallback, TTS streaming, context overflow retry, and compaction.
  - `DeepgramFluxSession` wraps the bounded, epoch-aware Flux sender; provider-neutral TTS lives in `tts_providers.py` with persistent Deepgram and Cartesia implementations.
  - `device_audio.py` owns the persistent device-clocked duplex stream, render reference, bounded jitter buffer, and generation-safe playout; `interruption.py` owns pure episode decisions.
  - `EPHEMERAL_PREFIX` and keep/delete behavior are useful starting points for source-thread safety.
- `opencode_voice/deepgram.py`
  - `build_flux_url`, `parse_flux_message`, `build_tts_url`, `TTSChunker`, `FlushLimiter`, and `SpeechTextFilter` are reusable.
  - Speech filtering is product-critical because code, diffs, commands, paths, and JSON must stay screen-only.
- `opencode_voice/state.py`
  - Context estimation, summary reset behavior, assistant delta tracking, and OpenCode event turn tracking are reusable for Engine compaction and streaming.
- `opencode_voice/__main__.py`
  - Managed OpenCode startup, server detection, config overlay rendering, port selection, and CLI shape are useful for local helper launch/discovery design.
- `opencode_voice/voice_agent.md`
  - Reusable voice-agent behavioral prompt for concise speakable output and screen-only implementation detail.

### Platform-Reusable

- `opencode_mercury_sidepod/src/tui.js`
  - Reusable TUI frame helpers, text wrapping, sidebar slot registration, keymap layer registration, mode push/pop pattern, popup host pattern, transcript/handoff draft rendering, clipboard attempt, and pulsing braille sprite approach.
  - The sprite host and terminal frame direction match the PRD better than the browser UI does.
- `opencode_mercury_sidepod/package.json`
  - Reusable OpenCode TUI plugin packaging metadata: `oc-plugin: ["tui"]`, package export shape, build/test commands, and peer dependency surface.

## Reference-Only Or Throwaway Pieces

- `opencode_voice/static/index.html`, `app.js`, and `styles.css`
  - Useful as a browser-backed technical reference for mic capture, PCM downsampling, TTS playback, status updates, and latency baseline capture.
  - Not suitable for packaged v1 UI because it shows a browser shell, session picker, typed fallback prompt, iframe, model labels, TTS model labels, and thread management.
- Current WebSocket control/event names in `opencode_voice/server.py` and `static/app.js`
  - Current names include `audio.start`, `audio.stop`, `turn.start`, `fork.ready`, `tts.first_audio`, `barge_in`, and generic `error`.
  - These need mapping or replacement with the PRD v0 protocol: `start`, `ptt.start`, `ptt.stop`, `live.set`, `refresh`, `barge_in`, `confirm.response`, plus `ready`, `listening`, `transcript`, `thinking`, `assistant.delta`, `speaking`, `complete`, `interrupted`, and `voice_bridge_issue`.
- Current sidepod command deck in `opencode_mercury_sidepod/src/tui.js` (this block updated 2026-07-03; the original inventory notes are resolved)
  - `last`/`items` diagnostics rows: removed.
  - `Clear Lane` is `[X]`; confirmed `Refresh` lands with the engine integration (MOR-96).
  - Voice control is a single `M` mic mute/unmute toggle (PTT and Live merged, owner decision 2026-07-03), isolated in Mortic focus mode.
  - `/mortic` focuses without sending a prompt; `ctrl+x v` remains as a secondary binding.
- `opencode_mercury_sidepod/src/index.js`
  - Empty server stub. It does not launch, discover, or connect to the bridge/helper.
- `runs/voice/`, local logs, `.env`, `.venv/`, `node_modules/`, and local run artifacts
  - These are ignored/local-only and must remain out of packaged output.

## Product-Critical Behavior Already Present

- Ephemeral fork safety
  - Current bridge forks the chosen source session, switches model/agent on the fork, tags the fork title with `[voice tmp]`, and deletes the fork on close/stop when `keep_fork` is false.
  - Risk: source-thread untouched assertions are not yet tested end to end.
- Event-first OpenCode streaming
  - Current turn path opens `/event`, sends `prompt_async`, tracks assistant deltas, and falls back to polling when stream setup or stream delivery fails.
  - This is central to the latency target.
- Polling fallback
  - Existing fallback can continue a turn after event stream failure, including a path for `poll_after_event`.
- Barge-in
  - Speech start/resume and manual barge-in close TTS, clear the active turn id, and best-effort abort the OpenCode fork turn.
- Speech filtering
  - Existing unit tests prove fenced code, markdown code details, commands, identifiers, and file names are removed or generalized before speech.
- Context handling
  - Active context estimation, 70k threshold, proactive compaction, wait-on-compaction, and compact-and-retry on context overflow are implemented.
- Safe local dev configuration
  - Inception and Deepgram keys are read from environment variables. Existing tests assert the OpenCode config uses `{env:INCEPTION_API_KEY}` rather than embedding a raw key.

## Owner Mapping

### Platform Track

Start from:

- `opencode_mercury_sidepod/src/tui.js`
- `opencode_mercury_sidepod/package.json`
- `opencode_mercury_sidepod/tests/package.test.mjs`
- PRD sections for sidepod layout, command deck, COMMS, Transcript, Handoff, Config, `/mortic`, and key isolation

Platform-owned next work:

- Replace diagnostics/provider labels with PRD-safe UI. *(Done.)*
- Command deck (revised 2026-07-03): `[M] Microphone`, `[X] Clear Lane`, `[T] Transcript`, `[H] Handoff`, `[ESC] End Session`. *(Done; `C Config` deferred to MOR-100, confirmed `R` Refresh with the engine integration.)*
- Esc/End Session confirmation. *(Done — explicit confirm dialog; `R` confirm pending engine.)*
- Make `/mortic` the focus entrypoint. *(Done, including refusal without an open session.)*
- `M` key capture: hold model retired (no terminal key releases); `M` is a plain mic mute/unmute toggle, isolated in focus mode. *(Done; documented in `docs/MORTIC_TERMINAL_CAPABILITY_SMOKE.md`.)*
- Implement a v0 protocol client against Engine-provided fixtures/helper.
- Render `Voice Bridge Issue` for bridge failures without provider/model/runtime detail.

### Engine Track

Start from:

- `opencode_voice/server.py`
- `opencode_voice/opencode_client.py`
- `opencode_voice/deepgram.py`
- `opencode_voice/state.py`
- `opencode_voice/__main__.py`
- `opencode_voice/voice_agent.md`

Engine-owned next work:

- Extract the browser-backed bridge into an invisible local helper or helper-compatible service.
- Replace browser mic capture with OS-native capture or a documented native capture plan.
- Adapt the current socket events to v0 protocol messages.
- Preserve fork creation/cleanup, event-first streaming, polling fallback, barge-in, compaction, and speech filtering.
- Ensure helper health emits `ready` or `voice_bridge_issue` without raw provider/model/runtime details.
- Add safe lifecycle logs and latency metrics without secrets.

### Shared Track

Start from:

- `docs/MORTIC_PROJECT_EXECUTION_PLAN.md` protocol section
- `tests/test_opencode_voice.py`
- This inventory

Shared-owned next work:

- Freeze message names, payload examples, and event ordering in v0.
- Create fixtures that both sidepod and helper tests can consume.
- Link fixture examples to `MOR-106` and helper transport tests.
- Decide the approval path for protocol changes after v0.

## Latency-Sensitive Paths

- Browser reference capture path
  - `static/app.js` uses `getUserMedia`, `createScriptProcessor(4096, 1, 1)`, downsampling to 16 kHz, and PCM16 frames over WebSocket. This is the current latency reference, not the packaged path.
- Deepgram STT turn-taking
  - `FluxTransport` packetizes capture into exact 80 ms Flux v2 packets without blocking the device callback, bounds audio freshness to 500 ms, reconnects with epoch fencing, and uses Happy Eyeballs for broken single-family routes. Uvicorn is pinned to standard asyncio because uvloop rejects those connection options before network I/O.
  - Eager EOT is disabled. `TurnResumed` is parsed for compatibility and has no playback/OpenCode side effect.
- OpenCode first text
  - Fast path is `/event` plus `prompt_async`.
  - `assistant.first_text` is emitted when first assistant delta arrives.
  - Three seconds of model silence starts an independently bounded polling producer without cancelling or blocking SSE; a message-ID tracker deduplicates hybrid observations. Managed OpenCode sets Bun's standalone-executable `BUN_OPTIONS` to prefer IPv4 without adding unsupported OpenCode CLI arguments; child output is inherited by the helper log for startup diagnosis.
- TTS first audio
  - `TTSChunker` emits sentence-sized chunks.
  - `DeepgramTTSProvider` keeps a prewarmed conversation socket and uses one final `Flush/Flushed` plus `Clear/Cleared`; `CartesiaTTSProvider` keeps one continued context per turn and fences late context audio.
  - Both provider readers feed bounded ordered delivery actors, so device-clock backpressure cannot starve WebSocket control frames. Provider `done`/failure and device drain jointly own turn completion.
  - `tts.first_audio` is emitted on the first non-silent device frame, not provider arrival.
- Compaction and context overflow
  - Proactive compaction can run in the background but may delay a turn via `maybe_wait_for_compaction`.
  - Context overflow triggers compact-and-retry.
- Poll fallback
  - Polling is a low-rate hedge and stream source is recorded as `event`, `poll`, or `hybrid`.

## Gaps And Risks To Track

- Protocol mismatch: **resolved 2026-07-04** — `SidepodConnection` translates all legacy engine vocabulary to v0 at a single `send_json` seam, validated against the generated schema (fail closed). The browser lane keeps its legacy names by design.
- Packaged UI mismatch: current browser UI exposes thread selection, typed fallback, model/provider details, and visible browser/iframe surface, all non-goals for packaged v1. Browser surface stays reference-only.
- Helper mismatch: **resolved for the sidepod lane 2026-07-04** — the plugin launcher discovers or spawns `mortic-helper` (`--no-managed`, pinned to the focused thread's OpenCode server) and the lane runs end to end over `/ws/sidepod`.
- Native mic gap: **hardened 2026-07-10** — `PersistentDeviceAudioEngine` drives a synchronized 10 ms duplex clock with timed AEC reference, bounded jitter buffering, generation fencing, and explicit half-duplex fallback. Live spoken-turn/soak verification remains an owner gate.
- Sidepod source gap: MOR-166 added `opencode_mercury_sidepod/src/`; the package now ships `src/` directly (no build step, no `dist/`).
- Sidepod test gap: MOR-166 adds package fixture tests; deeper TUI snapshot tests are still needed before larger visual changes.
- Source-thread safety gap: fork cleanup exists, but tests do not yet prove source OpenCode thread remains untouched after a voice turn.
- Secret/logging audit gap: automated tests cover config metadata fingerprints, monotonic correlation fields, provider error shaping, and content/secret redaction; consented capture retention and live log review remain beta gates.

## Ticket Linkage

- `MOR-88`: use the native sidepod proof as the starting shell, remove diagnostics/provider labels, and align the command deck with the PRD.
- `MOR-107`: reuse the Python bridge orchestration, OpenCode client, Deepgram sessions, fork handling, barge-in, compaction, and filtering as helper internals.
- `MOR-129`: capture the current browser-backed path as the reference latency baseline using first transcript, first assistant text, first TTS audio, total turn, retry/fallback source, and test conditions.
- `MOR-135`: freeze the v0 protocol and explicitly map or replace current bridge event names.
- `MOR-136`: build shared fixtures from the existing unit-tested event shapes plus new PRD v0 examples.
