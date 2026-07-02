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

## Run The Voice Bridge

```bash
source .venv/bin/activate
opencode-voice --managed-opencode --open
```

`--managed-opencode` starts a clean `opencode serve` process with a runtime config overlay for the Inception provider and Mercury model. If a running OpenCode server is detected, managed mode borrows that server's project directory so the clean server can still see the same threads.

Useful options:

```bash
opencode-voice --help
opencode-voice --managed-opencode --opencode-dir "/path/to/project" --open
opencode-voice --context-threshold 70000 --tts-model aura-2-phoebe-en --open
opencode-voice --model-variant low --open
opencode-voice --eager-eot-threshold 0.5 --open
```

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
