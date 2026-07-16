# Mortic Project Execution Plan

Status: Draft
Owners: Platform Track, Engine Track
Source context: `docs/MORTIC_OPENCODE_SIDEPOD_PRD.md`

> Revised 2026-07-03 (owner-directed after live testing; see the Revision section at the top of the PRD): PTT and Live are merged into a single mic mute/unmute toggle on `M`, emitting `live.set`. `ptt.start`/`ptt.stop` stay defined in protocol v0 but are not sent by the v1 sidepod. The shipped command deck is `[M] Microphone`, `[X] Clear Lane`, `[T] Transcript`, `[H] Handoff`, `[ESC] End Session` (`C Config` deferred to MOR-100; confirmed `R` Refresh lands with the engine integration). Esc is never destructive — ending a session is an explicit confirm inside the End Session dialog. Lines below that conflict are superseded.

## 1. Shared Product Requirements

Both tracks must align on these requirements before implementation.

- Mortic is a native OpenCode sidepod.
- `/mortic` focuses the sidepod and is not sent as a model prompt.
- While Mortic is focused, normal typing must not advance the OpenCode thread.
- `M` (mic toggle; revised 2026-07-03) must be isolated from other OpenCode keymaps.
- Command deck (revised 2026-07-03): `[M] Microphone`, `[X] Clear Lane`, `[T] Transcript`, `[H] Handoff`, `[ESC] End Session`.
- `R` and `Esc` must ask for confirmation before reset/exit actions run.
- `C Config` is a non-functional stub in v1.
- No visible browser UI ships in the packaged main path.
- Existing sidepod color scheme and sprite style should be retained as much as possible.
- Sprite state text goes through the sprite itself: `ready`, `listening`, `thinking`, `speaking`, `interrupted`, `voice issue`.
- User-facing bridge error copy is `Voice Bridge Issue`.
- No model/runtime/provider names appear in normal UI.
- Voice work happens in ephemeral forks.
- Source OpenCode thread remains untouched.
- Code, diffs, commands, paths, and JSON must not be spoken aloud.
- Latency must not regress from the current browser-backed technical reference.
- Engine owns helper/runtime distribution and Mercury serving/proxy concerns.
- Platform owns the OpenCode plugin surface and adapts Engine capabilities into whatever OpenCode allows, including sandboxing, permissions, plugin lifecycle, and adjacent UI surfaces.

## 2. Shared Protocol Requirements

The Sidepod <-> Engine protocol is owned jointly. Changes require both owners to approve before implementation.

### Sidepod Sends

`start`
- Sent when `/mortic` focuses the sidepod and the sidepod needs an active voice lane.
- Payload: active OpenCode session id, keep-fork flag, optional sidepod protocol version.

`ptt.start` (defined in v0; not sent by the v1 sidepod — revised 2026-07-03)
- Original: sent on isolated `M` press in Mortic focus mode.
- Payload: turn id or client event id, timestamp.

`ptt.stop` (defined in v0; not sent by the v1 sidepod — revised 2026-07-03)
- Original: sent on `M` release or PTT cancellation.
- Payload: matching turn/client event id, timestamp.

`live.set`
- Sent on every `M` press — the sole voice-capture control in v1 (mic mute/unmute; revised 2026-07-03).
- Payload: `{ "value": true | false }`, timestamp.

`refresh`
- Sent only after user confirms `R`.
- Payload: reason, current voice lane id if known.

`barge_in`
- Sent when platform detects explicit interruption intent, or when user action requires active speech to stop.
- Payload: reason, current turn id if known.

`confirm.response`
- Sent for confirmation prompts.
- Payload: prompt id, action id (`refresh` or `exit`), confirmed boolean.

### Engine Sends

`ready`
- Engine/helper is connected and can accept controls.
- Payload: voice lane id, current high-level state.

`listening`
- STT is active and accepting audio.
- Payload: mode (`ptt` or `live`), optional turn id.

`transcript`
- STT transcript update.
- Payload: text, final boolean, turn id, optional confidence/timing.

`thinking`
- User turn has been submitted to OpenCode/Mercury and the assistant is not yet speaking.
- Payload: turn id, source mode.

`assistant.delta`
- Streamed assistant text for COMMS.
- Payload: delta text, turn id, sequence number.

`speaking`
- TTS has started or is actively playing.
- Payload: turn id, optional first-audio latency.

`complete`
- Turn finished.
- Payload: turn id, full spoken text if available, latency fields, optional token summary.

