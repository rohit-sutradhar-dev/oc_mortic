# Decisions

Append-only record of non-obvious choices and why they were made. Never delete an
entry — supersede it with a later one that references it.

Add an entry when the reasoning would be hard to reconstruct from the diff alone:
where a module boundary was drawn, why an upstream approach was not taken, why an
obvious-looking option was rejected.

---

## 2026-07-18 — Stop merging upstream; read it instead

Merging `upstream/main` produced six conflicting files, of which `server.py` was
a genuine semantic conflict: upstream rewrote ~1,300 lines around structured
turns while this repo was extracting providers out of the same file. Two of the
conflicts hid problems that conflict markers did not show — a method calling
helpers that do not exist here, and an upstream bugfix that a naive resolution
would have silently dropped.

Both tracks are healthy; they are just moving in different directions. Repeated
merges would keep producing conflicts whose resolution requires re-deriving both
designs, and the structural work here is exactly what gets eroded.

**Decision:** fetch and read upstream, reimplement what is worth having, never
merge. Drift recorded in `upstream-drift.md`.

---

## 2026-07-18 — Keep `messages()` naming over upstream's `messages_for_tracking()`

Upstream's `messages_for_tracking()` and this repo's `messages()` are the same
method under different names: legacy shape first, falling back to a merge of the
v2 projection and the recent-messages tail on HTTP 400.

Upstream's copy could not be taken as-is — it calls `projected_messages` and
`recent_messages`, which are `_projected_messages` and `_recent_messages` here
(made private during the `AgentBackend` cleanup). Taking it verbatim would have
raised `AttributeError` at runtime.

But this repo's copy could not simply be kept either: upstream had added a
400-tolerant guard around the recent-messages call, for the case where the tail
holds only a structured user message whose persisted `format.retryCount` the
legacy decoder cannot read. That is a real fix, and keeping our side wholesale
would have dropped it.

**Decision:** keep this repo's naming (`messages()` public, helpers private),
graft upstream's guard into it. Upstream's new test for the guard was kept and
retargeted at `messages()`.

Callers: `response_eval.py` had two sites upstream upgraded to the tracking view —
those now call `messages()`. Two other sites upstream left on the raw path stay on
`_messages()`.

---

## 2026-07-18 — Leave `SpeechTextFilter` unwired rather than re-hooking it

Upstream's structured-turn rewrite removed the delta-streaming pipeline that
`SpeechTextFilter` was attached to. The merge left the class with zero call sites
and dropped its tests.

Re-hooking it at the surviving TTS site would not work: `evaluate_response` runs
*before* that point, so a filter applied there could never prevent the rejection
it was designed to prevent. Testing it against the contract's actual safety codes
showed it fixes none of them correctly — see the rejected-alternative section in
`proposals.md`.

**Decision:** do not re-wire. Keep the class and restore its tests, because it is
the starting point for the spoken-text normalizer proposal. Removed the now-unused
import from `server.py`.

**Open question deliberately not settled here:** whether upstream's strict
rejection (silence + error on unfixable violation) is the right product behavior.
Tracked in `proposals.md`.
