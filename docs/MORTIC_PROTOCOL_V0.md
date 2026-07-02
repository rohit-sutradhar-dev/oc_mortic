# Mortic Sidepod Engine Protocol v0

Status: frozen v0 contract for MOR-135
Date: 2026-07-02
Source: `docs/MORTIC_PROJECT_EXECUTION_PLAN.md` section 2

## Scope

This document defines concrete JSON examples for the first Sidepod <-> Engine protocol. The protocol is a local control/event contract between the native OpenCode sidepod and the invisible Mortic helper.

Field names use lower camel case. All timestamps are ISO 8601 UTC strings. Identifiers in examples are illustrative and must not be treated as fixed formats unless a later section explicitly says so.

## Contract Freeze

The frozen protocol version tag is `mortic.sidepod.v0`.

Lane negotiation:

- New WP-1 implementations must send `protocolVersion: "mortic.sidepod.v0"` on `start`.
- The Engine must echo `protocolVersion: "mortic.sidepod.v0"` on `ready` before the sidepod treats the lane as fully connected.
- After `ready`, messages on the same lane are interpreted as v0 unless a future approved version explicitly changes that rule.
- If `start` omits `protocolVersion`, the Engine may treat it as v0 for bootstrap compatibility, but should log the omission for developer diagnostics.
- If the Engine cannot support the requested protocol version, it must emit `voice_bridge_issue` with `diagnosticCode: "protocol_version_unsupported"` and safe user copy only.

Compatibility rules:

- Receivers must ignore unknown fields on known message types.
- Receivers must log and ignore unknown message types.
- Missing required fields make a message invalid. The Engine should respond to invalid sidepod commands with `voice_bridge_issue` and `diagnosticCode: "protocol_invalid_message"` when the transport is still usable. The sidepod should ignore invalid Engine events, keep the prior product state, and log a developer-only diagnostic.
- `sequence` is monotonic within a `turnId` and event type. Stale or duplicate sequence values must not regress sidepod state.
- Streaming events for inactive or superseded `turnId` values must not replace the active turn display.

Safety rules:

- Engine events must not include secrets, raw provider payloads, provider names, model names, or runtime names for normal UI.
- Sidepod UI must translate protocol messages into product states and must not display protocol type names directly outside developer-only logs.
- `assistant.delta` content with `screenOnly: true` must not be sent to TTS.

Change control:

- Post-v0 protocol changes require Platform and Engine owner approval and an update to this document.
- Adding optional fields is allowed only when both sides can ignore them safely and the field is documented here before use.
- Adding message types, removing fields, renaming fields, changing required fields, or changing event ordering is a protocol change.
- For WP-1, both-owner approval is recorded by the user verification review after this document and the MOR-136 fixtures are accepted.

## Sidepod Commands

Sidepod commands are sent from the OpenCode TUI sidepod to the Engine helper.

### `start`

Sent when `/mortic` focuses the sidepod and the sidepod needs an active voice lane for the current OpenCode session.

Required fields:

- `type`: `start`
- `clientEventId`: unique sidepod event id for local correlation.
- `sentAt`: client timestamp.
- `sourceSessionId`: active source OpenCode session id.
- `keepFork`: whether the helper should keep the voice fork after the lane ends.

Optional fields:

- `workspacePath`: current workspace path when OpenCode exposes it.

Contract field:

- `protocolVersion`: requested protocol version. New WP-1 implementations must send `mortic.sidepod.v0`.

Example:

```json
{
  "type": "start",
  "protocolVersion": "mortic.sidepod.v0",
  "clientEventId": "evt_sidepod_0001",
  "sentAt": "2026-07-02T04:00:00.000Z",
  "sourceSessionId": "ses_source_123",
  "keepFork": false,
  "workspacePath": "/Users/aeroknight/Documents/Fusion Self Benchmarking"
}
```

### `ptt.start`

