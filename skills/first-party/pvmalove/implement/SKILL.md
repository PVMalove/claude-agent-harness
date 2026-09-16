---
name: implement
description: "Coordinate one backend ticket through the approved architect, developer, review, QA, and publish handoffs."
disable-model-invocation: true
---

# Implement

**Objective:** Coordinate exactly one ticket through `architect → developer → code-review → qa → publish`, then offer `/to-pull-requests`. This session **is** the coordinator: it creates and observes dispatches but never implements the ticket itself.

## Route

This is the opt-in `backend-orchestration` route. Confirm that its installed coordinator is usable:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch status
```

If the command is unavailable or cannot read its state, stop and tell the developer to run `/fast-implement` instead.
Do not infer or repair an opt-in capability. Process one ticket to a terminal batch state before
beginning another.

## Coordinator contract

Resolve the ticket, its integration branch and its blockers through the project's tracker and Git
guidance. Use an isolated issue branch and worktree; protected and `integration/*` branches are
never write targets. Read the ticket's existing dispatch status before planning: an open batch is
evidence to present to the developer, not state to reuse or replace.

Before proposing each handoff, run `dispatch preflight` for its role. It resolves the project-owned
runtime, verifies the pinned Git worktree and snapshot, and returns a preview only; it never creates
a brief or starts a worker. Then show `batch decision-packet` beside every explicit approval request.
A report
is evidence, never authority to advance the batch. The architect is required
before a developer dispatch; review keeps independent Standards and Spec evidence; independent QA
verifies the candidate commit; publish pushes only the accepted SHA. The final report precedes the
separately approved publish dispatch.

Every dispatched role first records a model self-report against its immutable brief and emits
heartbeats while it works. Between send and report, run `dispatch wait`: it returns only `reported`,
`stale`, `model_mismatch`, `rate_limited`, or `failed`, never heartbeat chatter. A mismatch or stale
dispatch is a blocker for the developer; this event-driven watchdog recovers with a newly approved dispatch, never by editing a
brief or state record. For a 429, record the checkpoint and retry window with `dispatch rate-limited`;
resume only through the configured continuation policy after that window.

Before every architect, developer and code-review dispatch, the coordinator registers a fresh,
role-specific deterministic Context Package from pinned commits and links its ID and hash from the
brief. Reuse its exact diff, bounded dependency context, tests, and precedent cards inside the
batch; it is evidence, not a replacement for the immutable brief or human approval. A write-role may save a non-terminal checkpoint and resume the same dispatch in a
new worker session after a fresh self-report and heartbeat. Read-only roles cannot checkpoint or
resume, and planned-trigger resumes still require an existing coordinator decision.

The coordinator fetches the integration base and rechecks the base-commit gate before review and
publish. A stale base requires a new developer/rebase dispatch. After a Warning, delta-review is
allowed only for a new candidate whose diff is test-only; it is always a separate independent
review dispatch.

For an in-process transport, send records the immutable handoff and the coordinator immediately
launches the role subagent in the declared worktree. An external adapter is transport-only and must
preserve the same handoff, liveness, approval, and report contract.

## Authoritative guidance

This is a short coordinator contract, not a second orchestration manual. Full rules are module-owned guidance:

- `.harness/orchestration/playbook.md` owns lifecycle, authority, immutable brief, completion
  evidence, parallelism, and metric rules.
- `.harness/orchestration/roles/` owns each role's boundary, required proof, and specialist trigger.
- `docs/agents/backend-orchestration.md` owns setup, project configuration, CLI procedure, and
  operational recovery.
- `docs/agents/git-workflow.md` owns issue-branch, commit, push, and PR boundaries.

Follow those files rather than duplicating or weakening their rules here. In particular, do not
invent token metrics: use only provider- or runtime-observed telemetry and preserve missing-data
notes.

## Wrap-up

After an accepted publish, offer `/to-pull-requests <ticket>`. Do not invoke it automatically, open
or merge a PR, write to an integration branch, or close the ticket in this skill.