`interrupted`
- Active turn or TTS was interrupted.
- Payload: turn id, reason.

`voice_bridge_issue`
- Helper cannot proceed.
- Payload: safe user-facing message, diagnostic code, retryable boolean.

### Protocol Rules

- Message names above are the v0 contract.
- Unknown fields must be ignored.
- Unknown message types must be logged and ignored.
- Engine must never send raw provider keys, raw provider responses, or model/provider names to normal UI.
- Sidepod must translate protocol messages into product states, not display protocol names directly.
- Protocol changes require both owners to approve and update this document or a protocol appendix.

## 3. Platform Track

Owner goal: own the OpenCode product surface, OpenCode plugin install path, config stub UX, future account/API proxy UI surface, and sidepod behavior. Platform does not own model serving or provider secrets. Platform folds the Engine-provided Mercury/helper capability into OpenCode's plugin, sandboxing, permissions, lifecycle, and adjacent-surface constraints.

### P1: Native Sidepod Shell

Goal: establish the native OpenCode sidebar as the only packaged UI surface.

Deliverables:
- Native sidepod plugin renders in OpenCode.
- Existing color scheme, borders, typography, and sprite style are retained.
- Hero sprite shows state text inside the sprite, not below it.
- Command deck layout matches the v1 command list.
- No visible browser UI entrypoint is exposed in packaged flow.

User acceptance criteria:
- User sees a Mortic sidepod inside OpenCode.
- Sidepod looks like the existing extension, not a redesigned product.
- User sees `[M] Microphone`, `[X] Clear Lane`, `[T] Transcript`, `[H] Handoff`, `[ESC] End Session` (revised 2026-07-03).

Engine-owner acceptance criteria:
- Platform exposes a stable place to render engine state.
- Platform can show `ready`, `listening`, `thinking`, `speaking`, `interrupted`, and `voice issue`.
- Platform has no dependency on engine internals beyond the protocol.

### P2: `/mortic` Focus + Key Isolation

Goal: make `/mortic` the safe entrypoint into Mortic focus mode.

Deliverables:
- `/mortic` focuses the sidepod and is not sent as a model prompt.
- Normal typing is blocked from advancing the OpenCode thread while Mortic is focused.
- `M` keydown/repeat/keyup are captured inside Mortic focus mode.
- `M` does not leak into OpenCode prompt or other OpenCode keymaps.

User acceptance criteria:
- User types `/mortic` and focus moves to Mortic.
- User can tap `M` to toggle the mic without side effects in OpenCode (revised 2026-07-03; hold model retired).
- User cannot accidentally send a parallel typed instruction while speaking to Mortic.

Engine-owner acceptance criteria:
- Engine receives clean `ptt.start` and `ptt.stop` events.
- Engine does not receive duplicate PTT events from key repeat.
- Engine can rely on Platform to prevent prompt/keymap leakage.

### P3: Command Deck + Confirmations

Goal: make command deck actions deterministic and safe.

Deliverables:
- `M`, `L`, `R`, `C`, `T`, `H`, and `Esc` are wired in Mortic focus mode.
- `R` opens confirmation: `Exit current Mortic voice lane and start fresh? Y/N`.
- `Esc` opens confirmation: `Exit Mortic and return to OpenCode prompt? Y/N`.
- `C Config` is visible and non-functional.
- `C` still means copy inside Transcript and Handoff popups.

User acceptance criteria:
- User cannot reset the voice lane accidentally.
- User cannot exit Mortic accidentally.
- Config is visible but clearly not active in v1.

Engine-owner acceptance criteria:
- Engine receives `refresh` only after user confirmation.
- Engine receives no signal for declined confirmations.
- Platform can send `confirm.response` where engine needs explicit auditability.

### P4: COMMS / Transcript / Handoff / Config UI

Goal: render all user-visible voice artifacts.

Deliverables:
- COMMS shows the current user transcript, transient generic work activity, and the final validated display response.
- COMMS auto-scrolls/re-renders to keep active text visible.
- Transcript popup is scrollable and copyable.
- Handoff popup is scrollable, generate/preview/copy capable.
- Config stub renders disabled or placeholder-only.

User acceptance criteria:
- User can follow the active turn in COMMS.
- User can inspect and copy Transcript.
- User can generate and copy Handoff.
- User sees Config but cannot configure anything yet.

