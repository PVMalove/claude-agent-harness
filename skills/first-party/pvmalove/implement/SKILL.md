---
name: implement
description: "Implement a piece of work as a gated coordinator pipeline: architect, developer, code review, and independent QA."
disable-model-invocation: true
---

# Implement

**Objective:** Take one ticket through architect → developer → code-review → QA, pausing for the
developer's explicit approval at every gate, and hand the finished candidate to `/to-pull-requests`.

This session **is** the coordinator. It drives `coordinator.py` — batch, dispatch, report, decide —
and never edits implementation files itself. Work is done by dispatched roles.

It is step 5 of the delivery chain — `/grill-with-docs` → `/to-spec` → `/to-tickets` →
**`/implement <id>`** → `/to-pull-requests` — and it takes exactly one ticket from that
decomposition.

## Context and effort budget

Start `/implement` in a new, clean context. If this session has completed unrelated exploration or
another ticket, retain the ticket reference in the tracker and ask the developer to run `/clear`
before beginning; do not carry old dispatches, reports, or exploratory output into this batch.

Use `medium` effort for the coordinator and architect by default. A project assignment plan may
declare its own effort; otherwise pass `--effort medium` when creating the architect dispatch. Use a
higher effort only when the developer explicitly approves it for a named hard-to-reverse decision.

## Route selection

`/implement` needs the coordinator CLI, which the harness installs into the project itself. Every
command in this skill is invoked exactly like this — there is no `harness` executable to find, and no
`coordinator.py` on PATH:

```bash
python .harness/orchestration/coordinator.py --repo . <команда>
```

Prove the route with one command, which doubles as the inventory of what is already in flight:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch status
```

Exit 0 means the CLI runs and its state directory is readable. Do not go looking for a `harness`
command: the packager CLI lives in the harness repository, not in a harnessed project, so
`harness health` is not runnable here and its absence says nothing about this route.

`.harness/orchestration.json` is **optional**. Without it the coordinator defaults the zone to the
whole repository (`repository`) and takes the role's model and effort from this session, passed as
`--model`/`--effort` on `dispatch create`. With it, the project owns zones, models, effort, and the
per-role `transport`.

If that command is missing or exits non-zero, do not repair or infer an opt-in: stop and tell the
user to run `/fast-implement` instead, which is the ungated single-session path.

Process one ticket to a terminal batch state before starting another. Never track overlapping
approvals for unrelated tickets.

## Phase 1: Pre-flight

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
4. **Git pre-flight, before any dispatch:**
    - Resolve the exact integration branch from the ticket's `## Integration Branch` section or,
      for a child ticket that omits it, from its parent epic. An absent value is a blocker; do not
      infer a branch from memory, the current checkout, or a service name.
    - Create the batch's issue branch `feature/issue-<ID>-<slug>` from that integration branch. It
      must match `branch_pattern`; the coordinator refuses a base or `integration/*` branch.
    - Create the batch's worktree for that branch with plain `git worktree add`, and **stay in the
      main checkout**. This session is the coordinator, not a worker: its state lives in
      `.harness/orchestration/state/` relative to `--repo`, so entering the worktree would fork that
      state into a second copy and the batch you create there would be invisible from the repository
      root. You only need the worktree's path — `batch create` takes it as `--worktree`.
    - Do not use a worktree tool that switches this session into the tree, and do not edit
      `.claude/settings.local.json` or any other tool configuration to make one behave. Changing a
      project's configuration is not part of implementing a ticket.

## Phase 2: The batch

**First look at what already exists for this ticket.** The `dispatch status` output from route
selection lists every dispatch the coordinator knows, each with its `ticket`, `batch_id`, `state` and
`stale` flag. If any entry names this ticket, stop before proposing anything and show the developer
what you found: which batch, which role, what state, and how long it has been silent.

A leftover dispatch from an earlier attempt is the exact situation this pipeline exists to catch, so
treat it as evidence, not as debris. Never reuse it, never delete it, and never open a second batch
beside it — the coordinator refuses an overlapping zone anyway, and a silent retry is how the
original incident burned a usage window. The developer decides what happens.

Close the old batch through the CLI, never by editing anything under
`.harness/orchestration/state/`. Those records are the audit trail; hand-editing them destroys the
one thing this pipeline produces.

- **Its report is in and awaiting a decision** — `batch decide` with `accept`, `override-warning`,
  `retry`, `block` or `fail`, as usual.
- **It can no longer report at all** — a worker that died before its model self-report never will,
  and a batch with no pending report cannot be decided. That is what `batch abandon` is for:

  ```bash
  python .harness/orchestration/coordinator.py --repo . batch abandon \
    --batch <batch-id> --approved-by '<кто>' --approved-at <ISO-8601> \
    --reason '<почему решение больше недостижимо>'
  ```

  It requires an explicit approval and a reason, marks the batch `failed`, closes every open
  dispatch, and deletes nothing. `batch list --open` shows what is still unclosed; `--ticket <id>`
  narrows it to one ticket.

Only once nothing is open for the ticket do you continue.

Then read `.harness/orchestration/roles/` and `.harness/orchestration/playbook.md`. Propose one
batch — ticket, issue branch/worktree, zone, Definition of Done, prohibitions, checks, dependencies,
risk gates — and show it to the developer.

Keep this Gate 0 proposal compact: derive it from the current ticket, its direct dependencies, the
role manifests, and the named project contract. Do not inspect old batch state or previous-role
templates to reconstruct a new brief. State only observable acceptance criteria, the narrowest
credible zone, explicit prohibitions, and the commands that developer and QA must later run.

