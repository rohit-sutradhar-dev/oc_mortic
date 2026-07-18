# Development docs structure — design

**Date:** 2026-07-18 · **Status:** implemented

Structure for this repo's own documentation, now that it develops independently
of `mortiphi/oc_mortic`.

## Context

Two parallel implementations of the same product, by two collaborators who design
together and implement separately. Aditya's track is feature-first; this one is
structure-first. Neither is the source of truth. His base is substantial and is
inherited rather than replaced.

Merging upstream stopped being worthwhile — see `../decisions.md`. Reading it
never stops being worthwhile. So the docs need to support: our own primary docs,
a way to see what upstream changed, and an accurate record of where and why we
diverge.

## Structure

```
docs/*.md              primary docs — inherited, ours to edit
docs/upstream/*.md     verbatim mirror, never edited, refreshed deliberately
docs/dev/              working docs
docs/dev/specs/        specs for work being designed or built
AGENTS.md              operating guide (root, replaces upstream's)
```

The mirror is what makes editing the primary docs safe: `diff docs/ docs/upstream/`
shows drift mechanically, so `upstream-drift.md` only carries reasoning and cannot
rot into a changelog.

## Working-doc taxonomy

The sorting question is **what kind of thinking does this item need?** — not
product-vs-technical, which was the first attempt and broke immediately. The
spoken-text normalizer is deeply technical but needs design; `.DS_Store` is
trivially technical and needs none.

| Doc | Admits | Lifecycle |
|---|---|---|
| `decomposition.md` | Steps toward the target architecture | Standing plan, revised |
| `proposals.md` | Needs a design decision before work starts | Graduates to a spec |
| `chores.md` | Obvious fix, simply not done | Deleted when done |
| `decisions.md` | Why a non-obvious choice was made | Append-only, superseded |
| `upstream-drift.md` | Divergence from upstream and why | Living, by area |

Three lifecycles is the reason these are separate files rather than sections of
one: chores get deleted, proposals get promoted, decisions are permanent.

`decomposition.md` is deliberately not a proposal list. Its direction is settled,
so its entries are moves in a plan — mixing them with things that still need
deciding was what made a single "open items" list feel wrong.

## Rejected alternatives

**One `open-items.md`.** Conflates items needing design with items needing five
minutes, and buries the decomposition program inside a flat list.

**Product vs. technical split.** Fails on the normalizer, which is both.

**Overwriting the inherited docs.** The original request. Rejected: the PRD
(1,098 lines), protocol spec (623), and voice architecture (478) describe code
this repo actively runs, and upstream's protocol changes were merged the same day.
Deleting them would discard the specification for a live dependency. Only the
process layer — Jira, ownership tracks, execution plan — was actually a burden,
and that lives in `AGENTS.md`.

**`engineering/` at repo root.** Names the differentiator, but implies upstream's
work is not engineering, and adds a second top-level docs location.

**Flat in `docs/` with a `DEV_` prefix.** Matches upstream's flat convention but
interleaves the two pipelines in every listing — the exact problem being solved.

## Cross-cutting items

An item can legitimately appear in two docs when the *fact* and the *action* are
different. Upstream's failing Cartesia test is recorded in `upstream-drift.md`
(inherited, not a regression — matters when reading upstream changes) and in
`chores.md` (the fix). Cross-referenced, not duplicated.

## Not covered

`docs/MORTIPHI_YC_APPLICATION_*.docx` — company material, not engineering docs.
Left at `docs/` root, unmirrored, untouched.
