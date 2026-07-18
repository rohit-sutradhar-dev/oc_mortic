# Mortic — Agent Operating Guide

This repo (`rohit-sutradhar-dev/oc_mortic`) is one of two parallel
implementations of the Mortic OpenCode sidepod. The other is `mortiphi/oc_mortic`,
worked on independently by Aditya.

Both of us design the product and write the code. We discuss ideas constantly and
then each implement them ourselves, so that two independent attempts exist rather
than one merged compromise. Aditya's approach is feature-first and fast. This
repo's approach is structure-first: the product should be as good, and the code
should also be understandable, reviewable, and safe to change.

Neither repo is the source of truth. Aditya's base is substantial and well-built;
we inherit it, respect it, and build on it.

## Relationship to upstream

**Read upstream. Never merge upstream.**

`upstream` is fetched and read as reference. Its changes are not merged — merging
two independent tracks produced conflicts that cost more than reimplementation
and eroded the structural work this repo exists to do.

When upstream does something worth having, reimplement it here deliberately and
record the choice. When we deliberately do something differently, record that too.

## Documentation layout

| Path | What it is |
|---|---|
| `docs/*.md` | Primary product docs. Inherited from upstream, ours to edit. |
| `docs/upstream/*.md` | Verbatim upstream mirror. Never edit. Refresh deliberately. |
| `docs/dev/` | This repo's working docs (below). |
| `docs/dev/specs/` | Specs for work being designed or built. |

Edit `docs/*.md` freely — they are ours now. `docs/upstream/` is what makes the
editing safe, because drift stays visible.

## Working docs — where things get recorded

Record as you go, not at the end. Each of these has a distinct lifecycle; the
sorting question is *what kind of thinking does this need?*

- **`docs/dev/decomposition.md`** — the standing refactoring program. Target
  module boundaries, what has been extracted, what is next. Steps here are moves
  in a plan whose direction is already settled.
- **`docs/dev/proposals.md`** — ideas that cannot start until a design decision
  is made. Graduate to `docs/dev/specs/` when designed.
- **`docs/dev/chores.md`** — small items with an obvious fix that simply is not
  done. No design thinking required.
- **`docs/dev/decisions.md`** — append-only. Why a non-obvious choice was made.
  Never delete entries; supersede them.
- **`docs/dev/upstream-drift.md`** — where this repo diverges from upstream and
  why. Organized by area, because it is read while looking at an upstream change.

If an item needs a decision before work starts, it is a proposal. If the action is
obvious, it is a chore. If it is a step toward the target architecture, it belongs
in the decomposition plan.

## How to work here

**Understand before changing.** The reason this repo exists is that code should be
reviewable by understanding it, not only by checking that its surface behaves. A
change you cannot explain is not done.

**Characterize before refactoring.** Before restructuring code, make sure tests
capture the behavior you intend to preserve. If they do not exist, write them
first — they are the thing that makes the refactor safe.

**Extract toward single-purpose modules.** Prefer small files with one clear
responsibility and an explicit interface. A module should be describable as: what
it does, how it is used, what it depends on. Large files are a signal, not a style
preference — see `docs/dev/decomposition.md` for current targets.

**Protocols over concrete types.** Provider-neutral seams (`stt_provider.py`,
`agent_backend.py`, `callbacks.py`) exist so implementations can be swapped and
tested. Depend on the protocol, not the implementation.

**Scope changes.** One concern per change. Unrelated cleanup found along the way
goes in `chores.md` rather than into the current diff.

## Product rules

These are properties of the product and hold regardless of implementation:

- Code, diffs, commands, paths, and JSON must never be spoken aloud.
- Never log, print, commit, or display API keys or raw secrets.
- Keep source OpenCode threads untouched; voice work belongs in ephemeral forks.
- Do not expose provider, model, or runtime names in normal UI.
- Do not ship visible browser UI in the main path.
- Do not overwrite unrelated user changes. If the worktree is dirty, inspect and
  work around them.

## Commits

**Agents do not commit to `main`. Ever.** `main` commits are made by the repo
owner, by hand.

When work is ready to commit, say so and stop: what changed, what was verified,
and a suggested commit message. Do not run `git commit` on `main`, and do not
push.

Agents may commit freely **inside a git worktree on a scratch branch** — that is
what worktrees are for. Temporary commits there are working state, not history;
the owner decides what becomes a real commit.

```bash
git worktree add ../oc_mortic-<topic> -b <topic>   # isolated checkout + branch
# ... work and commit freely in that directory ...
git worktree remove ../oc_mortic-<topic>           # when done
```

`main` in the primary working directory stays untouched throughout.

## Verification

Default suite:

```bash
uv run pytest
```

Use the smallest verification that proves the change, and state what was run.

`tests/test_opencode_voice.py::HelperReadinessTests::test_readiness_has_no_issues_when_runtime_checks_pass`
currently fails — inherited from upstream, not caused by work here. See
`docs/dev/chores.md`. Do not treat it as a regression; do not let it mask one.

**Some tests are flaky under full-suite runs** — timing-sensitive ones around
interruption and echo. Before concluding a change broke something, re-run the
failing test in isolation. Before concluding it did not, re-run the full suite.
Never dismiss a failure as flaky without checking. See `docs/dev/chores.md`.

If a test cannot be run, say why and name the residual risk.

## Session shape

Start by reading this file, `git status --short`, and the working doc relevant to
what you are about to do.

Finish by stating what changed, what verification was run, what was recorded and
where, and any remaining risk. Keep it short. Never paste secrets or long logs.

Claims of completion require evidence. If tests fail, say so with the output. If a
step was skipped, say that.