The brief is the only channel a dispatched role has, so this repo's own delivery rules must be
written into the batch rather than assumed. Read `docs/agents/git-workflow.md` and carry its
implementation contract into the `--definition-of-done` entries verbatim enough to be checkable. In
particular, when that doc mandates TDD, one Definition-of-Done entry must say so — for example
`write the failing test first at the seams the architect named, then make it pass` — because a
dispatched developer inherits nothing from `/tdd`: it is not this session, and `verification_commands`
only run tests afterwards, they never require that the test came first.

**Gate 0 — the plan.** Stop for explicit approval. Only after it, run `batch create` and then
`batch approve`.

## Phase 3: The five gates

The sequence below is fixed. Run every step, in this order, for every ticket — a low-risk change
does not earn a shorter path here; a ticket that does not deserve the ceremony belongs in
`/fast-implement` instead.

```text
architect ─► HUMAN approve ─► developer ─► code-review ─► HUMAN approve ─► qa ─┐
                                  ▲                                            │
                                  └────── qa findings: developer fixes ◄────────┤
                                                                                │
                            final report ─► publish ─► HUMAN runs /to-pull-requests
```

Every gate is the same shape: propose → **stop for the developer's explicit approval** →
`dispatch create` → `dispatch send` → watch → `report submit` → `batch decide`. Never create a brief
before its approval, and never decide on a report for the developer. A worker report is evidence
only: accept, override, retry, block, and fail remain coordinator decisions.

1. **Architect.** `--role architect`. Read-only: it analyses the architecture as it exists today in
   this repository and proposes the plan — boundaries, viable options, the selected option,
   trade-offs, risks, acceptance criteria, and the seams the tests should sit on — with repository
   evidence for each. **This report is what the human approves before any code is written.** The
   coordinator enforces the order: `dispatch create --role developer` fails until an architect
   report for this batch has been accepted, so the step cannot be skipped from the CLI either. Ask
   for one concise decision brief; its completion report links to that brief rather than duplicating
   it. The architect uses targeted evidence only and must not run the batch's full verification suite
   merely to establish a baseline; developer and independent QA own those checks.
2. **Developer.** `--role developer`, after the accepted architect report. It implements on the
   batch's own issue branch in its isolated worktree test-first — the failing test at the architect's
   seams before the code that satisfies it, when the batch's Definition of Done requires TDD — runs
   the checks, commits, and pushes that issue branch, never a base or `integration/*` branch. Its
   commit SHA is the candidate. Its report's `changed_files` is where you see whether tests actually
   came with the change: an implementation-only diff against a TDD Definition of Done is a
   `--decision retry`, not an accept. Then run `risk assess` on that SHA.
3. **Code review.** `--role code-review --candidate-commit <sha>`, pinned to the assessed candidate,
   read-only, run for every candidate — the risk assessment decides only whether review is
   *mandatory*, never whether it is allowed. Standards and Spec stay separate evidence. A blocker
   requires `--decision retry`; a warning requires `--decision override-warning` with a recorded note.
   **Show the human the reviewed changes and the two axes, and stop for their approval to spend QA.**
4. **QA.** `--role qa --candidate-commit <sha>` after the accepted review. QA is independent: it runs
   in the clean-room lane (`qa run`) against the pinned SHA, not through a transport, and not on the
   developer's own checks. A QA finding is not a defeat: `batch decide --decision retry` sends the
   work back to the developer with the findings, and the loop re-runs risk assessment, review, and QA
   on the new candidate once the fix reports. Repeat until QA is green.
5. **Final report, then publish.** After accepted green QA, give the human the closing report
   *before* anything is published: ticket, candidate SHA, what each role concluded, accepted QA
   evidence and its artifact path, residual risks, and what is deliberately left out. Then create the
   publish dispatch (`--role developer --purpose publish --candidate-commit <sha>`). Never hand a
   publish brief to `dispatch send`/an adapter; use `dispatch publish`, the coordinator's own
   verified boundary, which pushes exactly the accepted SHA.

## Phase 4: Watching a dispatch

Between `dispatch send` and the report, this session is the watchdog. Poll:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch status --batch <batch-id>
```

- **Model self-report.** Every dispatched role, on any transport, confirms its actually active model
  as its first action (`dispatch self-report --dispatch <id> --model <model>`). A mismatch against
  the immutable brief blocks the dispatch immediately, and the coordinator refuses its completion
  report. Surface it to the developer as a blocker; the fix is a new dispatch, never an edited brief.
- **Heartbeat.** A role calls `dispatch heartbeat --dispatch <id>` while it works. When `dispatch
  status` reports `stale` for a dispatch, stop waiting and tell the developer which dispatch went
  silent and for how long. Do not silently keep waiting: a stalled dispatch spends the usage window
  and produces nothing.

For an `in-process` transport, `dispatch send` takes no adapter: it records the handoff, then this
session must immediately launch the role subagent against the same immutable brief in the batch's
worktree. Launch is the next action after `dispatch send`; do not inspect old templates, reports, or
configuration before it. The subagent performs the self-report and heartbeat calls itself. For
`orca`, pass `--adapter .harness/orchestration/orca_adapter.py`.

## Phase 5: PR & wrap-up

Offer `/to-pull-requests <ticket>` as the next command. Do not invoke it automatically, open or merge
a PR, write to an integration branch, or close the ticket in this skill.