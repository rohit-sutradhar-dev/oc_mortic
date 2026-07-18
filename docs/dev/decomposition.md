# Decomposition program

The standing plan for breaking the voice helper into modules that can be
understood and tested independently. Direction is settled; entries here are steps,
not proposals.

## Why

`server.py` accumulated the whole voice lane: audio device sessions, STT
transport, TTS streaming, turn orchestration, interruption handling, sidepod
protocol, and lane registry. Feature work in a file that size is possible but not
reviewable — correctness gets checked at the surface because the internals cannot
be held in the head at once.

The goal is not smaller files for their own sake. It is that each piece has one
purpose, an explicit interface, and its own tests.

## Current state

Measured 2026-07-18.

| Module | Lines | Notes |
|---|---|---|
| `server.py` | 4,872 | The target. See breakdown below. |
| `response_eval.py` | 1,578 | Eval harness. Second-largest; not yet examined. |
| `tts_providers.py` | 1,349 | Provider-neutral TTS. Deepgram + Cartesia. |
| `device_audio.py` | 728 | Device-clocked duplex stream, jitter buffer, playout. |
| `response_contract.py` | 711 | Structured envelope schema + graders. |
| `interruption.py` | 654 | Pure episode decisions. Good boundary already. |
| `flux_transport.py` | 430 | Extracted Flux transport. |
| `speech_filter.py` | 185 | `FlushLimiter` + `SpeechTextFilter` (currently unwired). |

### Inside `server.py`

| Class | Lines | Responsibility |
|---|---|---|
| `VoiceConnection` | ~1074–3756 (**~2,700**) | Everything about a voice turn. The core problem. |
| `SidepodConnection` | 3812–end | Sidepod protocol layer, subclasses `VoiceConnection`. |
| `NativeSpeakerSession` | 446–1074 | Playback, queue backpressure, pause/resume. |
| `NativeMicSession` | 315–446 | Mic capture. |
| `ActiveSidepodLaneRegistry` | 3783–3812 | Lane bookkeeping. |
| Small dataclasses | 121–152 | `CompactionOutcome`, `WorkFeedbackState`, and one error type. |

`VoiceConnection` at ~2,700 lines in a single class is the thing this program
exists to address. `SidepodConnection` inheriting from it means the protocol layer
and the turn engine are coupled through the base class.

## Done

Provider-neutral seams, extracted from `server.py`:

- `stt_provider.py` — STT protocol + `SpeechEvent`
- `deepgram_stt_provider.py` — `DeepgramSTTProvider` implementation
- `tts_providers.py` — TTS protocol, Deepgram + Cartesia implementations
- `tts_chunker.py` — `TTSChunker`
- `speech_filter.py` — `SpeechTextFilter`, `FlushLimiter`
- `agent_backend.py` — `AgentBackend` protocol covering what `server.py` uses
- `callbacks.py` — consolidated callback types
- `flux_transport.py` — Flux transport

Commits `988ae12`..`5f9caa2`.

## Next

Not yet sequenced — decide order before starting.

1. **Break up `VoiceConnection`.** The largest single win. Candidate seams,
   drawn from what the class already does: turn lifecycle/state machine, TTS
   output streaming, interruption/barge-in coordination, context overflow and
   compaction, message tracking. Each wants characterization tests before
   extraction.
2. **Decouple `SidepodConnection` from `VoiceConnection`.** Inheritance ties the
   protocol surface to the turn engine. Composition would let the protocol layer
   be tested without a full voice lane.
3. **Examine `response_eval.py` (1,578 lines).** Not yet looked at. Assess before
   planning.

## Rules

- Characterize first. A refactor without tests capturing intended behavior is a
  rewrite with extra steps.
- One seam per change. Extractions that move several concerns at once cannot be
  reviewed.
- Extract behind a protocol where a second implementation is plausible.
- Record non-obvious boundary choices in `decisions.md` — where a seam was drawn
  is exactly the thing that is hard to reconstruct later.
