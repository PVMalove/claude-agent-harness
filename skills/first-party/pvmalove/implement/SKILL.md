---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---

# Implement

**Objective:** Implement the work described by the user in the spec or tickets.

## Coordinator route for valid backend orchestration

Before the normal local route, determine whether this project has a valid opt-in to
`backend-orchestration`:

- `.harness/harness.lock` selects `backend-orchestration`;
- `.harness/orchestration.json`, `.harness/orchestration/coordinator.py`, the role manifests, and
  the orchestration schema are present; and
- `harness health .` succeeds.

If any condition is absent or invalid, use the existing local route below unchanged. Do not repair
or infer an opt-in from a partial configuration.

For a valid opt-in, `/implement <id>` is the coordinator entry point. Resolve the ticket and run
the same AFK/blocker/issue-branch pre-flight below, but do not edit implementation files or run a
developer role in this session. Read the project role manifests and playbook, propose one batch
(ticket, issue branch/worktree, zone, Definition of Done, prohibitions, checks, dependencies, and
risk gates), and create it with `coordinator.py batch create`.

Show the plan and stop for explicit human approval. Only after approval may you run
`batch approve`, which changes the batch from `planned` to `awaiting-approval`. Every later
developer, code-review, QA, publish, or retry dispatch likewise requires a new explicit human
approval before creating its immutable brief. A worker report is evidence only: accept, override,
retry, block, fail, and any next dispatch remain coordinator decisions.

After an accepted developer report, risk-assess its candidate SHA before preparing review or QA.
An accepted clean review prepares QA; accepted green QA prepares a publish-only developer brief
for that same SHA. Do not create a PR, merge, close a ticket, or write to an integration branch.
Use the coordinator's publish boundary only after its explicit approval; it verifies and pushes the
accepted QA candidate SHA exactly.

## Execution in Three Phases

A strict pipeline, resolved in order: **Pre-flight** (confirm the ticket is actually startable, and by this agent) → **Coding** (TDD, tests, an explicitly approved review, commit, and push) → **PR & Wrap-up** (offer the separate `/to-pull-requests` command). Only the developer can select `/to-pull-requests`.

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
4. **Git pre-flight, before editing files:**
    - Resolve the exact integration branch from the ticket's `## Integration Branch` section or,
      for a child ticket that omits it, from its parent epic. An absent value is a blocker; do not
      infer a branch from memory, the current checkout, or a service name.
    - The current branch must match `branch_pattern` and be an issue branch. If it does not,
      fetch the integration branch and create `feature/issue-<ID>-<slug>` from it before coding.
      Never commit or push directly to the project base branch or an `integration/*` branch.

### Phase 2: Coding

1. Use `/tdd` where possible, at pre-agreed seams.
2. Run typechecking regularly, single test files regularly, and the full test suite once at the end.
3. Ask the developer: “Провести code review?” Stop for their answer.
    - **Yes:** run `/code-review`. It launches the Standards and Spec subagents through the coding application's manually configured mechanism, waits for both reports, and returns its separate `## Standards` and `## Spec` report to this primary session. Address any requested changes, then repeat the relevant tests before continuing.
    - **No:** record that the developer declined review and continue.
4. Ask the developer for explicit permission to commit and push. Stop for their answer.
5. After approval, verify that the current branch still matches `branch_pattern` and is neither `base_branch` nor `integration/*`; commit the completed work with a Semantic Commit Message and push it to the current issue branch. Report the commit and push result to the developer.

### Phase 3: PR & Wrap-up

After a successful push, offer `/to-pull-requests <ticket>` as the next command. Do not invoke it automatically, open a PR, run `qa-gate`, or close the ticket in this skill.
