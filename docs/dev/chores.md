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

## Tell Aditya about the Cartesia readiness test

Fixed here 2026-07-18, but it still fails on `upstream/main` — it is his bug and
his suite is red because of it. See `upstream-drift.md` (Testing) for the detail
to send him.

Raised 2026-07-18.