Engine-owner acceptance criteria:
- Platform accepts one atomic `assistant.delta` for the validated final and treats earlier `thinking.activity` events as transient presentation only.
- Platform stores enough local turn data for Transcript/Handoff.
- Platform can request handoff generation through the agreed protocol or helper route.

### P5: Engine Protocol Client

Goal: connect sidepod to the invisible helper through the shared protocol.

Deliverables:
- Client connects to helper.
- Client sends all v0 control messages.
- Client handles all v0 engine messages.
- Reconnect and `Voice Bridge Issue` handling are implemented.
- Protocol version mismatch handling is defined.

User acceptance criteria:
- Sidepod shows Ready when helper is available.
- Sidepod shows `Voice Bridge Issue` when helper is unavailable.
- User can retry through Refresh after bridge issues.

Engine-owner acceptance criteria:
- Platform follows protocol names and payload expectations.
- Platform ignores unknown fields.
- Platform logs unknown message types without breaking UI.

### P6: OpenCode Plugin Distribution + Packaging

Goal: make the OpenCode plugin installable without exposing development-only surfaces.

Deliverables:
- OpenCode plugin install path is documented.
- Packaged plugin can start/locate the Engine-distributed helper without visible browser UI.
- Browser technical reference is not exposed in normal product path.
- `.env`, local logs, and ignored archives are not shipped.
- OpenCode sandboxing, permissions, plugin lifecycle, and adjacent UI constraints are documented.

User acceptance criteria:
- User can install and open Mortic in OpenCode.
- User does not see browser UI during normal use.
- User does not need to know model/provider/runtime details.

Engine-owner acceptance criteria:
- Engine provides the helper/runtime distribution artifact and launch/discovery contract.
- Engine startup status is machine-readable through the agreed helper API.
- Platform can surface helper failures as `Voice Bridge Issue`.

### P7: Future Config/API Proxy UI Readiness

Goal: reserve the OpenCode-side UI surface for future accounts, API keys, subscription state, and proxy routing without owning the underlying model-serving system.

Deliverables:
- `C Config` stub is present and stable.
- Config UI data model draft exists but is not active.
- Future API proxy/account UI concepts are documented.
- No BYOK or subscription logic ships in v1.

User acceptance criteria:
- User sees where settings will live.
- User cannot enter credentials into an unfinished flow.

Engine-owner acceptance criteria:
- Engine knows no config data is required from Platform in v1.
- Engine owns future API proxy, provider key, subscription, and Mercury-serving mechanics.
- Platform documents future UI fields early enough for Engine planning.

## 4. Engine Track

Owner goal: own the invisible helper, helper/runtime distribution, Mercury serving/proxy path, provider/API key mechanics, mic capture, Deepgram STT/TTS, Mercury/OpenCode fork loop, streaming, barge-in, compaction, speech filtering, and latency.

### E1: Invisible Local Helper + Runtime Distribution Baseline

Goal: run and distribute the helper without visible browser UI in the packaged path.

Deliverables:
- Helper starts locally and exposes health/status.
- Helper has a documented distribution artifact and launch/discovery contract.
- Helper accepts v0 protocol controls.
- Helper emits `ready` and `voice_bridge_issue`.
- Helper owns OS mic capture path or the native capture integration plan.
- Helper owns provider/runtime configuration and does not require Platform to handle provider secrets.
- Existing browser-backed path remains only as technical reference.

User acceptance criteria:
- User does not see browser UI in normal flow.
- User sees Ready or `Voice Bridge Issue` in the sidepod.

Platform-owner acceptance criteria:
- Platform can connect to helper with one documented endpoint.
- Platform receives stable health and error payloads.
- Helper does not expose model/runtime/provider names to normal UI.
- Platform does not need to know Mercury/provider implementation details.

### E2: PTT Voice Loop

Goal: make `M Hold PTT` work end to end.

Deliverables:
- `ptt.start` opens capture/listening.
- `ptt.stop` finalizes transcript and submits the turn.
- Engine emits `listening`, `transcript`, `thinking`, `assistant.delta`, `speaking`, and `complete`.
- Duplicate PTT events are safely ignored.

User acceptance criteria:
- User holds `M`, speaks, releases, and gets a response.
- Transcript appears while listening.
- Response starts streaming quickly.

Platform-owner acceptance criteria:
- Engine emits clean turn ids and ordered transcript/delta events.
- Engine tolerates Platform key repeat suppression or duplicate event defense.
- Engine never requires Platform to handle raw audio bytes.

### E3: Live Mode + Barge-In

Goal: support continuous conversation and interruption.

