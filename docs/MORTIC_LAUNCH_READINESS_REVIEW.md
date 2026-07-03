# Mortic Launch Readiness Review

Status: Review complete, corrections applied
Date: 2026-07-02
Scope: MOR Jira board, `docs/MORTIC_OPENCODE_SIDEPOD_PRD.md`, `docs/MORTIC_PROJECT_EXECUTION_PLAN.md`, `docs/MORTIC_CURRENT_CODE_INVENTORY.md`, repository code
Decision: July 18, 2026 is re-baselined to the S2 milestone — first end-to-end native PTT demo (MOR-137). S3–S5 (latency pass, beta, release) move past July 18.

## Verdict

Documentation and ticket quality are excellent, and the scariest technical unknowns (slash-command interception, typing lock, key-release PTT) were verified as feasible against the installed OpenCode 1.17.11 API during this review — with one real caveat: hold-to-talk needs a Kitty-protocol terminal (not macOS Terminal.app) and must ship with the tap-mode fallback. The remaining top risk is timeline-vs-scope: at review time implementation had not started (0 of 88 tickets in progress, 4 commits in the repo) and 16 days remained.

## What's Strong (verified)

- PRD is high quality: explicit non-goals, full state machine, v0 protocol contract, acceptance criteria at product/technical/UX levels, encoded decisions.
- Execution plan cleanly splits Platform/Engine/Shared with mutual acceptance criteria per phase.
- Sampled tickets (MOR-88, MOR-135, MOR-137) all have goal, owner track, affected modules, acceptance criteria, dependencies.
- Real reusable engine assets exist: `opencode_voice/server.py` (fork lifecycle, event-first streaming, polling fallback, barge-in, compaction), `deepgram.py` (STT/TTS, SpeechTextFilter), `opencode_client.py` (SSE parser), `state.py` (context estimation).
- The code inventory (MOR-133) is honest — it flags most of the real gaps itself.

## Critical Risks (ranked)

### 1. Timeline math does not work
88 open tickets, 0 in progress, 0 assignees, 0 due dates at review time; delivery in 16 days (~11 working days). AGENTS.md's priority algorithm keys on due dates that were never set. S1→S5 milestones cannot all land by July 18.
**Correction (applied):** July 18 re-baselined to S2 (MOR-137); due dates set on the S1/S2 critical chain.

### 2. Platform capabilities — feasibility VERIFIED (with caveats)
Checked against the installed OpenCode 1.17.11 binary and the `@opencode-ai/plugin` 1.17.8 type definitions:

- **`/mortic` slash command: SUPPORTED.** `TuiCommand.slash?: { name, aliases }` exists in the plugin API, and the host's own built-ins register the same way (`slash:{name:"thinking"}` found in the binary). Slash commands dispatch a handler — they are not sent as model prompts.
- **Typing lock: primitives confirmed.** The proof already uses `api.mode.push("mortic.sidepod")` with mode-scoped `api.keymap.registerLayer({ mode: "mortic.sidepod", ... })`; `TuiPromptRef.blur()/focus()` exists as a fallback. Remaining unknown (30-min live check): whether unbound printable keys leak to the prompt while the Mortic mode is pushed.
- **Key release (hold-to-talk): SUPPORTED via Kitty keyboard protocol.** OpenTUI (bundled in the binary) parses `eventType: press|repeat|release`, emits `keyrelease`, and `enableKittyKeyboard()` defaults to flags=3, which includes "report event types." `renderer.useKittyKeyboard` lets the plugin detect support at runtime.
  - **Caveat:** release events only arrive on Kitty-protocol terminals (Ghostty, Kitty, WezTerm, iTerm2 ≥3.5, Alacritty ≥0.13, foot) — **not macOS Terminal.app**. The PRD's tap-to-arm/tap-to-release degradation (§9.3) must ship as the fallback, gated on runtime detection.
  - `M` vs `Space`: both behave identically under the protocol. Keep `M` per PRD; `Space` can be an alias.
