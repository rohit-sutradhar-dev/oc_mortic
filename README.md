# Mercury 2 DRACO Benchmark

Local harness for comparing direct Mercury 2 calls against either OpenRouter Fusion or a local two-call Mercury synthesis strategy.

The default systems are:

- `solo_mercury_2`: direct `inception/mercury-2`
- `fusion_mercury_2_x2`: `openrouter/fusion` with the `fusion` plugin, judge/synth model `inception/mercury-2`, and two `inception/mercury-2` analysis models

## Setup

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENROUTER_API_KEY="..."
```

`PyYAML` is only needed for `.yaml` configs. The included JSON config works with the Python standard library.

## Download DRACO

The runner downloads and caches DRACO automatically when the cache file is missing. You can also cache it explicitly:

```bash
python -m scripts.download_draco
```

By default this writes `data/draco/test.jsonl`.

## Run

Smoke test the first 10 tasks in streaming mode:

```bash
python -m openrouter_bench.run --config configs/fusion_mercury_2.json --limit 10 --stream
```

Run direct Inception Labs Mercury 2 plus local Mercury x2 synthesis:

```bash
export INCEPTION_API_KEY="..."
python -m openrouter_bench.run --config configs/inception_mercury_2.json --limit 10 --stream
```

Run Inception Mercury 2 with Groq Llama 70B variants:

```bash
export INCEPTION_API_KEY="..."
export GROQ_API_KEY="..."
python -m openrouter_bench.run --config configs/groq_mercury_2.json --limit 10 --stream
```

That config includes:

- `solo_mercury_2`
- `solo_llama_3_3_70b_groq`
- `fusion_mercury_2_llama_70b_groq_synth`: Mercury and Llama 70B run in parallel, then Groq synthesizes.
- `mercury_2_draft_groq_llama_70b_check`: Mercury drafts first, then Groq checks/corrects it.

Run all 100 tasks:

```bash
python -m openrouter_bench.run --config configs/fusion_mercury_2.json --limit 0 --stream
```

Use non-streaming mode:

```bash
python -m openrouter_bench.run --config configs/fusion_mercury_2.json --limit 10 --no-stream
```

Run only one system:

```bash
python -m openrouter_bench.run --config configs/fusion_mercury_2.json --systems solo_mercury_2
```

Each run writes:

- `runs/<timestamp>/responses.jsonl`
- `runs/<timestamp>/run_config.json`

Each response row includes the task id, prompt, API name, system name, full response text, wall-clock latency, TTFT for streaming calls, token usage when available, generation/response ids when available, errors/retry count, request payload, and raw JSON response/chunks.

## Config

Edit `configs/fusion_mercury_2.json` or copy it to add baselines and fusion variants. For example, change `analysis_models` to compare Mercury 2 plus another fast model, or add another system object with a different `model`.

Any top-level `defaults` fields are copied into every request unless a system overrides them. System fields such as `plugins`, `provider`, `temperature`, `max_completion_tokens`, and `reasoning` pass through to OpenRouter.

For Inception Labs, use `configs/inception_mercury_2.json`. It reads `INCEPTION_API_KEY`, calls `https://api.inceptionlabs.ai/v1`, uses the direct model id `mercury-2`, and sets `temperature` to `0.75` because Inception currently accepts `0.5` through `1.0`.

For Groq, use `configs/groq_mercury_2.json`. It reads `GROQ_API_KEY`, calls `https://api.groq.com/openai/v1`, and uses `llama-3.3-70b-versatile` for the Llama 70B systems.

## Notes

- Streaming TTFT is measured from just before the HTTP request is opened until the first non-empty streamed `delta.content`.
- OpenRouter returns `X-Generation-Id` in response headers; the harness records it when present.
- DRACO rows use `problem` as the prompt and contain 100 `test` rows.

## OpenCode Mercury Voice Bridge

The voice bridge lets you speak to an existing OpenCode thread through a temporary fork. It uses solo Mercury 2 for the main model, OpenCode `small_model`, summaries, and proactive compaction; there is no Fusion or second model in this path.

```bash
source .venv/bin/activate
export INCEPTION_API_KEY="..."
export DEEPGRAM_API_KEY="..."
opencode-voice --managed-opencode --open
```

`--managed-opencode` starts a clean `opencode serve` process with a runtime `OPENCODE_CONFIG_CONTENT` overlay for:

- provider `inception`
- model `inception/mercury-2`
- `small_model` `inception/mercury-2`
- compaction, title, and summary model `inception/mercury-2`

If a running OpenCode server is detected, managed mode borrows that server's project directory so the clean server can still see the same threads. You can set the directory explicitly:

```bash
opencode-voice --managed-opencode --opencode-dir "/path/to/project" --open
```

If `--managed-opencode` is omitted, the launcher tries to use a running OpenCode server directly. That is useful for debugging, but a pre-existing server may have stale provider/model resolver state.

The browser UI opens at `http://127.0.0.1:8765` by default. Pick an OpenCode thread, start a fork, then use the mic or typed fallback. The fork is deleted when you end the session unless `Keep fork` is enabled.

Voice defaults:

- STT: Deepgram Flux `flux-general-en`
- TTS: Deepgram Aura `aura-2-thalia-en`
- audio: mono linear16 at 16 kHz
- compaction threshold: 70,000 context tokens

Useful options:

```bash
opencode-voice --help
opencode-voice --context-threshold 70000 --tts-model aura-2-phoebe-en --open
opencode-voice --model-variant low --open
opencode-voice --eager-eot-threshold 0.5 --open
```

Run logs are written to `runs/voice/<timestamp>/events.jsonl` and include fork lifecycle, token counts, compaction duration, first assistant text, TTS first audio, errors, and aborts.