Deliverables:
- `live.set true` starts continuous listening.
- `live.set false` stops continuous listening.
- Deepgram end-of-turn drives turn submission.
- User speech during TTS triggers barge-in.
- Engine emits `interrupted` when active speech/turn is interrupted.

User acceptance criteria:
- User can speak hands-free in Live.
- User can interrupt assistant speech naturally.
- Interrupted turns do not keep speaking.

Platform-owner acceptance criteria:
- Engine state messages are enough to update sprite and COMMS.
- Engine sends interruption reason and affected turn id.
- Engine handles barge-in without requiring Platform to inspect audio.

### E4: Mercury/OpenCode Fork Turn Loop

Goal: run voice turns safely against ephemeral OpenCode forks.

Deliverables:
- Active OpenCode source session is accepted from Platform.
- Engine serves Mercury/model capability to Platform through the helper/protocol, not through Platform-owned provider calls.
- Engine creates an ephemeral fork.
- Source thread remains untouched.
- Turns subscribe to `/event` before prompting and admit only the final strict `{displayText, spokenText}` result.
- Polling remains a low-rate hedge without cancelling the event reader.
- Fork is deleted by default when the voice lane ends.
- Model narration, ordinary text parts, tool arguments/results, partial JSON,
  and repair candidates are never sent to COMMS or TTS.

User acceptance criteria:
- Voice work does not mutate the source OpenCode thread.
- Assistant output appears quickly in COMMS.
- Fork cleanup happens without user intervention.

Platform-owner acceptance criteria:
- Engine provides voice lane/fork state where needed.
- Engine emits generic `thinking.activity` updates during work and one atomic validated final.
- Engine reports safe failure states as `voice_bridge_issue`.
- Platform can treat Mercury as an Engine-provided capability and focus on OpenCode constraints.

### E5: Structured Display/Spoken Safety

Goal: make Mercury author both renderings and reject unsafe or unnatural speech before admission.

Deliverables:
- Strict schema with separate `displayText` and `spokenText`, limited to 1,200 characters each.
- Code, diffs, commands, absolute paths, raw JSON, Markdown/URLs, secrets, and provider/runtime names are rejected.
- Literal parentheses, brackets, braces, and angle-bracket notation are rejected in speech and transformed semantically by Mercury.
- One prompt-based repair is allowed for repairable deterministic violations; invalid output never falls back to legacy prose.

User acceptance criteria:
- Mortic does not read code or notation punctuation aloud.
- User hears concise, useful spoken summaries.
- The displayed and spoken answers preserve the same facts, certainty, qualifications, and ordering.

Platform-owner acceptance criteria:
- Engine marks or separates spoken text from screen-only content.
- Platform can render assistant deltas without feeding unsafe content to TTS.
- Engine provides enough text for Transcript/Handoff without leaking raw payloads.

### E6: Refresh/Fork Reset + Compaction

Goal: make reset and context handling reliable.

Deliverables:
- `refresh` resets the current voice lane only after confirmation.
- Current fork is deleted/discarded.
- New fork is created from active source thread.
- COMMS/transcript/handoff reset is coordinated with Platform.
- Context threshold is 70k active tokens.
- Compaction runs only when threshold is truly crossed.
- Context overflow triggers compact-and-retry when possible.

User acceptance criteria:
- Refresh starts clean without touching source thread.
- No repeated unnecessary compactions.
- Long sessions continue without context failure where possible.

Platform-owner acceptance criteria:
- Engine emits clear reset lifecycle events.
- Engine does not require Platform to calculate token state.
- Engine reports compaction blocking/failure with safe copy.

### E7: Observability + Performance Targets

Goal: prove latency and reliability do not regress.

Deliverables:
- Logs include first transcript, end-of-turn, first assistant text, first TTS audio, total turn latency, barge-in, refresh, fork cleanup, and compaction.
- Performance report compares helper path to current browser-backed reference.
- Error taxonomy exists for `voice_bridge_issue`.
- Minimal replay/debug artifact exists for failed turns.

User acceptance criteria:
- Voice feels at least as fast as the current reference.
- Failures are understandable and recoverable.

Platform-owner acceptance criteria:
- Engine exposes enough timing data for sidepod diagnostics.
- Platform can show `Voice Bridge Issue` without raw errors.
- Performance data is stable enough for release decisions.

## 5. Shared Milestones

### S1: Protocol Freeze v0

Owner responsibilities:
- Platform: confirm UI states and needed controls.
- Engine: confirm message names, payloads, and event ordering.