Sent on isolated `M` press in Mortic focus mode. Key repeat must not emit duplicate active PTT starts for the same press.

Required fields:

- `type`: `ptt.start`
- `clientEventId`: unique sidepod event id for this press.
- `sentAt`: client timestamp.
- `turnId`: sidepod-selected turn id for the voice turn.
- `inputMode`: `ptt`.

Optional fields:

- `key`: physical/logical key used for push-to-talk.
- `eventType`: key event type, typically `press`.
- `terminalSupportsKeyRelease`: whether the sidepod believes hold-to-talk release events are available.

Example:

```json
{
  "type": "ptt.start",
  "clientEventId": "evt_sidepod_0002",
  "sentAt": "2026-07-02T04:00:01.000Z",
  "turnId": "turn_0001",
  "inputMode": "ptt",
  "key": "M",
  "eventType": "press",
  "terminalSupportsKeyRelease": true
}
```

### `ptt.stop`

Sent on `M` release, PTT cancellation, or tap-mode stop.

Required fields:

- `type`: `ptt.stop`
- `clientEventId`: unique sidepod event id for this stop.
- `sentAt`: client timestamp.
- `turnId`: turn id matching the active PTT turn.
- `reason`: why PTT stopped.

Optional fields:

- `matchingStartEventId`: `clientEventId` from the matching `ptt.start`.
- `eventType`: key event type, typically `release`, or `tap` for tap fallback.

Example:

```json
{
  "type": "ptt.stop",
  "clientEventId": "evt_sidepod_0003",
  "matchingStartEventId": "evt_sidepod_0002",
  "sentAt": "2026-07-02T04:00:04.000Z",
  "turnId": "turn_0001",
  "reason": "key.release",
  "eventType": "release"
}
```

### `live.set`

Sent when the user toggles Live voice mode.

Required fields:

- `type`: `live.set`
- `clientEventId`: unique sidepod event id.
- `sentAt`: client timestamp.
- `value`: `true` to enable Live, `false` to disable it.

Optional fields:

- `reason`: user or lifecycle reason for the toggle.

Example:

```json
{
  "type": "live.set",
  "clientEventId": "evt_sidepod_0004",
  "sentAt": "2026-07-02T04:00:10.000Z",
  "value": true,
  "reason": "user.toggle"
}
```

### `refresh`

Sent only after the user confirms `R`. Refresh resets the current Mortic voice lane and starts fresh from the current source OpenCode thread.

Required fields:

- `type`: `refresh`
- `clientEventId`: unique sidepod event id.
- `sentAt`: client timestamp.
- `reason`: refresh reason.

Optional fields:

- `voiceLaneId`: current voice lane id, if known.
- `sourceSessionId`: active source OpenCode session id, if the sidepod has it.
- `confirmedByPromptId`: confirmation prompt id that authorized the refresh.

Example:

```json
{
  "type": "refresh",
  "clientEventId": "evt_sidepod_0005",
  "sentAt": "2026-07-02T04:01:00.000Z",
  "reason": "user.confirmed_refresh",
  "voiceLaneId": "lane_123",
  "sourceSessionId": "ses_source_123",
  "confirmedByPromptId": "prompt_refresh_0001"
}
```

### `barge_in`

Sent when the sidepod detects explicit interruption intent or a user action requires active speech to stop.

Required fields:

- `type`: `barge_in`
- `clientEventId`: unique sidepod event id.
- `sentAt`: client timestamp.
- `reason`: interruption reason.

Optional fields:

- `turnId`: current turn id, if known.
- `voiceLaneId`: current voice lane id, if known.

Example:

```json
{
  "type": "barge_in",
  "clientEventId": "evt_sidepod_0006",
  "sentAt": "2026-07-02T04:01:30.000Z",
  "reason": "user.started_speaking",
  "turnId": "turn_0001",
  "voiceLaneId": "lane_123"
}
```

### `confirm.response`

