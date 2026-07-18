# Proposals

Ideas that cannot start until a design decision is made. If the action is obvious,
it belongs in `chores.md`. If it is a step toward the target architecture, it
belongs in `decomposition.md`.

Graduate a proposal to `docs/dev/specs/` once it is designed, and record the
resulting choice in `decisions.md`.

---

## Spoken-text normalizer

**Status:** open · raised 2026-07-18

**Problem.** The structured-turn contract gates `spokenText` at runtime
(`server.py` — `evaluate_response` → `safety_violations`). A violating turn gets
one repair attempt; if that also fails, `fail_structured_turn` aborts and the
agent says *nothing* while the user sees `structured_response_unavailable`. The
answer is discarded. Strict rejection is a deliberate upstream choice, but it
trades degraded speech for silence.

Some violations are mechanically fixable and do not need a model round-trip:

| Safety code | Mechanically fixable? |
|---|---|
| `speech_hostile_abbreviation` | **Yes.** `e.g.` → "for example", `i.e.` → "that is", `vs.` → "versus". |
| `spoken_bracket_notation` | No. `items[0]` → "the first item" needs to know what `items` is. |
| `spoken_path_spelling` | No. `release/status.md` → "the release status file" needs semantics. |

**Proposal.** A small normalizer applied *before* `evaluate_response`, handling
only the mechanically-solvable codes. Leave the rest to the repair loop.

**Rejected alternative.** Reusing `SpeechTextFilter` for this does not work, and
was tested rather than assumed:

- It does not strip `()[]{}<>` at all, so it cannot fix `spoken_bracket_notation`.
- `_INLINE_CODE_RE` only matches backticked text, so bare `server.py` sails
  through `spoken_path_spelling`.
- On `speech_hostile_abbreviation` it appears to pass, but only because its
  whitespace collapse turns `e.g. for` into `e.g.for`, defeating the regex's
  trailing `(?!\w)`. That is a false pass that corrupts the text — worse than the
  rejection it bypasses.

Root cause: `SpeechTextFilter` strips *formatting* from markdown prose. The
contract demands *rephrasing* into natural speech. Different problems.

**Also rejected:** running the normalizer and the repair reprompt concurrently to
save latency. The normalizer is synchronous regex work — microseconds — while the
reprompt is a network call. Parallelism only pays when both branches are
expensive; here it would just burn tokens.

**Open questions before this can start.**

1. Pre-`evaluate_response`, or between the first evaluation and the repair call?
   Pre-evaluation is simpler but normalizes text that would have passed anyway.
2. Does normalizing `spokenText` require the same treatment for `displayText`, or
   should they be allowed to diverge?
3. Is silent-failure-on-unfixable-violation acceptable, or is that a separate
   product question about the strictness of the gate itself?

Question 3 may be the real one — the normalizer only narrows the failure window,
it does not close it.