- **Churn risk:** `api.command` is marked deprecated ("Remove in v2") in 1.17. Use `api.keymap.registerLayer` exclusively and pin/track the OpenCode version.

**Remaining work:** a half-day live smoke test — `/mortic` slash entry, typing-leak check in Mortic mode, hold-M release in a Kitty terminal, degraded tap mode in Terminal.app. Record the terminal-support matrix in docs.

### 3. Platform track is built on a dist-only artifact
`opencode_mercury_sidepod/` is 557 lines of built JS in `dist/` with no source tree, no build pipeline, no tests. MOR-88 assumes a shell exists to modify.
**Correction (applied):** ticket filed for sidepod source project + build + test harness, sequenced before MOR-88/89/90.

### 4. Engine distribution is underestimated
The helper is Python/FastAPI. Shipping a Python runtime plus OS-native mic capture (macOS mic permission attribution, PortAudio-class deps not yet in `requirements.txt`) is routinely a multi-week tarpit.
**Correction (applied):** distribution direction chosen (PyPI + `uvx`, see research findings); ticket filed.

### 5. Test/verification substrate is broken today
pytest is not a declared dependency (0 hits in `uv.lock`), not installed in `.venv`, and there is no CI. `tests/test_opencode_voice.py` cannot currently run.
**Correction (applied):** ticket filed to declare pytest as dev dependency, verify the suite, document the test command.

### 6. Latency gate has no baseline
The release gate (MOR-132) is defined as "no regression vs browser-backed reference," but MOR-129 (capture that baseline) is TO DO. If the browser path rots during refactoring, the gate becomes unmeasurable.
**Correction:** capture and store the baseline (MOR-129/162) before touching the bridge.

### 7. Process hygiene drift
- MOR-133 is in TESTING but its deliverable (`docs/MORTIC_CURRENT_CODE_INVENTORY.md`) is uncommitted.
- The predecessor repo (`Mortic - Claude Ver`, Codex-based, last commit June 12) has no documented fate.
**Correction:** commit the inventory, close MOR-133; declare the Codex repo superseded (or not) in docs/README.

## Pre-Implementation Research Findings (resolved unknowns)

All verified against the installed OpenCode 1.17.11 SDK/binary, current vendor docs, and the bridge source — July 2, 2026.

### OpenCode server API (Engine track)
- **Every endpoint the bridge uses exists in the installed SDK**: `/session/{id}/fork`, `/prompt_async`, `/abort`, `/summarize`, `/message`, `/event`, DELETE `/session/{id}`, `/global/health`. Event names the bridge consumes (`message.updated`, `message.part.delta`, `message.part.updated`, `session.idle`, `session.status`) all exist in current SDK types.
- **Two dead paths in `opencode_client.py`**: `switch_model` and `switch_agent` call `/api/session/{id}/model` and `/api/session/{id}/agent`, which do NOT exist in the SDK. Per-prompt `model`/`agent` payload on `prompt_async` is the supported mechanism — remove or verify the dead paths (feeds MOR-117/118).
- **New v2 endpoints simplify compaction work**: `/api/session/{id}/context` (answers MOR-160 — query it instead of estimating locally) and `/api/session/{id}/compact` (candidate replacement for summarize-based compaction in MOR-125/126). A `session.next.compaction.started/delta/ended` event family also exists.

