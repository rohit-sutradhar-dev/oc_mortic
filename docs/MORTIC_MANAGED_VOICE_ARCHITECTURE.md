# Mortic Managed Voice Architecture

Status: implementation record for `12af132` (`Implement Mortic sidepod protocol and UI updates`)

Date: 2026-07-10

Related planning document: [Mortic Server-Helper Hygiene Implementation Plan](MORTIC_SERVER_HELPER_HYGIENE_PLAN.md)

## Purpose

This document records the architectural direction that landed with `12af132`,
why it was chosen, and what it changes in the delivery plan. It complements the
implementation plan rather than replacing it:

- the plan records the original evidence, intended phases, and open validation;
- this document describes the runtime that now exists and the constraints future
  work must preserve.

The central correction is simple: the OpenCode process that renders the visible
TUI is not a dependable HTTP server. Mortic uses that TUI as the command and
display surface, but it does not require a reachable TCP server behind it to
begin a voice session.

## Executive Summary

`/mortic` now opens a Mortic-owned voice lane instead of attaching the helper to
a presumed server behind the visible OpenCode TUI. After explicit user
confirmation, the sidepod starts a local helper for the current workspace. The
helper starts its own `opencode serve` child on a free port and supplies the
voice-only `voice-build` agent through `OPENCODE_CONFIG_CONTENT`.

The visible TUI still supplies the source session id, captures `/mortic`, owns
focus, and renders the lane. The helper owns audio, turn orchestration, the
managed OpenCode child, and cleanup. The managed server owns the voice-agent
configuration. The user's global OpenCode configuration is not inspected,
repaired, or mutated in the managed path.

This removes normal startup dependence on the nominal plugin `serverUrl`, port
`4096`, port `17242`, or a process scan finding the right visible TUI. It also
introduces a deliberate operational boundary: Mortic now owns a second OpenCode
process and must prove cross-server session visibility and concurrent-storage
safety before treating the ephemeral-fork design as production-ready.

## Runtime Topology

```text
Visible OpenCode TUI
  owns: /mortic command, source-session identity, focus, COMMS rendering
  records: host serverUrl only as a diagnostic/dev-attach hint
                  |
                  | local WebSocket: /ws/sidepod
                  v
Mortic helper (one local endpoint, normally 127.0.0.1:8765)
  owns: native mic, STT/TTS, turn loop, fork lifecycle, bridge issues
  owns process group and lease for:
                  |
                  v
Managed `opencode serve` (free port, current workspace)
  receives: OPENCODE_CONFIG_CONTENT with the `voice-build` agent
  owns: the OpenCode API used by Mortic voice turns
                  |
                  v
Ephemeral `[voice tmp]` fork of the source session
  owns: voice conversation state; deleted on normal lane shutdown
```

The sidepod and helper communicate only through the frozen v0 protocol. The
visible OpenCode server URL is not part of the normal route to the managed
server. `OPENCODE_VOICE_OPENCODE_URL` remains useful for deliberate development
attach and diagnosis, but it is not a required user setup step.

## Architectural Decisions

### Separate the command surface from the voice execution server

**Decision.** Treat the visible OpenCode TUI as a UI and identity source, not as
a reliable HTTP dependency. The plugin may record `input.serverUrl`, but Mortic
does not route normal voice traffic through it.

**Evidence.** A bare OpenCode 1.17.15 TUI was observed with no reachable TCP
port while the plugin hook still advertised `http://localhost:4096`. A manually
started `opencode serve --port <n>` was reachable. Those observations override
the earlier assumption that the TUI hook URL was sufficient to attach the
helper.

**Result and trade-off.** A missing or unusable nominal URL no longer prevents a
voice start. In return, Mortic needs its own OpenCode server and source-session
sharing between that server and the visible TUI is now a first-class
compatibility question.

### Make managed startup explicit and workspace-scoped

**Decision.** Start the managed helper/server path only after user confirmation.
The sidepod passes the current workspace with
`--managed-opencode --opencode-dir <workspace>`.

**Why.** The earlier `--no-managed` guard prevented an unnoticed shadow
`opencode serve` from appearing when server detection failed. The replacement
preserves that property: the managed child is intentional, visible in the
interaction model, and tied to one workspace.

