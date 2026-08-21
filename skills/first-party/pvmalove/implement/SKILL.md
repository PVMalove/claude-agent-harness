---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---

Implement the work described by the user in the spec or tickets.

If the invocation or immediate context names a specific tracker ticket (an issue number, URL, or a `.scratch/<feature>/issues/NN-*.md` path), check it before starting. If it carries `workflow::blocked` (see `docs/agents/triage-labels.md`), check its blockers using this repo's tracker (native GitHub/GitLab dependency links, or the `Blocked by:`/`**Blocked by:**` field — see `docs/agents/issue-tracker.md`). If every blocker is closed/resolved, replace `workflow::blocked` with `workflow::ready` (or the equivalent `**Workflow:**` line for a local ticket file), then proceed. If any blocker is still open, stop and tell the user which ones — don't start the work. Once you actually start work on the ticket, set `workflow::in-progress` (locally, `**Workflow:** workflow::in-progress`).

If the reference given is an **epic** (carries `workflow::specs`, or is otherwise the parent of a decomposition) rather than a specific ticket, pick the next ticket yourself instead of asking: run the same frontier query `/wayfinder` uses (`docs/agents/issue-tracker.md#wayfinding-operations`), scoped to the epic's sub-issues and filtered to `afk` (see `docs/agents/triage-labels.md`) — open, unblocked, unclaimed, `afk`, first in decomposition order. Never auto-pick a `hitl` ticket — that execution mode means a human routes it through `/to-guide` themselves, not this agent. Claim the chosen ticket (`gh issue edit <n> --add-assignee @me`) before any other write, the same way `/wayfinder` claims a ticket, so a concurrent session (e.g. a parallel worktree) doesn't pick the same one. If the filtered frontier is empty, stop and tell the user why: if open, unblocked, unclaimed tickets remain but all are `hitl`, say so and point at `/to-guide` instead of picking one yourself; otherwise explain that nothing is unblocked yet, or everything is already claimed. Once chosen this way, treat the ticket exactly like one named explicitly for the rest of this process.

If no ticket or epic reference is given at all: when this repo defines a git workflow doc (e.g. `docs/agents/git-workflow.md`) with an "Issue First" rule, stop and ask the user to name an existing ticket or run `/to-spec`/`/to-tickets` first — don't start the work. Otherwise, skip this check entirely.

Use /tdd where possible, at pre-agreed seams.

Run typechecking regularly, single test files regularly, and the full test suite once at the end.

Once done, use /code-review to review the work.

Commit your work to the current branch.

If this repo defines a git workflow doc (e.g. `docs/agents/git-workflow.md`), follow it to open a PR, then pause and ask the developer whether they want to review before it's considered done. Never merge the PR yourself — merging is the developer's call.

Before opening the PR, run the `qa-gate` skill if this repo has one — it's the full local check/test suite, run in isolation so its output doesn't clutter this context. Only proceed to `gh pr create` once it passes. If the git workflow doc specifies a PR body template with more structure than a short summary (e.g. numbered sections), delegate the body to the `pr-composer` subagent if this repo has one — it has its own read/diff access and fills the template properly, instead of you improvising a shorter body inline.

If this work is tied to the tracker ticket checked above: for a GitHub/GitLab-tracked ticket, the git workflow doc's mandatory `Closes #<ID>` in the PR body already closes it on merge — nothing further to do here for closing. If the ticket carries `task-report::required` (see `docs/agents/triage-labels.md`), post a completion-report comment on the issue when you open the PR — a short summary of what was implemented and how it was verified (reuse the `qa-gate` result if one ran this session), referencing the PR — unless the maintainer said to skip it. For a local-markdown-tracked ticket (no PR/merge step), set its `**Workflow:**` line to `done` once this ticket's work is complete; if it carries `**Task report:** required`, fold the same summary into that update instead of a separate comment.
