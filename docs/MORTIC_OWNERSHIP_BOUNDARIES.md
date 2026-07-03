# Mortic Platform Engine Ownership Boundaries

Status: MOR-134 boundary draft for owner review
Date: 2026-07-02
Sources: `docs/MORTIC_PROJECT_EXECUTION_PLAN.md` sections 3-5, `docs/MORTIC_CURRENT_CODE_INVENTORY.md`

## Purpose

This document prevents duplicate ownership between the native OpenCode sidepod surface and the invisible voice engine. When a file or feature crosses tracks, the owning track decides implementation details and the other track consumes only the agreed contract.

## Platform-Owned Surface

Platform owns the OpenCode-native product surface and OpenCode integration behavior.

Current repo modules:

- `opencode_mercury_sidepod/`
- Future sidepod `src/`, build, fixture, and TUI test files
- Sidepod-facing package metadata and install notes

Platform-owned responsibilities:

- Native sidepod UX, layout, retained Mortic visual language, sprite states, COMMS, Transcript, Handoff, and Config stub.
- `/mortic` focus entrypoint and prevention of prompt leakage.
- Focus-mode key handling, including `M` mic-toggle isolation (PTT and Live merged 2026-07-03), Clear, Refresh, Config, Transcript, Handoff, and Esc/End Session behavior.
- Confirmation UX for Refresh and Esc.
- Protocol client that sends v0 sidepod commands and renders v0 engine events.
- OpenCode sandboxing, plugin lifecycle, sidebar slots, keymap layers, and install constraints.
- User-facing bridge failure copy, including rendering `Voice Bridge Issue` without provider, model, or runtime names.

Platform does not own:

- Provider calls, secrets, model-serving mechanics, mic capture, STT, TTS, fork turn execution, speech filtering, compaction, or latency internals.

## Engine-Owned Surface

Engine owns the invisible helper and voice/runtime behavior behind the sidepod.

Current repo modules:

- `opencode_voice/server.py`
- `opencode_voice/opencode_client.py`
- `opencode_voice/deepgram.py`
- `opencode_voice/state.py`
- `opencode_voice/config.py`
- `opencode_voice/__main__.py`
- `opencode_voice/voice_agent.md`
- Future helper packaging, native capture, protocol transport, metrics, and helper tests

Engine-owned responsibilities:

- Helper/runtime distribution artifact and launch/discovery contract.
- OS microphone capture or documented native capture integration plan.
- Deepgram Flux STT and Deepgram Speak/Aura TTS behavior.
- Mercury/OpenCode fork turn loop and provider/runtime configuration.
- Ephemeral fork creation, cleanup, and source-thread untouched guarantees.
- Event-first OpenCode streaming, polling fallback, barge-in, compaction, speech filtering, and latency metrics.
- Structured helper health, readiness, lifecycle logs, and `voice_bridge_issue` diagnostics.
- Secret loading and redaction for provider keys and raw provider payloads.

Engine does not own:

- Sidepod visual layout, OpenCode focus/key behavior, user-facing config UX, command deck labels, or OpenCode plugin installation UI.

## Shared Surface

Shared ownership is intentionally narrow.

Shared modules and artifacts:

- `protocol/` — the normative TypeScript contract source (`schema.ts`), its generator, and the canonical JSON Schema (added 2026-07-04; the generated runtime copies live inside each track's package and are regenerated together via `npm run gen` in `protocol/`)
- `docs/MORTIC_PROTOCOL_V0.md`
- `docs/MORTIC_CURRENT_CODE_INVENTORY.md`
- Protocol fixtures and contract examples
- Cross-track demo notes and acceptance evidence
- Beta/release readiness checklists and cross-track bug triage

Shared-owned responsibilities:

- v0 protocol message names, payload examples, event ordering, versioning, and change approval.
- Shared fixtures that Platform and Engine both consume.
- First end-to-end demo acceptance, including observed state ordering.
- Security/privacy review of secrets, logs, mic permission behavior, and fork cleanup.
- Backlog ownership review when scope crosses Platform and Engine.

Shared does not own:

- Detailed implementation inside Platform UI modules or Engine helper modules after the protocol boundary is agreed.

## Handoff Rules

- Platform sends only v0 commands to Engine; Engine must not require Platform to handle raw audio, provider keys, provider payloads, or model/runtime details.
- Engine sends only v0 events to Platform; Platform must translate those events into product states and must not display protocol names directly unless in developer-only logs.
- Unknown fields are tolerated by both tracks; unknown message types are logged and ignored.
- Protocol changes require both-owner approval and an update to `docs/MORTIC_PROTOCOL_V0.md`. Since 2026-07-04 they also require editing `protocol/schema.ts` and regenerating the artifacts — both test suites fail on a schema edit without regeneration, and both runtimes validate at the WebSocket boundary (the Engine lane fails closed).
- Browser-backed UI under `opencode_voice/static/` is reference-only for WP-1 and must not become the packaged product surface.
- Source OpenCode threads remain untouched; voice work belongs to ephemeral forks owned by Engine and rendered by Platform.

## Acceptance Note

This boundary document is ready for Platform and Engine owner review. For WP-1, the user verification review is the owner acceptance gate.

