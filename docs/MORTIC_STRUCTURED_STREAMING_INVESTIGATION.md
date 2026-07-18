# Mortic Structured Streaming Investigation

Status: deferred architecture evidence; no production implementation planned

Date: 2026-07-13

Related work: MOR-172, Engine track

## Decision

Keep Mortic's current authoritative final-response path for Mercury 2. Native
structured streaming through an unmodified OpenCode server is technically
possible, including tool calls, but the measured safe latency headroom was only
0–180 ms. Mercury reasoning and tool execution remained the dominant delay.

Do not patch OpenCode or ship the provider-adapter workaround for Mercury 2.
Retain the evidence and design because an autoregressive provider may expose a
much larger interval between its first complete response segment and its final
object.

This decision does not change the production response contract, prompt, TTS,
sidepod protocol, provider selection, or reasoning effort.

## Question Investigated

Mortic currently asks OpenCode 1.17.18 for a strict final object containing
`displayText` and `spokenText`. OpenCode implements that request through a
required `StructuredOutput` tool. The product waits for the completed and
validated tool input before showing or speaking the answer.

The investigation asked whether Mortic could instead:

1. keep OpenCode's ordinary streamed-text path;
2. ask the provider for native JSON Schema output and streaming;
3. preserve OpenCode tools and multi-step execution;
4. receive the native structured content as ordinary OpenCode text deltas; and
5. eventually admit safe display/spoken segments before the whole response is
   complete.

## Confirmed Provider Capabilities

The current Inception OpenAPI schema exposes `tools`, `stream`, `realtime`,
`response_format`, and `reasoning_effort` on the same chat-completion request.
The `realtime` flag is described as reducing time to first token or first audio
token and is separate from reasoning effort. The documented effort values are
`instant`, `low`, `medium`, and `high`.

Primary references:

