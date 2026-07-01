# Mortic OpenCode Plugin Handoff

This workspace now has two relevant pieces:

- `opencode_voice/`: current Python/FastAPI Deepgram Flux + Speak + Mercury/OpenCode voice bridge.
- `opencode_mercury_sidepod/`: native OpenCode TUI plugin reference for the Mortic sidebar UI.

The goal is to merge these into an OpenCode-native Mortic plugin experience, while keeping the existing Deepgram/Mercury bridge behavior.

## Current State

`opencode_voice/` already handles:

- OpenCode server discovery or managed `opencode serve` startup.
- Mercury/Inception provider config overlay for managed OpenCode.
- Session list, fork, fork cleanup, and voice-build agent use.
- Deepgram Flux STT over WebSocket.
- Deepgram Speak/Aura TTS over WebSocket.
- Browser microphone capture and PCM playback.
- Barge-in behavior.
- Speech filtering so code/diffs/paths/commands are not read aloud.
- Context threshold handling and compaction support.

`opencode_mercury_sidepod/` is the native OpenCode TUI reference. It currently provides:

- Right-sidebar Mortic box.
- Pulsing globe with subtle `thinking` label.
- Command deck: PTT, Live, Clear, Transcript, Handoff.
- Transcript and handoff popups.
- OpenCode TUI command-palette entries:
  - `Mortic: Focus sidepod`
  - `Mortic: Push to Talk`
  - `Mortic: Transcript`
  - `Mortic: Handoff`
- Keymap/focus-mode experiment:
  - `mortic.focus` in `~/.config/opencode/tui.json` is set to `ctrl+x v`.
  - Focus-mode keys are intended to be `p/l/c/t/h/esc`.

OpenCode global config has been pointed at the local workspace copy:

```text
/Users/aeroknight/Documents/Fusion Self Benchmarking/opencode_mercury_sidepod
```

## Product Direction

Do not build a separate Electron overlay for v1. Build Mortic as an OpenCode-native sidebar/plugin experience.

The practical shape should be:

1. Keep native TUI sidebar as the control/status surface.
2. Keep the existing Python bridge for actual mic capture and TTS playback, because terminal TUI plugins cannot directly access browser microphone APIs.
3. Let the TUI plugin start/connect to the local voice bridge and drive it over HTTP/WebSocket.
4. Use the existing browser bridge UI only as a fallback/debug surface, not the primary experience.

## Suggested Architecture

Smallest useful plugin integration:

- Add a managed bridge launcher in the TUI plugin or a companion server plugin.
- Start `python -m opencode_voice --opencode-url <current server> --port <free-port>` as a child process.
- TUI sidepod connects to the bridge:
  - `GET /api/health`
  - `GET /api/sessions`
  - WebSocket `/ws/voice`
- TUI actions send the same messages the browser UI sends today:
  - `{ "type": "start", "session_id": "...", "keep_fork": false }`
  - `{ "type": "stop" }`
  - `{ "type": "text", "text": "..." }`
  - `{ "type": "audio.start" }`
  - `{ "type": "audio.stop" }`
  - `{ "type": "keep_fork", "value": true/false }`
- For real mic input in v1, either:
  - keep a tiny browser capture page opened by the plugin, or
  - add a native helper later.

Recommended v1 compromise:

- TUI plugin is the main control/status panel.
- Browser capture page can be hidden/minimal and only handles microphone permission + audio IO.
- TUI shows state, transcript, handoff, and bridge status.

## Files To Start With

Read these first:

- `opencode_voice/__main__.py`
- `opencode_voice/server.py`
- `opencode_voice/deepgram.py`
- `opencode_voice/opencode_client.py`
- `opencode_voice/static/app.js`
- `opencode_mercury_sidepod/dist/tui.js`
- `opencode_mercury_sidepod/package.json`

## Important Constraints

- Keep source OpenCode sessions untouched.
- Voice work should happen against a fork.
- Delete ephemeral fork by default unless user opts to keep.
- Do not make TTS read code, commands, JSON, diffs, or paths.
- Keep Mercury runtime disclosure quiet, in status/settings only.
- Prefer `@opencode-ai/plugin/tui` APIs and OpenCode-native slots/keymap/commands.
- Do not hard-require archive session cleanup; delete/keep fallback is fine.
- Keep Deepgram wording precise: Flux is STT/turn-taking, Speak/Aura is TTS.

## Prompt For Main Chat

Use this in the local workspace chat:

```text
We are in /Users/aeroknight/Documents/Fusion Self Benchmarking.

Please read MORTIC_PLUGIN_HANDOFF.md first.

Goal: mix the current Deepgram + Mercury OpenCode voice bridge in opencode_voice/ with the native OpenCode TUI sidepod reference in opencode_mercury_sidepod/.

Build Mortic as an OpenCode-native plugin/sidebar experience. The sidepod should become the primary control/status UI, while the existing Python bridge continues to handle Deepgram Flux STT, Deepgram Speak/Aura TTS, Mercury/OpenCode fork turns, barge-in, compaction, and speech filtering.

Start with a small milestone:
- keep opencode_voice runnable as-is;
- add plugin-side bridge discovery/launch or a very thin connection layer;
- show bridge health/session/fork/turn status in the sidepod;
- wire sidepod commands to existing bridge control messages;
- preserve the browser mic page as a fallback capture surface for now;
- do not rewrite the whole bridge.

Be careful with my existing uncommitted local changes. Do not revert unrelated files.
```
