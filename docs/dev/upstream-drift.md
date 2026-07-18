# Upstream drift

Where this repo diverges from `mortiphi/oc_mortic` and why. Organized by area
rather than chronologically, because it is read while looking at a specific
upstream change.

`docs/upstream/` shows *what* drifted mechanically. This file carries the *why*.

Baseline: merged `upstream/main` at `168bd17` on 2026-07-18 (commit `31ad78f`).
That was the last merge — see `decisions.md`.

---

## Process

**Upstream is read, never merged.** Reimplement what is worth having; record it
here. See `decisions.md` (2026-07-18).

**`AGENTS.md` is fully replaced.** Upstream's is built around a shared Jira board
(`MOR` on `mortic.atlassian.net`), three ownership tracks, and a delivery date.
None of that applies to independent work in this repo. Upstream's copy is at
`docs/upstream/` only in the sense that its guidance is superseded; the product
rules it contained are carried forward in the new `AGENTS.md`.

**`MORTIC_PROJECT_EXECUTION_PLAN.md` and `MORTIC_OWNERSHIP_BOUNDARIES.md` are not
used here.** Kept as inherited docs, but they describe two-people-splitting-one-repo
coordination. Work in this repo comes from `decomposition.md`, `proposals.md`, and
`chores.md`.

---

## Module layout

**`TTSChunker` and `FlushLimiter` live outside `deepgram.py`.**
Upstream keeps both in `deepgram.py`. Here, `TTSChunker` is in `tts_chunker.py`
and `FlushLimiter` in `speech_filter.py`, leaving `deepgram.py` as pure functions
(`build_flux_url`, `build_tts_url`, `parse_flux_message`).

Implementations were byte-identical at the time of the split — this is a pure
structural difference, so upstream changes to either class port over directly.

**Provider-neutral seams do not exist upstream.**
`stt_provider.py`, `deepgram_stt_provider.py`, `agent_backend.py`,
`callbacks.py`, `flux_transport.py`, `tts_chunker.py` are all extractions from
`server.py` made here. Upstream's `DeepgramFluxSession` is this repo's
`DeepgramSTTProvider`; the class does not exist here under upstream's name.

Expect upstream `server.py` changes to need re-siting rather than copying.

---

## OpenCode client

**`messages()` vs `messages_for_tracking()`.**
Same method, different names. Upstream: `messages()` is the raw call and
`messages_for_tracking()` is the fallback-merging one. Here: `messages()` is the
fallback-merging one and `_messages()` is raw, with `_projected_messages` and
`_recent_messages` private.

When reading upstream code, `client.messages(...)` means `_messages(...)` here.
Getting this backwards silently changes behavior — both names exist in both trees.

Upstream's 400-tolerant guard around the recent-messages tail **has** been ported.
See `decisions.md`.

---

## Voice pipeline

**`SpeechTextFilter` is retained but unwired.**
Upstream removed the delta-streaming pipeline it hooked into and does not have
the class. Here it is kept with tests, unused, as the basis for the spoken-text
normalizer in `proposals.md`.

**Turn execution follows upstream.** The structured-turn flow, the runtime
`safety_violations` gate, and the repair loop were taken from upstream's rewrite
rather than reimplemented. Cartesia as default TTS provider likewise.

---

## Testing

**Inherited failing test.**
`test_readiness_has_no_issues_when_runtime_checks_pass` fails here *and* on
pristine `upstream/main` — confirmed by running the suite in a clean worktree at
`upstream/main`. Upstream `168bd17` defaulted TTS to Cartesia without updating the
test's patched environment.

Recorded here so it is not mistaken for a regression introduced by this repo's
work. The fix is tracked in `chores.md`.

**Restored tests upstream dropped.** Three `SpeechTextFilter` tests
(`test_speech_filter_removes_fenced_code`,
`test_speech_filter_removes_markdown_code_details`,
`test_speech_filter_releases_safe_partial_sentences`) were lost when upstream's
test-file rewrite won the merge. Restored so the retained class stays covered.
