# Mortic Sidepod Engine Protocol v0

Status: command payload draft for MOR-163
Date: 2026-07-02
Source: `docs/MORTIC_PROJECT_EXECUTION_PLAN.md` section 2

## Scope

This document defines concrete JSON examples for the first Sidepod <-> Engine protocol. The protocol is a local control/event contract between the native OpenCode sidepod and the invisible Mortic helper.

Field names use lower camel case. All timestamps are ISO 8601 UTC strings. Identifiers in examples are illustrative and must not be treated as fixed formats unless a later section explicitly says so.

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

- `protocolVersion`: requested protocol version. In v0 examples this is `mortic.sidepod.v0`.
- `workspacePath`: current workspace path when OpenCode exposes it.

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

Engine-to-sidepod event payloads are documented by MOR-164.