**Normal sequence.**

1. `/mortic` validates that the TUI has an active source session.
2. Mortic refuses a `[voice tmp]` source and asks the user to switch to the
   original conversation.
3. Mortic probes helper ownership and workspace identity at the helper endpoint.
4. A ready same-workspace helper is reused. A foreign-workspace helper shows an
   issue before any managed-start confirmation is shown.
5. If there is no valid helper, the sidepod asks to start Mortic Voice.
6. Confirmation starts the helper, then its managed OpenCode child; the lane is
   available only after health checks pass.
7. Rejection starts nothing and returns focus to the normal OpenCode prompt.

**Planning implication.** Confirmation is a product contract. Automatic
startup, warm-server retention, and cross-workspace takeover must retain a clear
consent boundary and say what process will be reused, stopped, or moved.

### Inject the voice agent into the managed server, not global OpenCode config

**Decision.** Build `voice-build` into the managed server's environment with
`OPENCODE_CONFIG_CONTENT`. Mortic doctor does not inspect, repair, or mutate the
user's global OpenCode configuration.

**Why.** The spoken-output prompt is Mortic-specific and should not alter normal
agents. Global repair makes onboarding depend on unrelated visible-TUI setup and
risks persistent user-visible changes.

**Result.** The managed server starts with the isolated prompt from
`opencode_voice/voice_agent.md`. Health reports `opencode_agent_missing` if the
agent cannot be observed. Doctor is diagnostic only; startup preflight may warn,
but it does not write global files or modify agent configuration.

**Planning implication.** Managed-server launch and health are the authoritative
voice-agent verification point. Future prompt or model changes need testing
through the managed configuration overlay. Support guidance must not ask users
to add `voice-build` to their normal OpenCode config for managed `/mortic`.

### Use workspace identity as the helper reuse boundary

**Decision.** Helper reuse requires both `ready: true` and a matching normalized
`workspace_dir` from `/api/health`. A healthy helper for another workspace is
not reused.

**Why.** Reusing by port ownership alone would let a second workspace connect to
the first workspace's managed server and lane. That is a source-context and
cleanup violation, not merely an inconvenient error.

**Result.** The sidepod probes ownership before confirmation. A foreign owner
produces a clear message, restores normal prompt focus, and is neither attached
to nor stopped. The normal helper endpoint is still `127.0.0.1:8765`; dynamic
helper-port negotiation and multi-workspace takeover are deliberately deferred.

**Planning implication.** Multi-window support needs a real ownership protocol:
discovery, owner identity, consent, acknowledged handoff, old-client state
restoration, and failure recovery. It must not be implemented as a retry loop
around port `8765`.

### Preserve source-thread safety with ephemeral voice forks

**Decision.** The helper continues to create `[voice tmp]` forks for voice work
and deletes them by default when the lane ends. It rejects `/mortic` when the
focused source session is already a temporary voice fork.

**Why.** The source chat must remain untouched, while the voice lane needs an
independent session for agent configuration, compaction, aborts, and temporary
turn output. Starting from a temporary fork made cleanup and fork creation race
each other: stale cleanup could delete the focused source before it was forked.

**Result.** Temporary-session cleanup happens only after the requested source is
validated. Normal `stop`, WebSocket disconnect, and connection close delete the
active fork unless `keepFork` was explicitly requested. A stale temporary fork
can be reaped on a subsequent valid lane start.

**Critical caveat.** The implementation currently asks the managed server to
fork the session id supplied by the visible TUI. That assumes two OpenCode
servers operating in the same worktree can see the same session storage and
operate on it safely. The code follows the desired fork model, but the
repository does not yet contain a recorded Phase 0 result proving this behavior
against the installed OpenCode version.

**Planning implication.** Do not close the source-thread safety acceptance
criterion merely because unit tests pass. Record and repeat the cross-server
visibility/fork and concurrent-storage smokes. If either fails, change the
primary design to an isolated managed voice session seeded from an approved
source-context handoff; do not keep a separate managed server pointed at unsafe
shared storage.

