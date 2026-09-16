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

Resolve the tracker ticket, issue branch and blockers without opening a second batch for the same
work. Before `batch create`, run the batch-level preflight with bounded expected files, services and
diff size. If it rejects the ticket, split it with `/to-tickets`; never ask an architect to discover
whether an oversized ticket should have been split.

For each handoff, run `dispatch preflight`, show `batch decision-packet`, then create an approved
immutable brief. The coordinator never writes feature code or repairs state by hand. A report is
evidence, not permission to advance. Architect precedes developer; accepted candidate proceeds
through the required review/QA/publish gates.

Every transition needs explicit approval. Each worker records a model self-report and is observed
by the event-driven watchdog; those facts are evidence, never a reason to edit an immutable brief.

Use the brief's shared Context Package ID across architect, developer and resumed worker sessions
when the pinned base/candidate is unchanged. Read only its starting files and directly referenced
symbols before escalating. A changed candidate creates one new shared package for its review, not a
role-specific duplicate. Continuations and retries stop at the project budget; a 429 is not an
exception to that budget.

Run `dispatch wait` between send and report. `stale`, model/worktree mismatch, changed harness
snapshot, and a failed deterministic gate are blockers for the coordinator, not prompts for broad
LLM recovery. In-process and external transports preserve the same brief and evidence contract.

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
