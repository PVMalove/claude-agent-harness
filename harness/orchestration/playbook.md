# Backend batch orchestration playbook

This playbook is the runtime-neutral coordination contract for the optional
`backend-orchestration` capability. It is a manual protocol for a coordinator; it does not
dispatch work, select a provider, or require an Orca adapter. A runtime adapter may translate
these records into its own commands later, but it must preserve the rules below.

## Authority and invariants

The coordinator owns the batch lifecycle, dispatch approval, scope changes, and the decision to
accept a completion report. The role manifest is authoritative for role mode, write boundary,
required proof, and risk triggers. Project configuration resolves the provider profile, model,
fallback, zone, concurrency budget, and verification commands; it cannot weaken the manifest
contract.

Every batch has one ticket, one issue branch, and one isolated worktree. The coordinator records
the resolved provider profile and model in the dispatch brief. Credentials never belong in the
brief, project configuration, or reports.

## Lifecycle

The coordinator records exactly one current state for each batch. A role may report progress or a
blocker, but it cannot transition its own batch or silently widen its brief.

| State | Coordinator action and entry condition | Allowed next state |
| --- | --- | --- |
| `planned` | Ticket, backend zone, role sequence, DoD, prohibitions, and verification commands are drafted. | `approved`, `blocked` |
| `approved` | A human approves the cost, assignment, scope, parallelism, and required gates. | `dispatched`, `blocked` |
| `dispatched` | The coordinator records a dispatch ID, resolved assignment, and immutable handoff brief, then sends it to the role. | `working`, `blocked`, `failed` |
| `working` | The role has acknowledged the brief and is executing only within its declared boundary. | `completed`, `blocked`, `failed` |
| `completed` | All required role reports, commit proof, verification evidence, and risk gates are accepted. | terminal |
| `blocked` | An external dependency, missing authority, overlapping zone, or unavailable proof prevents safe continuation. | terminal |
| `failed` | The dispatch attempted work but could not produce an acceptable result. | terminal |

`completed`, `blocked`, and `failed` are terminal outcomes for that dispatch. A retry is a new
dispatch with a new brief and a new dispatch ID; it is never a transition from `blocked` or
`failed` back to `working`, and the old brief is never edited.

When a new fact appears after dispatch, the coordinator appends a new coordinator decision before
acting on it. The decision records the dispatch ID, fact and evidence, impact on scope or risk,
chosen action, and author/time. The original brief remains immutable. If the fact changes the
scope, zone, DoD, assignment, or required proof, the current dispatch is ended and the changed
work is planned and approved as a new dispatch.

## Immutable handoff brief

The coordinator creates the brief before dispatch and stores the exact version sent to the role.
It must contain, at minimum:

- `ticket`: tracker ID and the originating acceptance criteria;
- `dispatch ID`: unique ID for this attempt, plus the parent batch ID when retries exist;
- `role`: selected role manifest and its read-only or write mode;
- `resolved provider profile` and `model`: the project-owned assignment actually selected, including
  the fallback used if the default was unavailable;
- `zone IDs` and allowed paths: the exact backend zones the role may read or write;
- `branch/worktree`: issue branch and isolated worktree; protected branches and `integration/*`
  are never write targets;
- `Definition of Done`: observable acceptance criteria and the expected role output;
- `prohibited changes`: paths, operations, or decisions outside the declared scope;
- `verification commands`: exact project commands and any mandatory risk-review gate;
- `dependencies and assumptions`: known blockers, required inputs, and their owner;
- `coordinator approval`: approving person, timestamp, and the approved concurrency decision.

The brief is a starting contract, not a conversation buffer. A role must escalate an ambiguity,
overlap, credential request, irreversible action, policy decision, or missing proof. It must not
amend the brief through chat or treat a later message as an unrecorded scope change.

A minimal brief can be rendered as:

```markdown
# Dispatch brief: <dispatch-id>

- Ticket: <tracker-id>
- Batch: <batch-id>
- Role: <manifest-name> (<write|read-only>)
- Resolved provider profile: <profile-id>
- Model: <resolved-model>
- Zone IDs: <zone-id>, ...
- Allowed paths: <declared paths>
- Branch/worktree: <issue branch> / <isolated worktree>
- Definition of Done: <observable acceptance criteria>
- Prohibited changes: <out-of-scope paths and operations>
- Verification commands: <exact commands>
- Required gates: <risk and quality gates, or none>
- Dependencies and assumptions: <known inputs and owners>
- Coordinator approval: <person>, <timestamp>
```

## Completion report

Each dispatched role returns one completion report. The coordinator accepts the batch only after
the report is complete and its evidence is independently sufficient for the role's contract.

The report must include:

- dispatch ID, ticket, role, and outcome (`completed`, `blocked`, or `failed`);
- a concise output summary mapped to the brief's Definition of Done;
- `commit SHA` containing the work, required for every write role; a read-only role records
  `not applicable — read-only role` and must not create a production commit;
- the exact `changed files`, or an explicit `none` for read-only work;
- a `checks run` list with every exact command, result, and relevant evidence; “done” is not a
  check result;
- residual `risks`, including unverified edge cases and deferred decisions;
- `blockers`, or an explicit `none`;
- the next coordinator action, including the required independent gate when applicable.

For the high-risk triggers in the `code-review` manifest, Standards and Spec are separate
read-only reports. They must both be present before the coordinator accepts the batch; one combined
rating cannot replace either report.

Use this shape so missing proof is visible:

```markdown
# Completion report: <dispatch-id>

- Outcome: <completed|blocked|failed>
- Output: <what was produced>
- Commit SHA: <sha|not applicable — read-only role>
- Changed files: <exact paths|none>
- Checks run:
  - `<exact command>` — <pass|fail>, <evidence>
- Risks: <residual risks|none>
- Blockers: <blockers|none>
- Next coordinator action: <accept, block, fail, or create a new dispatch>
```

The coordinator does not rewrite a report to make it pass. A missing commit SHA, changed-file
list, check result, risk statement, or blocker statement is a proof gap and keeps the batch from
being marked `completed`.

## Parallel work and quality gates

Parallelism is allowed only for independent work that the coordinator has approved:

- separate batches may run concurrently when their service, bounded context, and infrastructure
  zone IDs do not overlap and the project `concurrency_budget` allows it;
- read-only work may run in parallel when it has no overlapping write operation or contradictory
  brief;
- role handoffs inside one batch are sequential, and there is one active writer at a time;
- multiple roles must never write to the same batch simultaneously, even if their paths appear
  different;
- heavy integration and verification runs use one serialized quality-gate lane; independent
  implementation work may continue while it waits, but concurrent heavy gates are not started;
- a conflict, unclear boundary, or unavailable lane is escalated as a blocker rather than resolved
  by overlapping writes or an unapproved retry.

Before dispatch, the coordinator records the zone comparison, active batches, writer, and quality
gate lane position. After each handoff, the incoming role receives the prior completion report as
evidence but still receives its own immutable brief.

## Baseline metrics

The first pilot records observations without importing a provider, runtime, or fixed numerical
target. Each observation includes the measurement period, batch/ticket IDs, source, missing-data
notes, and the same counting rules across the period.

- `agent starts per closed ticket`: role starts recorded for closed tickets during the period,
  with retries counted as new dispatch starts;
- `tokens per batch`: reported input and output tokens attributed to every dispatch in a batch,
  with unavailable runtime data marked as missing rather than estimated;
- `quality-gate wall time`: elapsed time from the serialized quality gate's start to its result,
  with queue time recorded separately when available;
- `post-integration defects`: defects linked to a batch after integration, using a project-declared
  observation window and severity rule.

The baseline is evidence for later targets, not a hidden limit. It must not prescribe a provider,
model, Orca behavior, or hard-coded concurrency or token number.