Sent for confirmation prompts when Engine needs explicit auditability. Platform must not send destructive commands such as `refresh` when confirmation is declined.

Required fields:

- `type`: `confirm.response`
- `clientEventId`: unique sidepod event id.
- `sentAt`: client timestamp.
- `promptId`: confirmation prompt id.
- `actionId`: action being confirmed, currently `refresh` or `exit`.
- `confirmed`: whether the user confirmed the action.

Optional fields:

- `voiceLaneId`: current voice lane id, if known.

Example:

```json
{
  "type": "confirm.response",
  "clientEventId": "evt_sidepod_0007",
  "sentAt": "2026-07-02T04:01:31.000Z",
  "promptId": "prompt_refresh_0001",
  "actionId": "refresh",
  "confirmed": true,
  "voiceLaneId": "lane_123"
}
```

## Engine Events

Engine events are sent from the helper to the OpenCode TUI sidepod.

Streaming events that belong to a turn must include `turnId`. Repeated updates for a turn should include monotonically increasing `sequence` values per event type where ordering matters. Sidepod renderers must ignore stale turn ids when a newer active turn has superseded them.

### `ready`

Sent when the helper is connected and can accept sidepod controls.

Required fields:

- `type`: `ready`
- `sentAt`: helper timestamp.
- `voiceLaneId`: active voice lane id.
- `state`: high-level state, normally `ready`.

Optional fields:

- `sourceSessionId`: source OpenCode session id associated with the lane.
- `forkSessionId`: active voice fork id, if already created.

Contract field:

- `protocolVersion`: helper protocol version. New WP-1 implementations must send `mortic.sidepod.v0`.

Example:

```json
{
  "type": "ready",
  "sentAt": "2026-07-02T04:00:00.500Z",
  "protocolVersion": "mortic.sidepod.v0",
  "voiceLaneId": "lane_123",
  "state": "ready",
  "sourceSessionId": "ses_source_123",
  "forkSessionId": "ses_voice_tmp_456"
}
```

### `listening`

Sent when STT is actively accepting audio.

Required fields:

- `type`: `listening`
- `sentAt`: helper timestamp.
- `voiceLaneId`: active voice lane id.
- `mode`: `ptt` or `live`.

Optional fields:

- `turnId`: active turn id if listening belongs to a specific turn.

Example:

```json
{
  "type": "listening",
  "sentAt": "2026-07-02T04:00:01.100Z",
  "voiceLaneId": "lane_123",
  "mode": "ptt",
  "turnId": "turn_0001"
}
```

### `transcript`

Sent for interim and final STT transcript updates.

Required fields:

- `type`: `transcript`
- `sentAt`: helper timestamp.
- `turnId`: active turn id.
- `sequence`: transcript update sequence within the turn.
- `text`: transcript text.
- `final`: whether this transcript is final for the turn.

Optional fields:

- `confidence`: recognizer confidence when available.
- `timing`: safe timing metadata.

Example:

```json
{
  "type": "transcript",
  "sentAt": "2026-07-02T04:00:02.000Z",
  "turnId": "turn_0001",
  "sequence": 1,
  "text": "Make the test output easier to scan.",
  "final": false,
  "confidence": 0.91,
  "timing": {
    "firstTranscriptMs": 420
  }
}
```

### `thinking`

Sent after the final user transcript is submitted to the voice fork and before assistant speech begins.

Required fields:

- `type`: `thinking`
- `sentAt`: helper timestamp.
- `turnId`: active turn id.
- `sourceMode`: `ptt` or `live`.

Optional fields:

- `voiceLaneId`: active voice lane id.
- `submittedTextChars`: character count of submitted user text, not the raw text.

Example:

```json
{
  "type": "thinking",
  "sentAt": "2026-07-02T04:00:04.300Z",
  "turnId": "turn_0001",
  "sourceMode": "ptt",
  "voiceLaneId": "lane_123",
  "submittedTextChars": 36
}
```