### Make child-process cleanup an ownership protocol

**Decision.** Managed OpenCode is launched in its own process group and is
tracked by a lease in `~/.mortic/managed-opencode-leases.json` (or the
`MORTIC_MANAGED_OPENCODE_LEASE_PATH` override). Each lease records the helper
pid, managed pid, process group id, workspace, URL, and heartbeat.

**Why.** A helper crash or TUI teardown must not leave a shadow OpenCode server
behind. Conversely, a stale lease must not kill a process it does not own.

**Result.** Mortic has three cleanup paths:

1. Graceful helper shutdown terminates the owned process group and removes its
   lease.
2. A watchdog observes the helper pid and heartbeat; when the helper is gone or
   stale, it terminates the recorded owned process group.
3. A later managed start reaps stale leases, but only after validating that the
   recorded pid is still an `opencode serve` process in the recorded process
   group.

The heartbeat loop survives an individual lease-write failure and tries again,
which prevents a transient filesystem problem from making the watchdog falsely
kill a live voice server. When the TUI itself is disposed, it stops the helper
synchronously rather than relying on a delayed timer that may not run during
OpenCode teardown.

**Planning implication.** This is production-critical lifecycle code and
deserves failure-injection coverage alongside normal user-flow tests. Any future
server reuse or dynamic port design must preserve process-group identity and the
conservative ownership check before it can terminate anything.

### Separate text completion from spoken completion

**Decision.** A streamed OpenCode completion is not immediately treated as a
completed spoken turn. The helper defers `turn.complete` while native TTS is
still draining, flushes it on playback drain (or the controlled fallback), and
flushes a pending completion before a new turn prunes the previous seam.

**Why.** The sidepod can render all assistant text before the audio device has
finished playback. Switching, stopping, or beginning another turn in that gap
previously made full text visible while only part of it had been spoken. A
pending completion could also be lost when the next `turn.start` removed the
completed turn state.

**Result.** The UI can distinguish output that is visible from speech that is
still playing. Interrupted playback remains an interruption, not a successful
spoken completion. A new turn cannot silently discard the previous turn's
deferred completion event.

**Planning implication.** Product telemetry and acceptance tests need separate
timestamps for final assistant text, first audio, audio drain, and interruption.
Do not define "turn complete" as only model-stream completion in future UI or
performance work.

### Treat startup errors as actionable lane states

**Decision.** Helper health problems become safe `Voice Bridge Issue` states.
Retryable offline failures keep Mortic focus so `M` really retries; failures
that require user action, such as a foreign-workspace helper or a temporary
source session, restore prompt focus.

**Why.** A toast saying "M to retry" is incorrect if the failure path has
already popped Mortic focus. Equally, a state that cannot progress without a
user changing context should not keep consuming the user's keyboard input.

**Result.** The current failure taxonomy distinguishes retryable lane
availability from start-blocking conditions. Repeated retry failures can surface
a current reason rather than being hidden by a stale offline-toast guard.

**Planning implication.** New error codes must declare whether they are
retryable, start-blocking, or terminal. Platform and Engine should test the
corresponding focus behavior together; an error's text alone is not sufficient
acceptance evidence.

## Lifecycle Sequences

### Normal managed start

```text
User invokes /mortic in an original OpenCode session
  -> sidepod checks source session and rejects [voice tmp] sources
  -> sidepod probes helper ownership at the configured helper URL
  -> same-workspace ready helper: connect
     otherwise no valid helper: request confirmation
  -> user confirms
  -> sidepod spawns mortic-helper --managed-opencode --opencode-dir <workspace>
  -> helper reaps eligible stale leases
  -> helper starts owned `opencode serve` on a free port
  -> helper injects OPENCODE_CONFIG_CONTENT with voice-build
  -> helper writes lease, starts watchdog and heartbeat
  -> health verifies dependencies and the voice agent
  -> sidepod opens /ws/sidepod and sends `start`
  -> helper validates source, cleans stale temporary forks, creates active fork
  -> sidepod enables the mic control after `ready`
```

The managed OpenCode port is selected dynamically. The helper port is not:
`8765` is the v1 discovery point unless a developer supplies
`MORTIC_HELPER_URL`.