- [Inception OpenAPI specification](https://api.inceptionlabs.ai/openapi.json)
- [Structured outputs](https://docs.inceptionlabs.ai/capabilities/structured-outputs)
- [Streaming and diffusion](https://docs.inceptionlabs.ai/capabilities/streaming)
- [Tool use](https://docs.inceptionlabs.ai/capabilities/tool-use)
- [Prompt guide](https://docs.inceptionlabs.ai/resources/prompt-guide)

A live direct-provider probe confirmed the combination rather than relying only
on schema compatibility. The request used Mercury 2 with `stream=true`,
`realtime=true`, `reasoning_effort=low`, a strict JSON Schema response format,
and a required function tool.

| Phase | Result | First SSE | Stream evidence |
| --- | --- | ---: | --- |
| Required tool selection | HTTP 200; correct tool and valid JSON arguments | 924 ms | 2 chunks |
| Final response after tool result | HTTP 200; exact required schema keys | 597 ms | 3 chunks, including 2 content chunks |

This proves that native schema output, streaming, and tools can coexist across a
complete tool iteration. A JSON Schema guarantees the response shape; it does
not by itself guarantee semantic completeness, safety inside string values, or
display/spoken factual equivalence.

## OpenCode 1.17.18 Behavior

OpenCode's public structured prompt format does not pass through as ordinary
provider content. It creates a required `StructuredOutput` tool and asks the
model to call it exactly once after all other work.

OpenCode receives provider `tool-input-delta` events, initializes the pending
tool state with `raw: ""`, and then discards the delta content. It exposes the
fully parsed input only when the completed tool call arrives. The behavior is
visible in the OpenCode 1.17.18
[`SessionProcessor`](https://github.com/anomalyco/opencode/blob/v1.17.18/packages/opencode/src/session/processor.ts#L315-L331).

A normal OpenCode plugin event hook cannot recover information that the
processor has already discarded. A plugin-defined output tool has the same
limitation because its executor receives arguments only after parsing finishes.

OpenCode's ordinary text path is different: provider text chunks are published
as `message.part.delta` events. That provides a way to test native structured
streaming without changing OpenCode core.

## Stock-OpenCode Escape Hatch

The successful isolated prototype used a stock OpenCode 1.17.18 binary and a
voice-only provider wrapper:

```text
Mortic voice fork
  -> OpenCode prompt in ordinary text mode (no OpenCode structured format)
  -> Mortic-only provider wrapper
       injects stream=true
       injects realtime=true
       preserves reasoning_effort=high
       injects native JSON Schema response_format
  -> Mercury tool calls and final schema-constrained content
  -> OpenCode ordinary message.part.delta events
  -> Mortic incremental JSON parser and final validators
```

For the bounded probe, an isolated file plugin used OpenCode's configuration
hook to add a custom provider `fetch` function. The function rewrote only the
body sent by a unique `mortic-probe` provider. The prompt did not include an
OpenCode `format`, so OpenCode treated the provider's streamed JSON as ordinary
assistant text.

That experiment proves feasibility, but mutating `provider.options.fetch` from
a configuration hook is an internal seam rather than the preferred production
contract. A future implementation should ship a Mortic-owned AI SDK provider
adapter with an explicit request-body transform or fetch wrapper, pin its
supported OpenCode range, and select it only for ephemeral Mortic voice forks.
It must not alter the user's ordinary provider or source thread.

## Stock-OpenCode Probe Results

The isolated server used:

- OpenCode 1.17.18;
- a unique temporary provider and `voice-probe` agent;
- `reasoning_effort=high` with `realtime=true`;
- native strict `displayText` / `spokenText` JSON Schema output;
- ordinary OpenCode text streaming; and
- a temporary workspace that was removed after the test.

### Real tool turn

The agent read a temporary file through OpenCode's real `read` tool. The event
sequence included `pending`, `running`, and `completed`. Mercury then produced a
schema-valid object containing the correct tool-derived 48 kHz fact, and the
concatenated OpenCode text deltas exactly matched the persisted final text.

| Metric | Observation |
| --- | ---: |
| First ordinary text delta | 3,380 ms |
| Session idle | 3,381 ms |
| Streaming headroom | 1 ms |
| Text delta count | 3 |

### Forced long response

The agent was given a fixed eight-sentence passage and asked to reproduce it
unchanged in both fields. It returned 552 exact characters per field.

| Metric | Observation |
| --- | ---: |
| First ordinary text delta | 2,426 ms |
| Session idle | 2,606 ms |
| Streaming headroom | 180 ms |
| Text delta count | 7 |

Most early chunks arrived in the same event-loop interval. The final one-byte
chunk arrived immediately before idle. Waiting for a complete and validated
`spokenText` therefore recovered almost none of the apparent 180 ms.

### Semantic stress observation

One deliberately constrained request asked for exactly eight sentences and
roughly 900 characters in both fields. Mercury took about 7.8 seconds and
returned schema-valid fields of only three characters each. This was a semantic
failure despite perfect structural validity. It reinforces that native schema
enforcement cannot replace Mortic's prompt calibration, deterministic safety
checks, semantic evaluation, or transactional repair policy.

## Why This Is Deferred for Mercury 2

The prototype changed where structured bytes appeared but did not materially
change when a safe utterance became available:

- the real tool turn exposed only 1 ms of headroom;
- the long response exposed 180 ms before idle, with a complete spoken field
  effectively arriving at the end;
- short voice responses normally contain only one or two sentences, reducing
  the opportunity further;
- tool and reasoning time occurs before the final structured stream and cannot
  be recovered by parsing it earlier; and
- early speech would add incremental JSON parsing, cancellation, repair, and
  semantic-commit complexity to a path that is currently authoritative and
  generation-safe.

Mercury uses diffusion generation. Ordinary streaming is append-only at the API
surface, while `diffusing=true` exposes revisable intermediate drafts. Mortic
must never speak or treat `diffusing=true` states as committed output.

The current final-object design remains the correct trade-off for Mercury 2.
Prompt quality, tool restraint, realtime serving evaluation, context handling,
and model/tool latency have more leverage than structured-chunk admission.

## Revisit Case: Autoregressive Providers

An autoregressive model may emit stable tokens progressively over a much longer
interval. In that environment, a complete first sentence or response segment
could precede the final object by enough time to justify early TTS.

Revisit this design only when a candidate provider satisfies all of the
following:

- native JSON Schema output, streaming, and tool calls coexist;
- streamed content is append-only and never revises committed bytes;
- high-quality reasoning and multi-step tool behavior are preserved;
- the provider adapter can be isolated to Mortic's ephemeral fork;
- a complete locally safe display/spoken pair arrives at least 500 ms before
  final validation at p95 on representative turns; and
- the gain persists on direct, long, and tool-backed turns rather than only a
  synthetic long-output case.

### Candidate future response shape

The current two-field object completes `spokenText` near the end. A future
streaming contract should use independently closable pairs:

```json
{
  "segments": [
    {
      "sequence": 0,
      "displayText": "The device clock is 48 kHz.",
      "spokenText": "The device clock is forty-eight kilohertz."
    }
  ]
}
```

Mortic could parse each fully closed segment, apply deterministic checks to
both fields, verify pair-level factual equivalence, and commit the display and
speech atomically. Incomplete JSON, open strings, and unclosed segments must
remain invisible and silent.

This narrows but does not eliminate semantic risk. Once spoken, an earlier
segment cannot be repaired or withdrawn. The prompt must require each segment
to be independently final, and the evaluation corpus must prove that later
segments do not contradict, qualify, or correct committed speech.

### Required implementation boundaries

If the revisit gates pass:

1. Package a Mortic-owned AI SDK provider adapter; do not rely on runtime
   monkey-patching of a user's provider.
2. Select the adapter only in the managed server overlay and ephemeral voice
   agent.
3. Keep OpenCode in ordinary text mode while injecting the provider's native
   response schema.
4. Preserve tools across all model iterations and require the schema only for
   final content.
5. Parse JSON incrementally with strict byte, depth, field-length, and timeout
   limits.
6. Commit only complete segment pairs after deterministic safety and
   equivalence checks.
7. Fence every segment and TTS chunk with the existing turn and playback
   generation token.
8. On interruption, retry, malformed JSON, repair, or provider failure, discard
   every uncommitted byte and prevent late segments from reaching the device.
9. Validate the complete final object even after segment commits and record any
   mismatch as a hard canary failure.
10. Keep the current final-object implementation as the rollback path until two
    clean provider cohorts pass.

### Required telemetry and tests

Record monotonic timestamps for provider request, first SSE byte, first JSON
byte, first closed segment, first admitted pair, final object, final validation,
first TTS audio, interruption, and drain. Do not log raw response content by
default.

Tests must cover split UTF-8, escape sequences, braces inside strings, arbitrary
chunk boundaries, duplicate chunks, reordered or repeated segment numbers,
schema failure, semantic correction after commit, tool retries, cancellation,
late provider bytes, TTS failure, and event-to-poll fallback.

Release remains blocked on any unsafe literal, screen/speech factual mismatch,
duplicate segment, post-cancellation audio, or committed statement later
corrected by the same response.

## Reproduction Hygiene

The direct and stock-OpenCode probes used local credentials without printing or
persisting them. All temporary OpenCode sessions, provider code, server
processes, and workspace files were removed. No production configuration,
source OpenCode thread, or repository file other than this documentation was
changed by the investigation.
