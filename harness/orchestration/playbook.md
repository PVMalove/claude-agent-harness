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
blocker, but it cannot transition its own batch or silently widen its brief. The installed
`coordinator.py` implements the first public slice of this contract and keeps its local state under
the gitignored `.harness/orchestration/state/` directory.

| State | Coordinator action and entry condition | Allowed next state |
| --- | --- | --- |
| `planned` | Ticket, backend zone, issue branch/worktree, DoD, prohibitions, and verification commands are drafted. | `awaiting-approval`, `blocked` |
| `awaiting-approval` | The coordinator is waiting for the next explicit human decision: first the role dispatch, and later acceptance of a report. | `active`, `blocked`, `completed`, `failed`, `abandoned` |
| `active` | An approved dispatch has been handed to the runtime adapter; the role is executing only within its immutable brief. | `awaiting-approval`, `blocked`, `failed` |
| `completed` | All required role reports, commit proof, verification evidence, and risk gates are accepted. | terminal |
| `blocked` | An external dependency, missing authority, overlapping zone, or unavailable proof prevents safe continuation. | terminal |
| `failed` | The dispatch attempted work but could not produce an acceptable result. | terminal |
| `abandoned` | A human explicitly gave up on the batch after a completion report, with a recorded reason. | terminal |

`reported` is a terminal outcome for one role dispatch but remains pending coordinator decision. The
batch returns to `awaiting-approval` until the coordinator accepts, retries, blocks, fails, abandons,
or creates a new dispatch. `completed`, `blocked`, and `failed` are terminal outcomes for that
dispatch. A retry is a new dispatch with a new brief and a new dispatch ID; it is never a transition
from `blocked` or `failed` back to `working`, and the old brief is never edited.

## Retry routing and abandon

`batch decide --decision retry` does not always mean "ask a developer again". The coordinator
stores a routing record on the decision (`previous_role`, `reason_category`, `next_role`,
`next_action`, `rationale`, and the `candidate_commit` when it has not changed) and derives the
reason from structured report data only: outcome, review findings, Standards/Spec severity, failed
checks, and whether the candidate moved. Free text in `blockers` or `output` is never classified. An
approver may pass `--reason-category` (`code`, `requirements`, `candidate-change`,
`verification-infrastructure`, `transport`, `unknown`); it can only narrow a route toward a
same-candidate re-run when the structured data agrees, and it never overrides a finding.
Rate limits, compaction, context limits, an unavailable Bash/WSL wrapper and transport failures are
operational evidence: record them as `verification-infrastructure` or `transport`, never as a code
finding.

| Reporting stage | `accept` | `retry` | `block` / `fail` | `abandon` |
| --- | --- | --- | --- | --- |
| architect | developer | new architect | terminal | `abandoned` |
| developer | risk assessment | `developer-retry` (new candidate, then a new risk assessment) | terminal | `abandoned` |
| code-review | qa | new code-review on the same candidate only if the report is `blocked`, the reason is `verification-infrastructure` or `transport`, there is no finding on either axis, no failed check and the candidate is unchanged; otherwise `developer-retry` | terminal | `abandoned` |
| qa | publish | new qa on the same candidate under the same conditions (QA stays read-only); a defect or a new candidate means `developer-retry` | terminal | `abandoned` |
| publish | completed | new publish on the same accepted SHA for `verification-infrastructure` or `transport`; `developer-retry` when the candidate must change | terminal | `abandoned` |

An `unknown`, contradictory or unsupported reason always takes the safe route, `developer-retry`.
A same-candidate retry is a new immutable dispatch: it gets a new dispatch ID, re-checks the
base-commit gate and Context Package freshness, and needs its own explicit human approval under
`manual_all`. The earlier brief, report and blocker stay untouched as audit evidence. A retry never
uses an empty or fictitious commit, a changed candidate always needs a new risk assessment before
review or QA, and `block` or `fail` never start a retry by themselves. `--retry-role developer`
forces a developer retry where a same-candidate re-run would otherwise be routed.

`abandon` is a fifth decision on a completion report. It needs explicit approval and a non-empty
`--reason`, moves the batch to the terminal `abandoned` state and marks unfinished dispatches
`abandoned`. It keeps the worktree, candidate, briefs, reports, Context Packages and audit records,
closes no issue and opens no PR. It removes only leftovers that are not evidence: the staged
copies of reports in the agent inbox and the QA queue entries of dispatches that will never run.
The batch records `abandoned.last_accepted` (the newest accepted stage and candidate), so a fresh
batch can be created on the same branch and candidate. It is never a fallback for `block`, `fail`
or `retry`.

## Versioned lifecycle ledger