### `assistant.delta`

Sent for streamed assistant text that COMMS can render as the current Mortic response.

Required fields:

- `type`: `assistant.delta`
- `sentAt`: helper timestamp.
- `turnId`: active turn id.
- `sequence`: assistant delta sequence within the turn.
- `delta`: text delta.

Optional fields:

- `screenOnly`: whether the delta is safe only for screen rendering and must not be fed to TTS.

Example:

```json
{
  "type": "assistant.delta",
  "sentAt": "2026-07-02T04:00:04.900Z",
  "turnId": "turn_0001",
  "sequence": 1,
  "delta": "I will tighten the failure summary and keep the detailed output in the thread.",
  "screenOnly": false
}
```

### `speaking`

Sent when TTS has started or is actively playing.

Required fields:

- `type`: `speaking`
- `sentAt`: helper timestamp.
- `turnId`: active turn id.

Optional fields:

- `voiceLaneId`: active voice lane id.
- `firstAudioLatencyMs`: latency from turn start to first playable audio.

Example:

```json
{
  "type": "speaking",
  "sentAt": "2026-07-02T04:00:05.300Z",
  "turnId": "turn_0001",
  "voiceLaneId": "lane_123",
  "firstAudioLatencyMs": 1320
}
```

### `complete`

Sent when a turn finishes.

Required fields:

- `type`: `complete`
- `sentAt`: helper timestamp.
- `turnId`: completed turn id.
- `latency`: safe timing summary.

Optional fields:

- `fullSpokenText`: final speakable text if available.
- `tokenSummary`: safe token summary for diagnostics or logs. Do not include provider payloads.
- `streamSource`: `event`, `poll`, or `poll_after_event`.

Example:

```json
{
  "type": "complete",
  "sentAt": "2026-07-02T04:00:08.000Z",
  "turnId": "turn_0001",
  "fullSpokenText": "I tightened the failure summary and kept the details in the thread.",
  "latency": {
    "firstTranscriptMs": 420,
    "firstAssistantTextMs": 900,
    "firstAudioMs": 1320,
    "totalMs": 7000
  },
  "tokenSummary": {
    "contextTokens": 18500,
    "source": "assistant_input"
  },
  "streamSource": "event"
}
```

### `interrupted`

Sent when active speech or an active turn is interrupted.

Required fields:

- `type`: `interrupted`
- `sentAt`: helper timestamp.
- `reason`: interruption reason.

Optional fields:

- `turnId`: affected turn id, if known.
- `voiceLaneId`: active voice lane id.

Example:

```json
{
  "type": "interrupted",
  "sentAt": "2026-07-02T04:00:06.500Z",
  "turnId": "turn_0001",
  "voiceLaneId": "lane_123",
  "reason": "barge_in"
}
```

### `voice_bridge_issue`

Sent when the helper cannot proceed safely. The payload separates user-facing copy from diagnostics.

Required fields:

- `type`: `voice_bridge_issue`
- `sentAt`: helper timestamp.
- `userMessage`: safe product copy. For bridge failures this must be `Voice Bridge Issue`.
- `diagnosticCode`: stable diagnostic code for logs and support.
- `retryable`: whether Refresh or reconnect may recover.

Optional fields:

- `safeDetail`: concise user-safe detail such as `Mic permission needed`.
- `voiceLaneId`: active voice lane id, if known.
- `debugRef`: opaque local log or run reference. Do not include secrets, raw provider payloads, or model/provider/runtime names.

Example:

```json
{
  "type": "voice_bridge_issue",
  "sentAt": "2026-07-02T04:00:09.000Z",
  "userMessage": "Voice Bridge Issue",
  "safeDetail": "Mic permission needed",
  "diagnosticCode": "mic_permission_needed",
  "retryable": true,
  "voiceLaneId": "lane_123",
  "debugRef": "run_20260702T040000Z"
}
```
