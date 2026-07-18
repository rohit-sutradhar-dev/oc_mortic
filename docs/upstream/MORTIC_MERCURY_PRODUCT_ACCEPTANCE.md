# Mortic Mercury Product Acceptance

Run the full voice checks with Deepgram and the focused regression checks with Cartesia. Use laptop speakers, the ordinary microphone, and normal conversational volume. Structured output is the only response path; there is no runtime response-mode flag or legacy fallback. Record the run directory for every failure.

For each response, mark correctness, completeness, screen/speech equivalence, naturalness from 1–5, unexplained silence, truncation or duplication, and whether the first interruption attempt worked.

## Startup and controls

- Cold-start `/mortic`. The UI must say connecting or starting until capture is genuinely live.
- Press `M` during startup and press it again to cancel. The microphone must not start later from a stale acknowledgement.
- Press `M` while Mortic speaks. Capture must mute immediately while playback continues without a flush or restart. Unmute and confirm listening resumes.
- Press `X` during a long answer. Old audio must stop within 200 ms and never return.

## Response prompts

1. “What year comes after 2025, and what is 25% of 80?”
   - Screen: `2026`, `25%`, and `20`.
   - Speech: natural equivalents; no digit-by-digit year or punctuation narration.
2. “Which file defines InterruptionController, and what does it own?”
   - Screen: `interruption.py` or the shortest useful relative reference.
   - Speech: a natural role such as “the interruption controller module,” never a spoken path or extension.
3. “Can you fix it?” with no antecedent.
   - One direct clarification question, no workspace inspection, and no tool cue.
4. “Inspect the voice config and tell me the TTS, device, and speech-recognition sample rates.”
   - One tool-start earcon and transient “I’m reviewing the relevant files.” feedback.
   - Correct facts: 16 kHz TTS, 48 kHz device/AEC, and 16 kHz recognition.
5. “Correction: I only care about the device and echo-canceller clock. What is it?”
   - Answer: 48 kHz, without resurrecting the superseded details.
6. “Inspect the current compaction and OpenCode streaming paths and tell me the two highest remaining risks.”
   - One onset earcon if a real tool starts before four seconds. At four seconds, hear one matching spoken activity phrase, or a quiet local holding cue if no tool was observed. No raw tool/model prose appears before the atomic final.
7. “Explain how one spoken turn travels from microphone to screen and speaker in exactly four concise sentences.”
   - Four complete sentences, shown and spoken once with no missing or repeated suffix.
8. Ask Mortic to read an absolute path, command, and JSON object aloud.
   - It must summarize them naturally instead of reproducing unsafe literals.
9. Ask for notation containing `[P1]`, `items[0]`, `Map<string, T>`, `refresh(options)`, and `(temporary)`.
   - The display may retain useful notation. Speech must say natural equivalents and contain no literal parentheses, square brackets, braces, or angle brackets, including unmatched punctuation such as `options)`.

## Long-session and full-duplex checks

- Open a known existing `simple_mortic` task above 70k active tokens. Ask for one early decision, one correction, and one recent unresolved action. Verify all three and confirm the source task is unchanged.
- Start a turn while compaction is active. If blocking is necessary, COMMS shows “Preparing context,” then “Continuing.” Several small follow-ups must not trigger another compaction.
- Run ten ordinary speaker-on turns. There must be no self-interruption or phantom transcript.
- Say “stop” during three long replies from normal conversational distance. All three must stop on the first attempt, within 200 ms p95, without stale audio.
- Ask a genuine replacement question immediately after a suppressed echo episode. It must be admitted without re-arming the suppressed episode.
- Disconnect networking for five seconds while listening, then restore it. Trouble must become visible, the next turn must recover, and no stale transcript or audio may replay.

## Release decision

The milestone fails on any unsafe spoken literal, duplicate or missing ending, source mutation, same-episode re-arm, stale-generation audio, screen/speech factual mismatch, failed explicit stop, or unexplained silent wait over four seconds.

It passes only when both providers pass all hard checks, at least 90% of scored turns receive naturalness 4/5 or better with none below 3/5, no-tool structured responses finish within 5 seconds p95, and validated-final-to-device audio remains within 750 ms p95.
