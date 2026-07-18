# Chores

Small items with an obvious fix that simply is not done. No design decision
required — if one is, it is a proposal.

Delete entries when done.

---

## `.DS_Store` is tracked

Committed to the repo and present in `upstream/main`, so it arrives with every
doc refresh. Should be untracked here and added to `.gitignore`.

```bash
git rm --cached .DS_Store
echo '.DS_Store' >> .gitignore
```

Raised 2026-07-18.

---

## Flaky tests under full-suite runs

Known issue. Observed 2026-07-18: `tests/test_sidepod_lane.py::
InterruptionControllerIntegrationTests::test_echo_episode_ducks_once_then_owns_restart_and_turn_resumed`
failed in a full `pytest tests/` run, then passed 5/5 in isolation with no code
change between runs. A second failure in the same run was not captured.

Suggests load- or ordering-sensitivity rather than a bad assertion — plausible
given the interruption and echo tests exercise real timing.

This is worth more than its size: a flaky suite undermines "characterize before
refactoring", because a red test during an extraction cannot be trusted to mean
the extraction broke something. Worth identifying the timing dependencies before
the `VoiceConnection` split, which is exactly the work that will lean hardest on
these tests.

Not yet diagnosed. If it turns out to need a design decision (e.g. injecting a
clock), promote to `proposals.md`.

---

## Inherited failing test: readiness vs. Cartesia default

`tests/test_opencode_voice.py::HelperReadinessTests::test_readiness_has_no_issues_when_runtime_checks_pass`

Fails on this repo **and identically on pristine `upstream/main`** — verified by
running the suite in a clean worktree at `upstream/main`. Not caused by the merge.

Upstream `168bd17` changed the default TTS provider to Cartesia
(`config.py:68` — `required_credentials(tts_provider: str = "cartesia")`) without
updating this test, which seeds only `DEEPGRAM_API_KEY` and `INCEPTION_API_KEY`.
`required_credentials` therefore reports `missing_cartesia_api_key`.

Fix here: add `CARTESIA_API_KEY` to the patched environment. Worth reporting to
Aditya rather than only fixing locally — it is his bug and it fails in his tree.

Cross-referenced in `upstream-drift.md` (Testing), which records that the failure
is inherited so it is not mistaken for a regression.

Raised 2026-07-18.
