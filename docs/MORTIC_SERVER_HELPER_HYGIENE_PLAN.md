# Mortic Server-Helper Hygiene Implementation Plan

Status: revised after live debug review
Date: 2026-07-08
Scope: OpenCode server ownership, Mortic helper ownership, `voice-build` prompt isolation, crash cleanup, and minimal v1 onboarding hygiene.

## Plain-English Correction

The first draft had one big hidden assumption: it treated the visible OpenCode TUI as something the helper could always reach over HTTP. Live debugging disproved that.

The model must now be:

1. The visible OpenCode TUI is where the user types `/mortic`.
2. That TUI may have no reachable TCP server at all, even if its plugin hook reports a nominal URL.
3. Mortic therefore cannot depend on calling the visible TUI server.
4. Mortic needs its own confirmed voice path: helper plus a Mortic-owned OpenCode voice server that has the `voice-build` agent.
5. Before we rely on voice forks, we must prove whether a Mortic-owned server can see and fork the visible TUI session.

If that fork test fails, the product design changes: Mortic voice becomes a fresh managed voice session seeded from source-thread context, not a fork of the visible source session.

## Live Evidence Overrides

Treat these as stronger than current docs until contradicted by a new smoke:

- A bare `opencode` TUI on installed OpenCode `1.17.15` was observed listening on zero TCP ports and only a connected Unix domain socket.
- The plugin `server` hook advertised `http://localhost:4096`, but `localhost`, `127.0.0.1`, and `::1` on port `4096` all refused connections.
- `opencode serve --port <n>` and `opencode --port <n>` do bind reachable TCP servers.
- A Mortic-managed `opencode serve` on a free port can carry the `voice-build` agent through `OPENCODE_CONFIG_CONTENT`.
- `server` hook and `./tui` entry can run as separate processes/module graphs. Process env is not a reliable handoff between them. A file or registry is required.

Docs still matter as API guidance:

