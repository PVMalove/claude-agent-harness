---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---

# Implement

**Objective:** Implement the work described by the user in the spec or tickets.

## Execution in Three Phases

A strict pipeline, resolved in order: **Pre-flight** (confirm the ticket is actually startable, and by this agent) → **Coding** (TDD, tests, review, commit) → **PR & Wrap-up** (open the PR, close out the ticket). Phase 3 carries its own confirmation checkpoints from `docs/agents/git-workflow.md` (ask before `gh pr create`, ask again once the PR is open) — those apply in addition to, not instead of, this phase order.

### Phase 1: Pre-flight

1. **Resolve the ticket.**
    - **A specific ticket is named** (an issue number, URL, or a `.scratch/<feature>/issues/NN-*.md` path): use it.
        - **Fail fast on `hitl`:** if it carries `hitl` (see `docs/agents/triage-labels.md`), stop immediately and tell the user to run `/to-guide` instead — don't check anything else on this ticket, this agent only implements `afk` work.
    - **An epic is named** (carries `workflow::specs`, or is otherwise the parent of a decomposition) rather than a specific ticket: pick the next ticket yourself instead of asking. Never auto-pick a `hitl` ticket — that execution mode always routes through `/to-guide`, not this agent.
        - **GitHub/GitLab:** run the same frontier query `/wayfinder` uses (`docs/agents/issue-tracker.md#wayfinding-operations`), scoped to the epic's sub-issues and filtered to `afk` — open, unblocked, unclaimed, first in decomposition order. Claim the chosen ticket (`gh issue edit <n> --add-assignee @me` on GitHub, `glab issue update <n> --assignee @me` on GitLab) before any other write, the same way `/wayfinder` claims a ticket, so a concurrent session (e.g. a parallel worktree) doesn't pick the same one. If the filtered frontier is empty, stop and tell the user why: if open, unblocked, unclaimed tickets remain but all are `hitl`, say so and point at `/to-guide`; otherwise explain that nothing is unblocked yet, or everything is already claimed.
        - **Local tracker:** read each `.scratch/<feature>/issues/NN-*.md` file in filename order — a purely linear chain — and take the first one that is both `**Workflow:** workflow::ready` and `**Execution:** afk`. If tickets remain but every `workflow::ready` one is `hitl`, say so — naming them — and point at `/to-guide` instead of picking one.
        - Once chosen this way, treat the ticket exactly like one named explicitly for the rest of this process.
    - **Nothing is named:** when this repo defines a git workflow doc (e.g. `docs/agents/git-workflow.md`) with an "Issue First" rule, stop and ask the user to name an existing ticket or run `/to-spec`/`/to-tickets` first — don't start the work. Otherwise, skip this check entirely.
2. **Check blockers**, on the resolved ticket. If it carries `workflow::blocked` (see `docs/agents/triage-labels.md`), check its blockers using this repo's tracker (native GitHub/GitLab dependency links, or the `Blocked by:`/`**Blocked by:**` field — see `docs/agents/issue-tracker.md`).
    - Any blocker still open → stop and tell the user which ones. Don't start the work.
    - All blockers closed/resolved → clear the block before proceeding:
        - **GitHub/GitLab:** replace the `workflow::blocked` label with `workflow::ready`.
        - **Local tracker:** set the file's `**Workflow:**` line to `workflow::ready`.
3. **Mark it in progress**, once you actually start work:
    - **GitHub/GitLab:** set the `workflow::in-progress` label.
    - **Local tracker:** set the file's `**Workflow:**` line to `workflow::in-progress`.

### Phase 2: Coding

1. Use `/tdd` where possible, at pre-agreed seams.
2. Run typechecking regularly, single test files regularly, and the full test suite once at the end.
3. Once done, use `/code-review` to review the work.
4. Commit your work to the current branch.

### Phase 3: PR & Wrap-up

1. **Open the PR**, if this repo defines a git workflow doc (e.g. `docs/agents/git-workflow.md`) — follow it, then pause and ask the developer whether they want to review before it's considered done. Never merge the PR yourself — merging is the developer's call.
    - Before opening, run the `qa-gate` skill if this repo has one — the full local check/test suite, run in isolation so its output doesn't clutter this context. Only proceed to `gh pr create` once it passes.
    - If the PR body template has more structure than a short summary (e.g. numbered sections), delegate the body to the `pr-composer` subagent if this repo has one, instead of improvising a shorter body inline.
2. **Close out the ticket**, if this work is tied to the ticket resolved in Phase 1:
    - **GitHub/GitLab:** the git workflow doc's mandatory `Closes #<ID>` in the PR body already closes the ticket on merge — nothing further to do here. If it carries `task-report::required` (see `docs/agents/triage-labels.md`), post a completion-report comment on the issue when you open the PR — a short summary of what was implemented and how it was verified (reuse the `qa-gate` result if one ran this session), referencing the PR — unless the maintainer said to skip it.
    - **Local tracker:** there's no PR/merge step. Once this ticket's work is complete, set its `**Workflow:**` line to `done`; if it carries `**Task report:** required`, fold the same summary into that update instead of a separate comment.
