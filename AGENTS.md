# Mortic Agent Operating Guide

This repo is the Mortic OpenCode sidepod project. Agents working here should use Jira as the work queue, use the repo as the implementation truth, and start the next highest-value task for the requested owner track.

## Primary Sources

- Product requirements: `docs/MORTIC_OPENCODE_SIDEPOD_PRD.md`
- Delivery plan: `docs/MORTIC_PROJECT_EXECUTION_PLAN.md`
- Jira project: `MOR` on `https://mortic.atlassian.net`
- Delivery date: July 18, 2026

If Jira and local docs disagree, call out the mismatch before implementing. Do not silently reinterpret product scope.

## Jira Connection

Use the Atlassian/Jira connector when available.

Required Jira fields:
- Project: `MOR`
- Track labels: `platform-track`, `engine-track`, `shared-track`
- Start date: `Start date`
- Due date: `duedate`
- Primary statuses: `TO DO`, `IN PROGRESS`, done-status equivalents

Useful JQL:

```jql
project = MOR ORDER BY duedate ASC, key ASC
```

```jql
project = MOR AND labels = platform-track ORDER BY duedate ASC, key ASC
```

```jql
project = MOR AND labels = engine-track ORDER BY duedate ASC, key ASC
```

```jql
project = MOR AND labels = shared-track ORDER BY duedate ASC, key ASC
```

```jql
project = MOR AND duedate <= now() ORDER BY duedate ASC, key ASC
```

If the connector is unavailable, say so and use the local PRD/execution plan to continue with best effort. Do not invent Jira state.

## Startup Routine

At the start of a work session:

1. Read this file.
2. Check `git status --short`.
3. Review the relevant PRD/execution-plan sections.
4. Query Jira for the requested track.
5. Query Jira for due or overdue work.
6. Inspect local code/tests related to the likely next task.
7. Summarize the chosen immediate priority before editing files.

If the user names a track, prioritize that track. If they do not, choose the earliest due unblocked item across all tracks and say which track you selected.

## Track Selection

Platform Track owns:
- Native OpenCode sidepod surface.
- `/mortic` focus command.
- Focus-mode typing lock and key isolation.
- Command deck, confirmations, COMMS, Transcript, Handoff, Config stub.
- Sidepod protocol client.
- OpenCode plugin packaging, sandboxing, permissions, and lifecycle integration.

Engine Track owns:
- Invisible local helper.
- Helper/runtime distribution artifact.
- OS mic capture.
- Deepgram STT/TTS.
- Mercury/Inception calls.
- OpenCode ephemeral fork turn loop.
- Event streaming and polling fallback.
- Barge-in, compaction, speech filtering, and latency instrumentation.

Shared Track owns:
- Protocol freeze.
- Cross-track demos.
- Security/privacy review.
- Beta and release readiness.
- Backlog quality and ownership review.

## Taking Stock

Before starting a ticket, determine whether it is already done.

Check:
- Jira status, comments, linked issues, and blockers.
- Local implementation files.
- Tests and fixtures.
- Recent git commits or uncommitted changes.
- Existing docs and runbooks.

If implementation exists but Jira is not updated, report that and propose the Jira status change. If Jira says done but local evidence is missing, treat it as a verification task, not a rewrite.

## Priority Algorithm

For the selected track:

1. Prefer overdue or due-soon tasks.
2. Prefer blockers for other tickets.
3. Prefer tasks needed for the next shared milestone.
4. Prefer tasks with clear acceptance criteria.
5. Avoid starting packaging/release work before core flow evidence exists.

Do not start low-risk polish while a dated blocker is open.

Immediate priority output should include:
- Selected track.
- Selected Jira key and title.
- Why it is next.
- Acceptance criteria being targeted.
- Files or modules likely affected.
- Verification plan.

Keep the summary short, then start the work unless the user explicitly asked only for planning.

## Implementation Rules

- Keep changes scoped to the selected Jira ticket.
- Preserve the existing Mortic visual language unless the PRD says otherwise.
- Do not expose provider/model/runtime names in normal UI.
- Do not add a typed fallback to the packaged sidepod.
- Do not ship visible browser UI in the main path.
- Keep source OpenCode threads untouched; voice work belongs in ephemeral forks.
- Code, diffs, commands, paths, and JSON must not be spoken aloud by Mortic.
- Never log, print, commit, or display API keys or raw secrets.
- Do not overwrite user changes. If the worktree is dirty, inspect and work around unrelated changes.

## Jira During Work

When starting implementation:
- Move or mark the Jira issue as in progress if the workflow/tooling allows it.
- If the issue is blocked, record the blocker instead of forcing implementation.

When finishing:
- Run the relevant tests.
- Update the Jira issue with evidence: files changed, tests run, known gaps.
- Move or mark the issue done only when acceptance criteria are met.

If Jira updates are not available, include the exact update text in the final response so a human can paste it.

## Verification Expectations

Use the smallest verification that proves the ticket.

Examples:
- Platform UI/state work: sidepod fixture tests, protocol reducer tests, visual snapshots if available.
- Key/focus work: tests proving `/mortic` is not sent as a prompt and `M` does not leak into OpenCode keymaps.
- Engine helper work: unit tests for protocol loop, health, startup/shutdown, and redaction.
- STT/TTS work: mocked Deepgram tests plus one manual/live smoke only when keys and environment are intentionally available.
- OpenCode streaming work: SSE parser tests, event-turn state tests, polling fallback tests.
- Speech filtering: fixture corpus tests proving code/diffs/commands/paths/JSON are screen-only.
- Performance work: timing report with first transcript, first assistant text, first TTS audio, total turn, retries, and stream source.

If a test cannot be run, state why and identify the residual risk.

## Final Response Shape

End each session with:
- Jira key worked.
- What changed.
- Verification run.
- Any remaining risk or next dependency.

Keep the final response concise. Do not paste secrets or long logs.