- [OpenCode server docs](https://opencode.ai/docs/server/) describe the TUI/server architecture and `opencode serve`.
- [OpenCode plugin docs](https://opencode.ai/docs/plugins/) describe plugin startup, context, and hook/event surfaces.
- [OpenCode agents docs](https://opencode.ai/docs/agents/) define custom agents with custom prompts.
- [OpenCode commands docs](https://opencode.ai/docs/commands/) show slash command agent config, but `/mortic` is a focus command, not a prompt command.

## Revised Key Decisions

### D1. Do not route v1 through the visible TUI server

The visible TUI server URL can be logged or probed opportunistically, but it is not a product dependency.

Use plugin-side context for identity:

- `directory`
- `worktree`
- route `sourceSessionId`
- generated plugin window id

Do not require `GET {visibleServerUrl}/path`, `GET {visibleServerUrl}/project/current`, or any other visible-server HTTP probe in the happy path.

### D2. Create a Mortic-owned OpenCode voice server after confirmation

Mortic should start a managed `opencode serve` on a free port with `OPENCODE_CONFIG_CONTENT` containing the `voice-build` primary agent. This isolates the spoken-output system prompt from the user's normal OpenCode agents.

First-run prompt:

```text
Start Mortic Voice?
Mortic needs an isolated voice server for this workspace. Start it now?
```

Confirm starts the helper and managed voice server. Reject restores prompt focus and starts nothing.

### D3. Resolve the fork model before building dependent phases

Before implementation beyond the MVP bootstrap, run a blocker smoke:

1. Start a reachable "source" OpenCode server for the same worktree.
2. Create or select a source session through that server.
3. Start a second Mortic-managed OpenCode server in the same worktree.
4. From the managed server, attempt to get and fork the source session id.
5. Run a lock/race smoke with both servers operating on the same project storage.

Result rules:

- If the managed server can see/fork the source session and concurrent server access is safe, keep the ephemeral fork model.
- If it cannot see/fork the source session, make "fresh managed voice session seeded from source context" the primary design.
- If concurrent access is unsafe, do not run a separate managed server against the same storage without an isolation/locking design.

### D4. v1 favors a small reliable core over full machinery

MVP should be:

1. Confirmed managed voice server startup.
2. Helper startup with `voice-build` doctor gate.
3. Clear `Voice Bridge Issue` when OpenCode voice server, credentials, mic, or agent config is wrong.
4. Crash cleanup for managed server processes.
5. No happy-path port guessing.

Defer unless evidence demands them:

- full dynamic helper port negotiation;
- full multi-window takeover protocol;
- large runtime registry with owner/PID/session schema.

### D5. No happy-path guessing

Remove product reliance on:

- blind `4096` fallback;
- blind `17242` fallback;
- `pgrep` scrape as normal behavior;
- helper reuse based only on `ready: true`.

Keep env overrides only for dev/debug:

- `MORTIC_HELPER_URL`
- `OPENCODE_VOICE_OPENCODE_URL`
- `MORTIC_DEV_ALLOW_SERVER_DETECT=1`

## Current Code Map

### Platform sidepod

- `opencode_mercury_sidepod/src/index.js`
  - Server hook receives `input.serverUrl`.
  - Live evidence says the value may be nominal and not TCP-reachable.
  - Use it as a diagnostic, not a hard dependency.

- `opencode_mercury_sidepod/src/host-context.mjs`
  - Current env handoff is insufficient across process boundaries.
  - Commit `6a34cce` already moves toward a temp-file handoff for URL diagnostics.
  - Revised plan should generalize this into a small handoff file for directory/worktree/window id, not a large registry yet.

- `opencode_mercury_sidepod/src/helper-launcher.mjs`
  - Currently hard-codes helper URL `http://127.0.0.1:8765`.
  - Currently spawns with `--no-managed`.
  - `--no-managed` was added to avoid silently leaking shadow `opencode serve`.
  - Revised plan keeps the safety property by requiring explicit confirmation before managed mode.

- `opencode_mercury_sidepod/src/tui.js`
  - `/mortic` focuses the sidepod and owns user confirmation.
  - Local type definitions expose `api.ui.DialogConfirm`, `api.ui.dialog.replace`, and `api.ui.dialog.clear`.
  - Runtime smoke is still required before depending on the exact behavior.

### Engine helper

- `opencode_voice/__main__.py`
  - `start_managed_opencode` already starts `opencode serve` on a free port.
  - It injects `OPENCODE_CONFIG_CONTENT`.
  - It currently cleans up the child only on graceful helper shutdown.

- `opencode_voice/config.py`
  - `render_opencode_config` can define `voice-build` as a primary agent with the voice prompt.

- `opencode_voice/voice_agent.md`
  - Product-critical prompt that keeps spoken output short and avoids code, commands, paths, diffs, JSON, and stack traces.

- `opencode_voice/doctor.py`
  - Existing doctor checks reachability, voice agent presence, and a model round trip.
  - Reuse the diagnostic checks only; managed `/mortic` supplies `voice-build` through `OPENCODE_CONFIG_CONTENT` and must not inspect or mutate global OpenCode config.

- `opencode_voice/server.py`
  - `/api/health` currently can throw when OpenCode is unreachable.
  - Commit `6a34cce` fixes this by returning 200 with `opencode_unreachable`; reuse this work.

- `opencode_voice/opencode_client.py`
  - Per-turn `agent` payload is the authoritative path for `voice-build`.
  - SDK v2 local types expose `switchAgent` and `switchModel`, but the Python client routes must be live-verified before changing them.

## Phase 0: Mandatory Smokes Before Fork-Dependent Work

Do this before implementing the fork path, takeover path, or any code that assumes source-session sharing.

### Smoke A: Cross-server session visibility and fork

Goal: answer whether a Mortic-managed server can see and fork a session created by another OpenCode server in the same worktree.

Procedure:

1. Create a temporary worktree or test project.
2. Start server A with reachable TCP, using installed `opencode 1.17.15`.
3. Create a source session on server A.
4. Start server B with `opencode serve` in the same worktree and `OPENCODE_CONFIG_CONTENT` containing `voice-build`.
5. Call server B:
   - `GET /session/{sourceSessionId}`
   - `POST /session/{sourceSessionId}/fork`
6. Record pass/fail and exact response codes.

Required follow-up:

- Repeat once with a real TUI-launched source if possible, not only two headless servers.

### Smoke B: Concurrent server safety

Goal: answer whether two OpenCode servers can safely operate on the same project/session storage.

Procedure:

1. Keep server A and server B open in the same worktree.
2. Create, update, fork, and delete throwaway sessions from both.
3. Watch logs for locking errors, corrupt state, lost sessions, or event-stream confusion.
4. Close both servers and reopen one server to verify storage integrity.

### Decision after Phase 0

If Smoke A and B pass:

- Use managed voice server plus ephemeral fork of the source session.

If Smoke A fails:

- Make handoff-seeded managed voice sessions the primary design.
- The sidepod should say "Mortic is ready" only after the managed voice session is seeded.
- Do not claim source-thread fork safety.

If Smoke B fails:

- Do not run dual servers against the same storage.
- Either isolate voice storage and seed context, or require the visible TUI to be started with a reachable/configured voice-capable server.

## MVP Phase 1: Adopt Existing Reliability Work

Do not reimplement these from scratch. Bring forward or cherry-pick the proven pieces from branch `mor-111-voice-reliability`.

### Commit `6a34cce`

Purpose:

- `/api/health` never 500s when OpenCode is unreachable.
- Health returns an `opencode_unreachable` issue with safe detail.
- Launcher carries health failure reasons to the sidepod toast.
- URL handoff uses a temp file instead of process env alone.

Plan:

- Adopt the health never-500 behavior.
- Keep the user-facing safe detail.
- Keep file handoff as a diagnostic/handoff primitive, but do not depend on visible `serverUrl` for routing.

### Commit `defb1fa`

Purpose:

- The doctor made missing `voice-build` loud before a turn hangs.
- The old global-config repair path does not apply to managed `/mortic`; the managed server should receive `voice-build` through `OPENCODE_CONFIG_CONTENT`.

Plan:

- Reuse the doctor diagnostics and tests.
- Remove global repair assumptions from Mortic v1.
- Managed voice server startup must use `OPENCODE_CONFIG_CONTENT` so the user's main config is untouched.

## MVP Phase 2: Confirmed Managed Voice Startup

Files to inspect/edit:

- `opencode_mercury_sidepod/src/tui.js`
- `opencode_mercury_sidepod/src/helper-launcher.mjs`
- `opencode_mercury_sidepod/src/host-context.mjs`
- `opencode_voice/__main__.py`
- `opencode_voice/config.py`
- `opencode_voice/doctor.py`
- `opencode_voice/server.py`
- `tests/test_doctor.py`
- `tests/test_opencode_voice.py`

Work:

1. `/mortic` checks for an active session route and source session id.
2. If no helper/managed server is ready for this workspace, show `DialogConfirm`.
3. On confirm, start helper in explicit managed mode.
4. Helper starts `opencode serve` on a free port with `OPENCODE_CONFIG_CONTENT`.
5. Helper runs a doctor gate against its managed server:
   - server reachable;
   - `voice-build` present;
   - model/provider usable enough for first turn or configured to fail safely.
6. Only then connect the sidepod and allow `M` to start audio.
7. On reject, restore prompt focus and start nothing.

Do not ask about ports, URLs, Python, providers, or runtime details in the main prompt.

## MVP Phase 3: Crash Cleanup

This is mandatory if managed mode becomes explicit.

Problem:

- `--no-managed` existed because a detection miss once leaked a managed `opencode serve`.
- Graceful shutdown cleanup is not enough. If the helper crashes, its managed OpenCode child can orphan.

Required mechanisms:

1. Start managed `opencode serve` in an owned process group.
2. Write an owned-process lease file containing:
   - helper pid;
   - managed server pid;
   - process group id;
   - workspace;
   - managed server URL;
   - updated heartbeat timestamp.
3. Helper updates heartbeat while alive.
4. Helper handles SIGTERM/SIGINT and kills the owned process group.
5. On next helper start, reap stale leases whose helper pid is gone or heartbeat is stale.
6. Add a watchdog mechanism for crash cleanup:
   - preferred: small watchdog process monitors helper pid and kills managed process group if helper dies;
   - acceptable MVP fallback: stale lease reaper plus short idle TTL, but document residual orphan risk.

Tests:

- helper graceful exit kills managed server;
- helper crash leaves stale lease and next start reaps it;
- stale lease for unrelated process is not killed without identity match.

## Deferred Phase: Helper Port Negotiation

For MVP, keep `8765` as the preferred helper port.

If `8765` is occupied:

- if the occupant is a healthy Mortic helper, reuse only after identity/doctor checks;
- if the occupant is not Mortic, show a clear `Voice Bridge Issue`;
- allow dev override through `MORTIC_HELPER_URL`.

Do not build a full dynamic-port registry until onboarding data shows real collisions.

When promoted:

- choose a free helper port;
- write a tiny handoff file with `helperUrl`;
- make sidepod connect through that handoff.

Avoid a large registry schema until multiple-window or multi-helper evidence requires it.

## Deferred Phase: Multi-window Takeover

The desired UX is valid:

```text
Mortic is already active in another thread.
Move voice here?
Confirm / Reject
```

But the full active-owner protocol is not MVP unless we observe or intentionally test multi-window usage.

MVP guard:

- helper allows one active WebSocket voice owner;
- if another starts, helper reports `voice_owner_conflict`;
- sidepod shows Confirm/Reject;
- Confirm stops current lane if the old WebSocket is still connected;
- Reject leaves the old lane untouched.

Deferred hardening:

- durable owner registry;
- old sidepod focus restoration across disconnected clients;
- cross-workspace ownership arbitration;
- robust "ownership lost" reducer states.

## Visible Server URL Audit

Survives:

- diagnostic logging;
- optional reachability probe in debug mode;
- support for explicit dev attach mode through `OPENCODE_VOICE_OPENCODE_URL`.

Removed from product dependencies:

- no `GET {visibleServerUrl}/path` requirement;
- no `GET {visibleServerUrl}/project/current` requirement;
- no assumption that plugin `serverUrl` is TCP-reachable;
- no helper routing through visible server in the happy path.

Replacement identity:

- plugin `directory`;
- plugin `worktree`;
- route `sourceSessionId`;
- generated sidepod/window id;
- managed server workspace path.

## Verify-Before-Build Assumptions

These must be verified against installed OpenCode `1.17.15`, not only docs.

1. TUI dialog API:
   - local type definitions expose `DialogConfirm`, `dialog.replace`, and `dialog.clear`;
   - still run a runtime smoke inside OpenCode before shipping.

2. `OPENCODE_CONFIG_CONTENT`:
   - verify `opencode serve` with that env exposes `voice-build` through `/agent`;
   - verify a per-turn payload with `"agent": "voice-build"` actually selects that prompt;
   - managed env injection should get its own smoke because global config repair is not part of v1.

3. Switch routes:
   - SDK v2 type definitions expose `switchAgent` and `switchModel`;
   - current Python client uses older `/api/session/{id}/agent` and `/api/session/{id}/model` paths;
   - verify live before editing this area;
   - per-turn `agent` remains the safer MVP path.

4. Cross-server source session:
   - Phase 0 smokes are blockers.

## UX Edge Cases

### User types `/mortic` and wants nothing else

First run may ask exactly one confirmation. After that, if a valid managed voice server is warm for the workspace, `/mortic` should open directly.

### User rejects the prompt

No process starts. Focus returns to the normal OpenCode prompt. No key swallowing remains active.

### `8765` is busy

MVP shows a specific helper-port issue. Full alternate-port negotiation is deferred until collisions are observed.

### Managed server starts but lacks `voice-build`

Doctor gate catches it before mic starts. Surface `Voice Bridge Issue`, not a silent hang.

### Bare TUI has no TCP server

This is expected and should not block `/mortic`, because v1 should not depend on the visible server over TCP.

### Managed server cannot fork visible source session

Do not start listening as if source-thread safety exists. Use the handoff-seeded design or fail with a clear issue, depending on the Phase 0 decision.

### Helper crashes

Owned managed server must be cleaned up by watchdog or stale lease reaper.

## Verification Plan

Automated:

```bash
uv run pytest
npm test --prefix opencode_mercury_sidepod
```

Required live smokes:

1. Phase 0 cross-server session/fork smoke.
2. Phase 0 concurrent server safety smoke.
3. DialogConfirm runtime smoke in installed OpenCode.
4. Managed `OPENCODE_CONFIG_CONTENT` exposes `voice-build`.
5. Per-turn `agent: voice-build` selects the prompt.
6. Helper crash cleanup reaps managed server.
7. Bare `opencode` with no reachable TCP still lets `/mortic` start a managed voice path after confirmation.

## Recommended Ticket Split

1. Phase 0: cross-server fork and locking smoke.
2. Adopt `6a34cce`: health never-500 and safe OpenCode-unreachable issue.
3. Adopt the `defb1fa` doctor diagnostics without the global-config repair path.
4. Confirmed managed voice server startup with `voice-build`.
5. Managed process lease, watchdog, and stale reaper.
6. Decide fork-vs-handoff primary design from Phase 0 results.
7. Defer dynamic helper port negotiation until collisions are observed.
8. Defer full multi-window takeover until tested/observed demand.