### Deepgram (Engine track)
- **Flux STT: bridge matches docs exactly.** `wss://api.deepgram.com/v2/listen`, model `flux-general-en`, linear16 @ 16 kHz supported, `eot_threshold` (0.5–0.9, default 0.7), `eot_timeout_ms` (500–10000, default 5000), `eager_eot_threshold` optional; events StartOfTurn/EndOfTurn/EagerEndOfTurn/TurnResumed confirmed. Docs recommend **80 ms audio chunks** — carry into the native capture spec (MOR-111). `flux-general-multi` exists for future multilingual. Cost note: enabling eager EOT increases LLM calls 50–70%; keep it off by default (it already is).
- **TTS: verified.** `wss://api.deepgram.com/v1/speak`, Speak/Flush/Clear/Close client messages, Flushed/Cleared/metadata server messages, binary PCM frames, `aura-2-*` model family. The bridge's `FlushLimiter` (20/60 s) matches Deepgram's documented flush sensitivity.

### Mercury / Inception (Engine track)
- **`mercury-2` is a real, current model** (released 2026-03-04): 128K context, up to 50K output tokens, $0.25/M input, $0.75/M output, OpenAI-compatible at `https://api.inceptionlabs.ai/v1`. The bridge's provider overlay (`@ai-sdk/openai-compatible`, `{env:INCEPTION_API_KEY}`) is correct as-is.

### macOS microphone — product risk (Engine track, MOR-111)
- macOS TCC attributes mic permission to the **terminal app that spawned the helper** (Ghostty/iTerm/Terminal.app can prompt; **IDE-integrated terminals often silently deny** — known VS Code issue). Consequences:
  - MOR-111 must include: detect silent denial (zero audio frames after ptt.start) → emit `voice_bridge_issue` with `Mic permission needed` copy (already in PRD §18).
  - Docs/runbook (MOR-139/142) must include per-terminal mic-grant instructions and the integrated-terminal limitation.
  - Avoid PyInstaller-style unsigned app bundles in v1 — unsigned bundles break TCC attribution entirely.

### Helper distribution direction (MOR-109)
- Publish helper to PyPI, launch via `uvx mortic-helper` (uv detection + documented pip fallback). Avoids the PyInstaller/code-signing tarpit for beta; aligns with mic-TCC guidance above. The OpenCode plugin (npm) spawns/discovers it per E1's launch contract.

### OpenCode plugin distribution (Platform track)
- Plugins install from npm specs in config (auto-installed by Bun into `~/.cache/opencode/node_modules/`) or `file:` specs / `opencode plugin` command (the proof already installs this way). Publishing = plain npm package with `oc-plugin: ["tui"]`.
- **Churn risk:** official docs cover hook plugins only; the TUI sidebar API (slots/keymap/modes) is typed but underdocumented, and `api.command` is already marked "Remove in v2." Pin the tested OpenCode version; use `api.keymap.registerLayer` exclusively.

### PRD §25 open questions — recommended answers
1. **PTT key**: `M` per PRD; hold-to-talk gated on Kitty-protocol detection (`renderer.useKittyKeyboard`), tap-to-arm/tap-to-stop fallback elsewhere; `Space` optional alias. *[Superseded 2026-07-03: live investigation showed no real terminal delivers key releases for plain keys, and the tap-toggle fallback then merged with Live into a single `M` mic mute/unmute toggle emitting `live.set` — see `docs/MORTIC_TERMINAL_CAPABILITY_SMOKE.md` and the PRD Revision section.]*
2. **Handoff generation**: helper-side direct Mercury call over the local turn log (not an OpenCode agent turn) — cheaper, faster, no fork dependency; the transcript is the PRD-specified source anyway.
3. **Debug mode**: logs-only in v1; no hidden panel.
4. **Config stub**: placeholder popup (`Config coming later`) — reuses the existing popup host.
5. **Transcript metadata**: purely conversational in v1; latency lives in logs.
6. **Handoff "files touched"**: omit in v1.

## S1/S2 Critical Chain (due dates set in Jira)

Protocol freeze (MOR-134/135/136) → live capability smoke → sidepod source project + MOR-88 shell + MOR-102 protocol client → helper baseline MOR-107 + mic MOR-111 + STT MOR-112 + TTS MOR-113 → fork turn loop MOR-116/117/118 → **S2 demo MOR-137, due July 18**.