`coordinator.py` is the lifecycle ledger's CLI adapter. The selected state generation is named by
an atomic `ledger.json` pointer; immutable records and every transition append checksummed audit
records inside that generation. Normal execution never parses an old layout opportunistically.

For state created before the ledger, run `coordinator.py --repo . ledger migrate`. It copies the
legacy records into a complete candidate generation, validates JSON, batch-plan consistency and
audit checksums, then switches the pointer only after that validation succeeds. The legacy files
remain untouched as migration evidence. `ledger reset --confirm RESET` selects a fresh generation
only after the literal confirmation, and refuses while any batch is `active`; prior generations
remain immutable audit history.

When a new fact appears after dispatch, the coordinator appends a new coordinator decision before
acting on it. The decision records the dispatch ID, fact and evidence, impact on scope or risk,
chosen action, and author/time. The original brief remains immutable. If the fact changes the
scope, zone, DoD, assignment, or required proof, the current dispatch is ended and the changed
work is planned and approved as a new dispatch.

## Role order, liveness, and transport

The architect step is not optional and not implicit: no `developer` dispatch exists for a batch until
that batch already carries an `architect` completion report the coordinator has accepted. The
installed `coordinator.py` refuses to create the brief otherwise, so a manual CLI call cannot skip it
either.

A dispatched role is not assumed to be alive because it was sent. Its first action after receiving
its brief is a model self-report: it names the model it is actually running, and the coordinator
compares that against `resolved_model` in the immutable brief. When the project enables
`worker_attestation_required`, that same first action proves the canonical Git top-level, branch and
pinned candidate from the runtime's actual CWD. A model or worktree mismatch blocks the dispatch
immediately, and its completion report is refused; the recovery is a new dispatch, never an edited
brief. While it works, the role emits a heartbeat, and the coordinator watches for a silence longer
than its declared threshold. A stale dispatch is escalated to the human as a blocker; the coordinator
does not change state on a timeout by itself.

The transport carrying a role — an externally dispatched isolated worker, or an in-process subagent
of the coordinator session — is a project choice recorded in the assignment plan. An omitted
transport resolves to `in-process`; `orca` must be selected explicitly before an external worker can
start. It changes nothing
above: the same immutable brief goes out, the same self-report and heartbeat are required, and the
same completion report comes back. For an in-process handoff, `dispatch send` records the brief but
does not create an independent runtime: the coordinator launches that subagent immediately as its
next action, before any unrelated discovery.

An `approved` dispatch that has not yet been sent may be cancelled with recorded approval and reason.
The brief remains immutable evidence, its batch returns to `awaiting-approval`, and the coordinator
creates a new approved brief only after the corrected assignment is reviewed. `batch abandon` is for
a dispatch that cannot report, not for an unsent configuration mistake.

When the pinned snapshot already satisfies every Definition of Done, use `batch not-required` with
explicit approval and evidence instead of manufacturing a write-role commit. It terminally records
`not-required`, cancels any open dispatches, and returns the tracker recommendation
`resolution::wontfix`. This is not a successful implementation and must not weaken ordinary
write-role report validation: a normal write report still requires a real commit and exact changed
files.

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
- `verification commands`: exact project commands and any mandatory risk-review gate. A developer
  work dispatch may use focused developer commands; clean-room QA always uses the full verification
  commands;
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
- `tokens per batch`: provider- or runtime-observed input and output tokens attributed to every
  dispatch in a batch; a role self-report or completion report is not token telemetry, and
  unavailable data is marked as missing rather than estimated;
- `quality-gate wall time`: elapsed time from the serialized quality gate's start to its result,
  with queue time recorded separately when available;
- `post-integration defects`: defects linked to a batch after integration, using a project-declared
  observation window and severity rule.
- `cache read/write tokens`: provider-observed cache write and cache read token counts attributed to
  every dispatch in a batch, using the same attribution and missing-data rule as `tokens per batch`;
- `worker sessions per dispatch`: one plus every ledger-recorded checkpoint/resume continuation for a
  dispatch, with each session's coordinator-recorded compaction or restart reason (a recognized
  rate-limit termination, or a planned trigger such as a context limit, TDD-cycle count, or
  failure-log size); a role's own account of why it restarted is not this reason;
- `review diff scope excess`: the share of a code-review dispatch's diff files that fall outside the
  write role's declared zone, read from the immutable dispatch and risk-assessment records;
- `QA failure rate`: the share of a batch's decided QA dispatches whose coordinator decision was not
  `accept`, from the coordinator's recorded decision rather than a QA role's own outcome claim.

The baseline is evidence for later targets, not a hidden limit. It must not prescribe a provider,
model, Orca behavior, or hard-coded concurrency or token number.

Use the accompanying [pilot guide](pilot.md) to record the first observation period with the same
counting rules and missing-data treatment across batches.