Required demo:
- Mock sidepod client sends v0 controls.
- Mock engine emits v0 state messages.

User acceptance criteria:
- Product behavior can be described without implementation-specific terms.

Cross-track acceptance criteria:
- Both owners approve protocol v0.
- Protocol changes after this require documented approval.

### S2: First End-to-End Demo

Owner responsibilities:
- Platform: `/mortic`, command deck, COMMS, basic protocol client.
- Engine: helper, PTT, forked OpenCode turn, streaming deltas, TTS.

Required demo:
- User enters `/mortic`, holds `M`, speaks, releases, sees transcript and assistant stream, hears TTS.

User acceptance criteria:
- No browser UI appears.
- Source OpenCode thread remains untouched.
- User can complete one PTT turn.

Cross-track acceptance criteria:
- Platform and Engine agree on observed state ordering.
- Logs include enough timing to inspect latency.

### S3: Latency/Quality Pass

Owner responsibilities:
- Platform: remove UI rendering delays, verify COMMS updates as deltas arrive.
- Engine: optimize STT/TTS/OpenCode timing, barge-in, and chunking.

Required demo:
- Run a scripted set of PTT and Live turns against current browser-backed reference timings.

User acceptance criteria:
- First text and first audio do not regress materially from reference.
- Barge-in works without stale speech.

Cross-track acceptance criteria:
- Performance report is reviewed by both owners.
- Any regression has an owner and fix plan.

### S4: Beta Readiness

Owner responsibilities:
- Platform: install path, focus/key isolation, confirmations, transcript/handoff/config stub.
- Engine: helper reliability, fork cleanup, speech filtering, compaction, observability.

Required demo:
- Keyboard-only session covering PTT, Live, Refresh confirmation, Esc confirmation, Transcript copy, Handoff copy, Config stub, and bridge failure.

User acceptance criteria:
- User can complete a normal voice session without mouse or browser UI.
- User-facing errors use `Voice Bridge Issue`.
- No model/provider/runtime names appear in normal UI.

Cross-track acceptance criteria:
- All v0 protocol messages are covered by tests or smoke checks.
- Beta known issues are documented.

### S5: Release Readiness

Owner responsibilities:
- Platform: package, install docs, final sidepod UX checks.
- Engine: release helper, performance report, operational logs, failure handling.

Required demo:
- Fresh install on a clean machine or clean user profile.
- Full PTT and Live workflow.
- Refresh and Esc confirmations.
- Fork cleanup verified.

User acceptance criteria:
- Install works from documented steps.
- Voice interaction works without exposing hidden infrastructure.
- No source thread mutation from voice turns.

Cross-track acceptance criteria:
- Release checklist is complete.
- Rollback plan exists.
- Protocol version is tagged.

## 6. Definition of Done

Repo:
- Source tree contains only packaged product code, docs, tests, and required build metadata.
- Benchmark/reference artifacts remain ignored or external.
- No secrets, `.env`, logs, run outputs, or `node_modules` are tracked.

Product:
- Native OpenCode sidepod is the packaged UI.
- `/mortic` focuses the sidepod and is not sent to the model.
- Command deck matches v1.
- `R` and `Esc` confirmation flows work.
- `C Config` exists as a non-functional stub.
- No visible browser UI in the main path.
- No model/runtime/provider names in normal UI.

Runtime:
- Invisible helper owns mic capture, STT, TTS, OpenCode fork turns, barge-in, compaction, and filtering.
- Voice turns run in ephemeral forks.
- Source OpenCode thread remains untouched.
- Fork cleanup works by default.

Protocol:
- v0 protocol is documented, versioned, and accepted by both owners.
- Platform and Engine tolerate unknown fields.
- Protocol changes are reviewed by both owners.

Testing:
- Unit tests cover protocol parsing, focus/key handling, state mapping, speech filtering, refresh/reset, and compaction gates.
- Integration smoke covers `/mortic`, PTT, Live, barge-in, Refresh, Esc, Transcript, Handoff, Config stub, and helper failure.
- Keyboard-only run passes.

Performance:
- First assistant text, first TTS audio, and total turn latency do not regress from the current browser-backed technical reference.
- Barge-in stops stale speech reliably.
- Performance report is attached before beta and release.

Release:
- OpenCode plugin install path and Engine helper distribution path are documented.
- User-facing failures use `Voice Bridge Issue`.
- Debug logs are available without leaking secrets.
- Rollback path is documented.