### Normal stop and teardown

```text
User ends Mortic session, changes thread, or closes the sidepod
  -> sidepod sends protocol `stop` and waits briefly for `stopped`
  -> helper stops capture and TTS, deletes the active fork by default
  -> sidepod closes its lane WebSocket
  -> normal release: sidepod stops the helper after stop acknowledgment/timeout
     disposal: sidepod stops the helper immediately
  -> helper terminates the owned managed OpenCode process group
  -> helper removes its lease
```

If graceful teardown does not happen, the watchdog and stale-lease reaper are
the recovery paths. They are safeguards, not a reason to relax normal teardown
requirements.

### Foreign-workspace helper

```text
Second workspace invokes /mortic while port 8765 is owned elsewhere
  -> sidepod reads helper health and workspace_dir
  -> workspace mismatch: show Voice Bridge Issue / actionable message
  -> do not show a misleading managed-start confirmation
  -> restore normal prompt focus
  -> do not attach to, stop, or retarget the foreign helper
```

This is intentionally conservative. Full takeover has not shipped.

## Invariants Future Work Must Preserve

- The visible TUI server URL is optional diagnostic information, never a normal
  managed-path dependency.
- `/mortic` starts from an original source conversation, never from
  `[voice tmp]`.
- The normal managed path never writes or repairs global OpenCode configuration.
- A reusable helper must be healthy and match the requested workspace.
- Mortic terminates only a process group it can validate as its own
  `opencode serve` child.
- The source conversation is not mutated by voice work; temporary forks are
  deleted by default.
- Completion reaches the sidepod only when the relevant speech lifecycle is
  resolved as drained, timed out under an explicit policy, or interrupted.
- Normal UI reports safe capability states, not provider credentials, raw
  errors, model names, or internal URLs.

## Planning Changes

### Re-prioritize validation before more fork-dependent features

The existing execution plan treats ephemeral forks as a core engine deliverable.
With a dedicated managed server, the critical unknown is no longer merely
whether the helper can call the OpenCode API. It is whether the managed server
and visible TUI have safe shared access to source sessions.

Before implementing or declaring complete the following, run and retain Phase 0
evidence against the installed OpenCode version:

- refresh/reset from the current source session;
- long-running compaction on a voice fork;
- cross-window handoff or takeover;
- source-thread preservation claims in a managed-server environment;
- beta/release readiness for the managed-fork path.

The smoke must create a source session through one server, read and fork it
through a second server in the same worktree, exercise create/update/fork/delete
operations from both, then reopen storage to verify integrity. Record OpenCode
version, commands, response codes, logs, and the exact conclusion in the repo.

### Reframe helper distribution and onboarding

The distribution contract is now two local processes, not one:

- a Mortic helper exposed at a known local endpoint;
- an OpenCode server privately owned by that helper for the active workspace.

Installation, troubleshooting, and support material should teach the user only
the Mortic interaction: invoke `/mortic`, approve managed startup, then use the
sidepod. It should not ask them to find, expose, or configure the visible TUI's
HTTP port. Developer documentation can retain explicit attach overrides.

### Preserve protocol boundaries while updating assumptions

The v0 `start.opencodeUrl` field remains compatible as an optional debug/attach
hint. It must not silently become a required production routing field again. The
sidepod-to-helper WebSocket protocol is still the Platform/Engine boundary;
managed-server ownership remains internal to Engine.

If a future design needs source-context seeding rather than a shared-storage
fork, define the required handoff material and privacy rules explicitly. Do not
leak transcript, raw tool output, credentials, or global OpenCode configuration
across that boundary as an ad hoc fallback.

### Add lifecycle evidence to release readiness

The managed process model adds these release checks:

- confirmed start and reject paths leave expected process state;
- OpenCode TUI exit kills the managed helper/server rather than orphaning them;
- helper crash triggers watchdog cleanup;
- stale leases never kill an unrelated process;
- a transient lease write failure does not stop heartbeats;
- a stale temporary fork is cleaned without deleting the requested source;
- text completion, TTS drain, interruption, and a subsequent new turn produce
  the correct sidepod event order.

### Keep deferred work deliberately deferred

The following are real hardening items, but they are not silently included in
the v1 managed-server architecture:

- dynamic helper-port negotiation and sidepod handoff-file discovery;
- cross-workspace/multi-window takeover with a durable owner registry;
- hardening the `--print-config` path so it cannot create an unnecessary
  managed child;
- making process inspection resilient to truncated `ps` output;
- cancelling every delayed completion task during connection close;
- serializing concurrent lease updates to prevent lost writes;
- confirming managed-process death after SIGKILL before removing the lease;
- narrowing broad content-policy error matching so retryable failures are not
  treated as permanent policy failures.

These should remain individual bug or reliability tickets, with production
telemetry used to decide when alternate helper ports and takeover need promotion
from deferred design to active scope.

## Evidence and Test Map

The change has focused automated coverage in these areas:

- `tests/test_managed_opencode.py`: managed process-group launch, leases,
  watchdog/reaper behavior, conservative ownership checks, and heartbeat
  resilience.
- `tests/test_sidepod_lane.py`: temporary-source rejection, same-workspace lane
  exclusion, fork cleanup, deferred completion flushing, and speech lifecycle.
- `tests/test_doctor.py`: doctor remains diagnostic and has no global-config
  repair path.
- `opencode_mercury_sidepod/tests/helper-launcher.test.mjs`: managed helper
  arguments, health-based workspace matching, and foreign-owner blocking.
- `opencode_mercury_sidepod/tests/package.test.mjs`: sidepod startup, focus,
  and teardown contracts.

Automated tests prove local contracts. The following remain live acceptance
smokes because they depend on the installed OpenCode runtime and host TUI:

1. Cross-server source-session visibility and fork.
2. Concurrent access to the same worktree/session storage.
3. Runtime behavior of the managed-start confirmation, including Enter and Esc.
4. `OPENCODE_CONFIG_CONTENT` exposing `voice-build` and selecting it for an
   actual managed turn.
5. Bare TUI startup with no reachable visible TCP server.
6. Helper crash cleanup of the managed server.

## Code Ownership Map

- `opencode_mercury_sidepod/src/tui.js`: `/mortic` focus, start confirmation,
  helper ownership decisions, prompt restoration, lane WebSocket, and immediate
  shutdown behavior.
- `opencode_mercury_sidepod/src/helper-launcher.mjs`: helper command resolution,
  readiness probing, workspace matching, managed launch arguments, and helper
  process ownership from the plugin process.
- `opencode_mercury_sidepod/src/host-context.mjs` and `src/index.js`: capture
  the host server URL for diagnostics only.
- `opencode_voice/__main__.py`: managed/helper CLI modes, free managed-server
  port selection, config overlay construction, stale reaping, and helper shutdown.
- `opencode_voice/managed_opencode.py`: leases, heartbeat, watchdog launch,
  conservative reaping, and owned process-group termination.
- `opencode_voice/managed_watchdog.py`: out-of-process crash cleanup watcher.
- `opencode_voice/server.py`: health/readiness issues, workspace identity,
  sidepod lane lifecycle, voice-fork cleanup, and TTS-aware completion.
- `opencode_voice/doctor.py` and `opencode_voice/config.py`: diagnostics and
  managed voice-agent configuration without global config mutation.

## Decision Gate for the Next Architecture Step

The next fork-dependent engineering decision is binary and should be made from
recorded Phase 0 evidence:

| Observation | Primary architecture | Consequence |
| --- | --- | --- |
| A second server can see/fork the source session, and concurrent storage is safe | Managed server plus ephemeral fork | Continue the current design; make the smoke a release regression test. |
| The second server cannot see/fork the source session | Isolated managed voice session seeded from approved source context | Stop presenting shared-session forks as a guarantee; design the context handoff. |
| Concurrent access corrupts, locks, or loses state | Do not run dual servers against the same storage | Isolate voice storage or require a supported single-server topology. |

Until this gate has durable evidence, the managed server should be treated as an
implemented reliability foundation with a fork-model compatibility dependency,
not as final proof that every source-thread safety goal is complete.

