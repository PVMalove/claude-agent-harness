# Backend batch orchestration playbook

This playbook is the runtime-neutral coordination contract for the optional
`backend-orchestration` capability. It is a manual protocol for a coordinator; it does not
dispatch work, select a provider, or require a runtime adapter. An adapter may translate
these records into its own commands later, but it must preserve the rules below.

Before the first English handoff, the coordinator must read
[Technical English](../docs/technical-english.md).

## Authority and invariants

The coordinator owns the batch lifecycle, dispatch approval, scope changes, and the decision to
accept a completion report. The role manifest is authoritative for role mode, write boundary,
required proof, and risk triggers. Project configuration resolves the provider profile, model,
fallback, role write ceiling, concurrency budget, and verification commands; it cannot weaken the manifest
contract.

Every batch has one ticket, one issue branch, and one isolated worktree. The coordinator records
the resolved provider profile and model in the dispatch brief. Credentials never belong in the
brief, project configuration, or reports.

## Lifecycle

The coordinator records exactly one current state for each batch. A role may report progress or a
blocker, but it cannot transition its own batch or silently widen its brief. The installed
`coordinator.py` implements this contract's lifecycle and keeps its local state under
the gitignored `.harness/orchestration/state/` directory.

| State | Coordinator action and entry condition | Allowed next state |
| --- | --- | --- |
| `planned` | Ticket, explicit allowed paths, issue branch/worktree, DoD, prohibitions, and verification commands are drafted. | `awaiting-approval`, `blocked`, `failed`, `not-required` |
| `awaiting-approval` | The coordinator is waiting for the next explicit human decision: first the role dispatch, and later acceptance of a report. | `active`, `blocked`, `completed`, `failed`, `not-required`, `abandoned` |
| `active` | An approved dispatch has been handed to the runtime adapter; the role is executing only within its immutable brief. | `awaiting-approval`, `blocked`, `failed`, `not-required` |
| `completed` | All required role reports, commit proof, verification evidence, and risk gates are accepted. | terminal |
| `blocked` | An external dependency, missing authority, an exhausted concurrency budget, or unavailable proof prevents safe continuation. | `awaiting-approval` only through `batch resume --reason` for a startup-blocked or stale batch (a human `block` decision is never resumable); `failed` |
| `not-required` | `batch not-required` recorded, with approval and evidence, that the pinned snapshot already satisfies every DoD item. | terminal |
| `failed` | The dispatch attempted work but could not produce an acceptable result. | terminal |
| `abandoned` | A human explicitly gave up on the batch after a completion report, with a recorded reason. | terminal |

`reported` is a terminal outcome for one role dispatch but remains pending coordinator decision. The
batch returns to `awaiting-approval` until the coordinator accepts, retries, blocks, fails, abandons,
or creates a new dispatch. `completed`, `blocked`, and `failed` are terminal outcomes for that
dispatch. A retry is a new dispatch with a new brief and a new dispatch ID; it is never a transition
from `blocked` or `failed` back to `working`, and the old brief is never edited.

`needs_attention` is not a lifecycle state. It is a flag on a batch (see "Attention state" below)
that halts creation of the next dispatch without changing `state`, `next_action`, the candidate or
any evidence.

## Retry routing and abandon

`batch decide --decision retry` does not always mean "ask a developer again". The coordinator
stores a routing record on the decision (`route`, `previous_role`, `reason_category`, `next_role`,
`next_action`, `rationale`, and the `candidate_commit` when it has not changed) and derives the
reason from structured report data only: outcome, review findings, Standards/Spec severity, failed
checks, and whether the candidate moved. Free text in `blockers` or `output` is never classified. An
approver may pass `--reason-category` (`code`, `requirements`, `candidate-change`,
`verification-infrastructure`, `transport`, `context-pressure`, `tooling`, `block-bypass`,
`unknown`); it can only narrow a route toward a same-candidate re-run when the structured data
agrees, and it never overrides a finding. Rate limits, an unavailable Bash/WSL wrapper and transport
failures are operational evidence: record them as `verification-infrastructure` or `transport`,
never as a code finding. A context limit is `context-pressure` only when a critical
`context_pressure` observation was recorded for the reported dispatch; the claim alone is `unknown`.
In this table and the Recovery route table, an operational reason is one of these three categories;
`tooling` and `block-bypass` have their own routes.

| Reporting stage | `accept` | `retry` | `block` / `fail` | `abandon` |
| --- | --- | --- | --- | --- |
| architect | developer | new architect | terminal | `abandoned` |
| developer | risk assessment | `verification` on the registered candidate when the report is `blocked` with an operational reason; otherwise `developer-retry` (new candidate, then a new risk assessment) | terminal | `abandoned` |
| verification | risk assessment | `developer-retry` | terminal | `abandoned` |
| code-review | qa | new code-review on the same candidate only if the report is `blocked`, the reason is `verification-infrastructure`, `transport` or `context-pressure`, there is no finding on either axis, no failed check and the candidate is unchanged; otherwise `developer-retry` | terminal | `abandoned` |
| qa | publish | new qa on the same candidate under the same conditions (QA stays read-only); a defect or a new candidate means `developer-retry` | terminal | `abandoned` |
| publish | completed | new publish on the same accepted SHA for `verification-infrastructure`, `transport` or `context-pressure`; `developer-retry` when the candidate must change | terminal | `abandoned` |

`code`, `requirements`, `candidate-change` and `unknown` always route to `developer-retry`; only the
three operational categories, besides `tooling` and `block-bypass` below, may re-run a read-only
stage on the same SHA, and only with empty findings, an unchanged candidate and no scope or
requirement blocker. A contradictory or unsupported reason always takes the safe route,
`developer-retry`.

`tooling` means a hook, the safety classifier or the ledger blocked a legitimate role action. The
coordinator assigns it only from a `blocked` report's structured `tooling_blocker` (`tool`, exact
`command`, `message`), when no finding, failed check, moved candidate or developer category
outranks it; `--reason-category tooling` without that field is `unknown`, and another named
operational category keeps its own route. Its route is `tooling-retry` at every stage: a new
dispatch of the same stage on the same SHA (architect, verification, code-review, qa or publish),
and for a developer a `developer-retry` that continues its last commit, recorded as the routing
record's `candidate_commit`. A `tooling-retry` spends no `retry_policy.max_developer_retries` and
is not refused when that budget is exhausted; the human decision on every retry and the
`tooling-retry-repeated` attention bound it instead. `--retry-role developer` on a read-only stage
still forces a budgeted `developer-retry`.
A same-candidate retry is a new immutable dispatch: it gets a new dispatch ID, re-checks
Context Package freshness, and needs its own explicit human approval under
`manual_all`. The earlier brief, report and blocker stay untouched as audit evidence. A retry never
uses an empty or fictitious commit, a changed candidate always needs a new risk assessment before
review or QA, and `block` or `fail` never start a retry by themselves. `--retry-role developer`
forces a developer retry where a same-candidate re-run would otherwise be routed.

`block-bypass` means a read-only role (code-review, qa or verification) worked around a hook or tool
block instead of stopping with `tooling_blocker`. Only an approver names it, and none of that report
is evidence: its findings, failed checks and outcome do not route the retry, and only a moved
candidate still sends it to `developer-retry`. Its route is `bypass-rerun`: a new dispatch of the
same stage on the same SHA (verification on its registered candidate) with no new candidate commit.
`batch decide` requires a `--note` naming the violation, the report is never accepted or
warning-overridden, and the re-run dispatch always needs an explicit approval, under every
`approval_policy`. A `bypass-rerun` spends no `retry_policy.max_developer_retries`. `block-bypass`
is refused for an architect, developer or publish report, which is still retried with a developer
reason category (`code`, `requirements`, `candidate-change`) or blocked.

A code-review `blocker` can never be accepted. While `retry_policy.max_developer_retries` still
allows a developer retry, it takes `retry` or `abandon`; once that budget is exhausted, `retry` is
refused and the blocker takes `block`, `fail` or `abandon`, after which the work is split or
re-planned in a new batch. A `tooling-retry` neither spends this budget nor is refused by it.

`abandon` is a decision on a completion report, alongside `accept`, `override-warning`, `retry`,
`block` and `fail`. It needs explicit approval and a non-empty
`--reason`, moves the batch to the terminal `abandoned` state and marks unfinished dispatches
`abandoned`. It keeps the worktree, candidate, briefs, reports, Context Packages and audit records,
closes no issue and opens no PR. It removes only leftovers that are not evidence: the staged
copies of reports in the agent inbox and the QA queue entries of dispatches that will never run.
The batch records `abandoned.last_accepted` (the newest accepted stage and candidate), so a fresh
batch can be created on the same branch and candidate. It is never a fallback for `block`, `fail`
or `retry`.

## Recovery route table

Every `retry` and `abandon` decision of `batch decide` records its recovery route as
`routing.route`, one value of the closed set `RECOVERY_ROUTES`. The route is set in the same step
that sets `next_action`, from the same structured evidence, and never from free text. `batch abandon`
(a batch that will never have a report) and `batch resume` record no route. `report-completion` is
not recorded by `batch decide`: `report submit` names it in its `completion` object when the policy
chain after a recorded report stops, and the coordinator completes that chain with
`report complete --dispatch <dispatch-id>`. `carry-over` is recorded by an `accept` or
`override-warning` with `--findings-file` and by `batch carry-over`; its routing record names the
`carried_item_ids` and is never applied to `next_action`, which moves through risk assessment as on
any developer accept; `batch carry-over` itself moves a `next_action` of `qa`, set by an earlier
assessment, to `code-review`. The coordinator chooses a route by this table:

| Situation | Route | Who approves | Evidence |
| --- | --- | --- | --- |
| An architect report is retried, whatever the reason category except `tooling` | `architect-retry` | A human decides the retry with `batch decide`; the new architect dispatch is approved under `approval_policy` | Dispatch ID and `report_sha256` of the architect report. Not `same-candidate-rerun`: it is not conditioned on a reason category and pins no candidate |
| A developer report is `blocked` with an operational reason, no finding and no failed check | `verification` | A human decides the retry; the read-only verification dispatch is approved under `approval_policy` | Dispatch ID, `report_sha256`, the `candidate_registrations` entry with its `source_report_sha256`, and for `context-pressure` the critical `context_pressure` record |
| A code-review, qa or publish report is `blocked` with an operational reason, no finding on either axis, no failed check and an unchanged candidate | `same-candidate-rerun` | A human decides the retry; the new dispatch on the same SHA needs its own approval under `approval_policy` | Dispatch ID, `report_sha256`, the unchanged `candidate_commit`, and for `context-pressure` the critical `context_pressure` record |
| A verification report is retried, whatever its outcome or reason category except `tooling` and `block-bypass` | `developer-retry` | A human decides the retry; it spends one `retry_policy.max_developer_retries` | Dispatch ID and `report_sha256` of the verification report. A verification report is never re-run on the same SHA, except by `tooling-retry` or `bypass-rerun` |
| A conflict-resolver report is retried and its `resolver.cause` is not `task-defect` | `same-candidate-rerun` | A human decides the retry; the new resolver dispatch is a fix on the same target and is bounded by `retry_policy.max_developer_retries` | Dispatch ID and `report_sha256` of the resolver report and its `resolver` block |
| A finding or a warning/blocker severity, a failed check, a moved candidate, a `code`, `requirements`, `candidate-change` or `unknown` reason, a contradictory reason, or `--retry-role developer` | `developer-retry` | A human decides the retry; it spends one `retry_policy.max_developer_retries` | Dispatch ID, `report_sha256`, and the axis, check or candidate change that decided it |
| A hook, the safety classifier or the ledger blocked a legitimate command: a `blocked` report carries `tooling_blocker` (tool, exact command, message), with no finding, no failed check and an unchanged candidate | `tooling-retry` | A human decides the retry after confirming the false positive and filing a bug ticket against the tool; the new dispatch of the same stage (same SHA; a developer continues its last commit) is approved under `approval_policy`; no `retry_policy.max_developer_retries` is spent | Dispatch ID, `report_sha256`, the `tooling_blocker`, and the `candidate_commit` (for a developer, its last commit) |
| A code-review, qa or verification role worked around a hook or tool block (another command form, tool, script file, `eval`, interpreter or a split command): the approver names `block-bypass`, and the candidate is unchanged | `bypass-rerun` | A human decides the retry with a `--note` naming the violation and never accepts or warning-overrides the report; the new dispatch of the same stage on the same SHA always needs an explicit approval (`--approved-by`), under every `approval_policy`; no `retry_policy.max_developer_retries` is spent | Dispatch ID and `report_sha256` (evidence of the violation only, never of its findings or checks), the `--note`, and the unchanged `candidate_commit` |
| The coordinator finds a defect in a clean developer report whose Definition of Done is met inside its allowed paths | `carry-over` | The approver of the `accept` (`batch decide --findings-file`); after a policy auto-accept the coordinator itself (`batch carry-over`, `policy:carry-over`) while no code-review dispatch exists for the candidate; the code-review dispatch is approved under `approval_policy` | Dispatch ID and `report_sha256` of the accepted developer report, the candidate, and the `carried_items` item IDs. No developer retry is spent before review |
| `batch decide --decision abandon` on any completion report | `abandon` | A human only, with a non-empty `--reason`; never a policy | Dispatch ID, `report_sha256` and `abandoned.last_accepted` |
| `report submit` recorded the report but its policy chain stopped (`completion.failed_step`: `policy-decide`, `risk-assess` or `next-dispatch`) | `report-completion` | No human approval: the coordinator runs `report complete` itself; it replays only the `auto_accept_policy` decision recorded at submit, and a step that needs a human stops with that step's remedy | Dispatch ID, `report_sha256`, the submit `completion` object and the `report complete` steps |
| Ledger busy: `ledger is locked by another operation`, or a `ledger_busy` answer from `dispatch status` | `report-completion` | No approval: repeat `dispatch wait`/`dispatch status`, run `report complete` when a recorded report's chain stopped, and never remove the lock by hand; a lock that stays held goes to `ledger release-lock`, which refuses a live owner | Lock owner (`pid`, `host`, `acquired_at`, `held_seconds`) and the `ledger release-lock` verdict |

`batch decision-packet` shows the route before the decision is recorded: its `route_preview` holds
the `retry` routing record computed as `batch decide` computes it (it takes the same
`--reason-category` and `--retry-role` flags) and the `abandon` route; it writes nothing. The preview
does not check `retry_policy.max_developer_retries`: once that budget is exhausted it still shows a
`developer-retry` route, which `batch decide --decision retry` then refuses. When the retry route
cannot be computed, for example because the configured retry-reason classifier extension fails, the
packet still renders and `route_preview.retry` is `{"route": null, "refused": ..., "remedy": ...}`
with the error `batch decide --decision retry` refuses with. With `--findings-file`,
`route_preview["carry-over"]` holds the carry-over record that `batch decide --findings-file` on the
pending developer report, or else `batch carry-over`, would record, or the same
`{"route": null, "refused": ..., "remedy": ...}` refusal. Every `batch decide`
decision stores a `decision` detail on its batch transition audit record: the `route` (`null` for a
decision that routes nothing), the `evidence` (`dispatch_id`, `report`, `report_sha256`) and the
`approver` (`{"kind": "policy" | "human", "name": ...}`, set by the path that approved it). A route outside
`RECOVERY_ROUTES` is refused when it is written and when a batch is read back.

A defect the coordinator finds in a clean developer report is a coordinator finding, not a reason
for a retry before review: retrying a developer report without accepting it is an exception allowed
only for an unmet Definition of Done item or a change outside the declared allowed paths. A finding file is
`{"findings": [{"summary", "files", "expected_evidence"}, ...]}` in English. The batch records each
finding append-only and hash-checked in `carried_items`; every later code-review brief carries the
open ones, and an open finding sends the candidate to code-review even when risk assessment matched
no trigger. A finding is settled once a code-review whose brief carried it is accepted or
warning-overridden; a retried review leaves it open, and the developer-retry that follows carries it
together with the review's findings, so `retry_policy.max_developer_retries` is spent once.
`batch carry-over` refuses once a dispatch other than a cancelled or abandoned one follows the
accepted developer report, and names it: an unsent one is cancelled with `dispatch cancel` first, a
sent one leaves the defect to the decision on its report, and one whose report is already decided
takes the finding through `batch decide --findings-file` when the next developer report is accepted.

A later recovery route adds its `RECOVERY_ROUTES` value and its row here in the same change.

## Developer-retry handoff

A developer retry starts a new worker session from a compact handoff; it never resumes the previous
developer session or replays its history. The handoff has the shape of a checkpoint, structured
evidence without chat history or logs of failed attempts:

- `developer_report`: the last developer work report, accepted or returned with `retry`;
- `retry_decision`: the retried dispatch, its role, `route`, `reason_category`, rationale and note,
  and every review finding with its axis;
- `commit_plan`: the plan that developer brief carried;
- `context_package_id`: the batch's latest registered Context Package, or the no-package sentinel.

`dispatch preflight` for a developer retry returns this handoff as `retry_start` and estimates the
retry's starting context (the preview brief, the Context Package and the handoff) with the same
byte-based token estimate a Context Package uses. The smart-zone threshold is
`adaptive_continuation_policy.context_warn_ratio × context_limit`. Above it, the preflight compacts:

- the handoff keeps only structured fields: the report's dispatch ID, outcome, commit, changed
  files and `commit_map`; the decision's dispatch, role, route and reason category; each finding's
  axis, severity and summary, without its quoted evidence;
- the starting files narrow to the files a finding names or the developer report lists in
  `changed_files`;
- a large document stays only as its `sections` index.

`retry_start.context_estimate` records the threshold and the estimates before and after the compact,
and the preflight's decision packet repeats it as `retry_context_estimate` with
`retry_context_warning`. The preflight writes nothing and never blocks the dispatch: when the compact
cannot reach the threshold, the warning names both numbers and the dispatch proceeds. Read-only roles
(code-review, QA) always start new independent sessions.

## Approvals bound to the transition digest

Before any dispatch exists, `dispatch propose` renders the canonical transition and its
`transition_digest` (SHA-256 of: batch ID, previous dispatch ID and role, reason category, next
role/action and purpose, candidate SHA, base SHA, review scope, verification commands, Context
Package ID and required gates) and writes no brief. It registers the shared Context Package the brief
would pin, so the package ID is part of what the human sees. `dispatch create` with an explicit
approval must pass that digest as `--transition-digest`; the coordinator recomputes the transition
from the ledger and refuses on any difference, so a changed scope, candidate, role, verification
command, reason category or Context Package needs a new proposal and a new approval. The digest is
stored in the approval and in the immutable brief, together with the transition itself, and ledger
validation re-derives it. A policy approval (`milestone`, `low_risk`) is derived from the transition
being created and binds to its own digest.

With `approval_ttl_seconds` set, an `--approved-at` older than that (or dated in the future) is
rejected. A denied terminal confirmation (`human_approval_gate: "tty"`) or an expired approval fails
closed: the coordinator never repeats the call and never falls back to an older approval.

## Read-only retry idempotency

Every `architect`, `code-review`, `qa` and `publish` brief records
`retry_idempotency_key = sha256(role + candidate SHA + base SHA + review scope + reason category +
verification-command digest)`. Creating a dispatch is refused while any active (not decided,
cancelled or abandoned) dispatch of an open batch carries the same key. A completed retry never
blocks a new dispatch on the same candidate, which always receives a new immutable ID; a changed
candidate always yields a different key, and a re-run never edits an earlier brief or report.

## Context pressure

`dispatch context-pressure` records `observed_tokens`, `context_limit`, `warning_threshold`, `level`
(`ok`, `warning`, `critical`) and `recorded_at` for one dispatch. The count must come from the
provider or runtime (`--source probe|provider-usage|runtime-adapter`, or a configured
`context_telemetry_provider`); a model's self-report is rejected. The limit and warning ratio are the
values frozen into the brief. The record is observation only: it never changes `next_action`, starts
a retry or revokes an approval. At `critical` it states the worker's obligation: a write role
checkpoints at the next green TDD boundary or returns a structured blocker; a read-only role returns
the blocker. A continuation exists only from a checkpoint, in a new session that must attest its
model again. A `context-pressure` retry needs a critical record for the reported dispatch.

## Attention state

The coordinator sets `needs_attention` (with `attention_reason`, `attention_since`,
`last_safe_action`, `recommended_human_action`) when: a retry has waited longer than
`attention_policy.retry_queue_seconds`; operational retries of one candidate exceed
`max_infrastructure_retries`; a third consecutive `tooling-retry` on one candidate is decided
(`tooling-retry-repeated`); a retry's reason is `unknown`; a dispatch's pinned Context Package no
longer matches the batch base or accepted candidate; or a live dispatch is silent past
`stale_dispatch_seconds`. It is evaluated by `batch attention check`, by `batch decide --decision
retry` and by `dispatch wait`. While it is set no next dispatch is created; nothing is deleted and
the candidate is not touched. `batch attention resolve` (approval and note required) acknowledges the
open findings, so the same occurrence is not raised again.

## Pluggable operational interfaces

The core keeps lifecycle transitions, the ledger, routing, approval validation, the
candidate/base/Context Package invariants and idempotency validation. Transport health, verification
environment health, the retry reason classifier, the context telemetry provider and the
human-notification adapter are interfaces (`extensions.py`) selected under `extensions` in
`.harness/orchestration.json`; each defaults to an inert `none`. A classifier only proposes a category
that still passes the routing rules above. A failing notification adapter is recorded and never blocks
the attention state. None of them adds a model tool or edits a prompt, and the selected names, the
attention thresholds and the approval TTL are frozen in each brief as `orchestration_policy`.

These batch fields and the four brief fields (`transition`, `transition_digest`,
`retry_idempotency_key`, `orchestration_policy`, all present or all absent) are optional; a record
without them needs no ledger migration.

A routing record's `route` and the `decision` detail of a batch transition audit record are optional
under ledger version 3 in the same way. A decision recorded before them is read verbatim and stays
valid; `ledger migrate` neither adds nor derives a route for it, and only a new `batch decide`
decision records one. A recorded `route` outside the closed route set is rejected on read-back.

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

Every coordinator command holds the exclusive ledger lock, which records its owner (`pid`, `host`,
`acquired_at`). A busy lock fails a write command with a remedy to repeat it; `dispatch wait` and
`dispatch status` keep polling instead (`dispatch status` answers `ledger_busy`). A lock that stays
held is released only by `coordinator.py --repo . ledger release-lock`, which refuses a lock whose
owner process is alive or runs on another host, releases a dead owner's lock, releases a lock
without an owner record only once it is stale, and never releases a lock whose owner record cannot
be read; concurrent releases are serialised. Nobody removes the lock or any state file by hand.

When a new fact appears after dispatch, the coordinator appends a new coordinator decision before
acting on it. The decision records the dispatch ID, fact and evidence, impact on scope or risk,
chosen action, and author/time. The original brief remains immutable. If the fact changes the
scope, DoD, assignment, or required proof, the current dispatch is ended and the changed
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
transport resolves to `in-process`; `external` must be selected explicitly before an external worker can
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
- `allowed tools` and `context budget`: the role's working tool set and its token budget, both chosen
  from the project's `.harness/orchestration.json`. `allowed_tools` defaults from the role manifest's
  mode (read-only roles get no edit tools) and a project may override it with `tool_policy`;
  `context_budget` is `adaptive_continuation_policy.context_limit`. The tool list is the role's
  working set, never a deny-list: a brief does not disable the runtime's global tools. A brief
  without these fields is valid;
- `allowed paths`: the explicit scope the role may write, pinned from the batch and never wider than
  the role's write ceiling; a change outside it is rejected;
- `branch/worktree`: issue branch and isolated worktree; protected branches and `integration/*`
  are never write targets;
- `Definition of Done`: observable acceptance criteria and the expected role output;
- `prohibited changes`: paths, operations, or decisions outside the declared scope;
- `verification commands`: exact project commands and any mandatory risk-review gate. A developer
  work dispatch may use focused developer commands; clean-room QA always uses the full verification
  commands;
- `dependencies and assumptions`: known blockers, required inputs, and their owner;
- `coordinator approval`: approving person, timestamp, and the approved concurrency decision;
- `commit plan` (developer work): ordered entries `{id, summary, expected_paths, covers}`, where
  `covers` names the Definition of Done items an entry implements. It is one entry per item unless
  the operator pinned the architect's plan with `batch decide --decision accept --commit-plan-file`;
- `commit plan divergence` (code-review): how the last accepted initial or rebase developer report
  diverged from its plan, or `null`;
- `carried items`: one shared channel keyed by the kind of source,
  `{"coordinator-finding": [...], "review-finding": [...]}`, each item
  `{item_id, source, summary, files, expected_evidence}`, or `{}`. A code-review or developer work
  brief carries every open coordinator finding; a developer brief answering a retried code-review
  also carries that review's Standards and Spec findings. A non-empty section is bound into the
  transition as `carried_items_sha256`.

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
- Allowed tools: <role working set from tool_policy or the mode default>
- Context budget: <tokens from adaptive_continuation_policy.context_limit>
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
- the next coordinator action, including the required independent gate when applicable;
- for a developer brief with a `commit_plan`: a `commit_map` of `{commit_sha, plan_entry_id}` pairs
  covering every created commit. An initial or rebase report may map one commit to several entries
  and one entry to several commits; when that mapping is not one-to-one it also carries
  `dod_coverage` (one record per Definition of Done item, either its covering commits or
  `not_covered` with a reason) and a `divergence_justification` naming what was merged, split or
  added and why. A developer-retry report maps each new commit to one distinct entry and carries
  neither field;
- for a code-review brief with carried items: `review.carried_items`, one
  `{item_id, status: closed | open | unverified, evidence}` per item the brief carried. An omitted,
  `unverified` or `open` item is a carried gap: the report is never clean, no policy accepts it, plain
  `accept` is refused, and only `override-warning` with a note other than `none` (recorded as
  `carried_items_gap`) or `retry` decides it. An `open` item is `code` evidence for the retry route;
- for a role a tool blocked: `outcome: blocked` and `tooling_blocker`, exactly the non-empty strings
  `tool`, `command` (as invoked) and `message` (verbatim), each at most 1600 characters. It is valid
  only on a `blocked` report and is the only evidence of the `tooling` reason category.

Optional `lessons` and `used_memory` are lists of non-empty strings; empty lists and omission are
valid. `lessons` records historical observations, never confirmed truth: memory indexes them only
with explicit `completion_report` source/path authorization, after sanitization, with status
“не подтверждено человеком” and the ordinary FTS ranking. `used_memory` lists the identifiers of
used hits as a weak signal: the coordinator neither resolves them nor uses them as an acceptance
gate, and memory never indexes them. These fields do not change required proof or ledger version.

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
- Lessons (optional, не подтверждено человеком): <historical observations, no extra weight>
- Used memory (optional, weak signal, not a gate): <used hit identifiers>
```

In JSON, represent the optional fields as `"lessons": ["<observation>"]` and
`"used_memory": ["<hit identifier>"]`; omit them when there is nothing to record.

Report submission rejects structural commit-plan errors, each with a remedy, such as an
unmapped created commit, an unknown plan entry, an uncovered item without a reason, or a divergence
without a justification. It also checks `dod_coverage` against `commit_map` and the plan's `covers`:
an item claimed by commits none of which `commit_map` maps to an entry covering that item is
refused, and the remedy names the item, the claimed commits, and the entries that cover it. A
`not_covered` item that the mapping formally covers stays valid and needs a manual decision like any
other `not_covered` item. A justified divergence with full coverage does not by itself make a report
unclean, and the decision records it as `commit_plan_divergence`. Any `not_covered` item is never
clean: no policy accepts it automatically, plain `accept` is refused, and the report can be
accepted only by `override-warning` with a note other than `none`, or returned with `retry`.

The coordinator does not rewrite a report to make it pass. A missing commit SHA, changed-file
list, check result, risk statement, or blocker statement is a proof gap and keeps the batch from
being marked `completed`.

## Parallel work and quality gates

Parallelism is allowed only for independent work that the coordinator has approved:

- separate batches may run concurrently, each in its own issue branch and worktree, even when their
  allowed paths overlap, as long as the project `concurrency_budget` allows it; overlapping files
  are reconciled at integration, not by delaying a start. A second batch for the same unfinished
  ticket, branch, or worktree is rejected;
- read-only work may run in parallel when it has no overlapping write operation or contradictory
  brief;
- role handoffs inside one batch are sequential, and there is one active writer at a time;
- multiple roles must never write to the same batch simultaneously, even if their paths appear
  different;
- heavy integration and verification runs use one serialized quality-gate lane; independent
  implementation work may continue while it waits, but concurrent heavy gates are not started;
- an exhausted budget, unclear boundary, or unavailable lane is escalated as a blocker rather than
  resolved by overlapping writes or an unapproved retry.

Before dispatch, the coordinator records the active batches against the budget, the writer, and the
quality gate lane position. After each handoff, the incoming role receives the prior completion report as
evidence but still receives its own immutable brief.

## Integration accounting

A `completed` batch is history and is never reopened or rewritten. After an accepted publish the
coordinator records the link a later integration step needs in a separate immutable Integration
record, through the public `integration` command group; none of its commands writes a batch, plan,
dispatch or report, or changes Git:

- `integration prepare --ticket T --branch B [--batch ID] [--candidate-commit SHA]` links ticket,
  issue branch, source batch, published candidate SHA and the integration (target) SHA, together with
  the accepted green QA evidence of that candidate. It selects the one completed batch with an
  accepted publish and never substitutes another: several candidates need an explicit `--batch`.
  The published SHA is confirmed by the accepted publish report and by the remote branch. It is
  idempotent: repeating it neither loses nor duplicates the record. Every refusal states a remedy,
  including an unpublished result, a different ticket or branch, a candidate that is not the
  published SHA, and an integration ref that already moved while no record exists.
- `integration status` is a read-only observation: `current`, `stale` (the integration ref moved;
  `refresh_required`) or `unavailable`. A stale record never lets the old QA stand in for a new
  candidate/target pair, and observing it never creates a dispatch. After a refresh it reports the
  current pair and `verification`: CI or local-QA of that pair is required (resolver evidence does
  not count), and re-review is never required by the refresh alone.
- `integration refresh` is the PR-preparation route when the integration base moved after review,
  QA or publish: those stages keep verifying their pinned candidate, and there is no mandatory
  base-freshness gate or developer restart. With an unchanged target it runs no rebase. Otherwise it
  rebases only the batch's own issue branch onto the exact target SHA and publishes it with
  `--force-with-lease` against the recorded candidate, then writes an immutable refresh record
  linking the old and new candidate, the target and the resulting history. It refuses a dirty
  worktree, a worktree off the issue branch, and a changed remote branch, and never writes a
  protected or `integration/*` branch. A clean rebase does not invoke the resolver or spend its two
  cycles; a textual conflict returns `state: conflict` with resolver data and leaves the branch and
  worktree as they were. The old QA stays historical evidence. It does not depend on the
  developer-retry rebase route of ADR 0012, which still handles conflicts and developer work.
- `integration link-evidence --kind ci|local-qa|resolver` is the only way later CI, local-QA and
  resolver results are attached. Each is its own immutable record with its own candidate/target pair
  and `verification: unverified`; the record's initial evidence keeps its original pair.
- `integration resolve` hands a textual conflict between the candidate and the moved target to a
  `conflict-resolver` (issue #534). It writes nothing to Git: it refuses a clean rebase (that is
  `integration refresh`), creates a new resolver batch (kind `resolver`) beside the finished batch
  of the ticket, and sets `next_action: resolve-conflict`. After the regular `batch approve` and
  `dispatch create --role conflict-resolver`, the brief carries an immutable `resolver` section: the
  ticket, `sides.candidate` and `sides.target` requirements (the target side comes from `(#N)` in the
  commit subjects and their plan records, otherwise from the commit subject and body), the
  candidate and target SHA, the scope, the prohibitions, the commit plan, the checks, the remaining
  cycle budget and the `report_staging_path`. Two automatic target SHAs are allowed; a third needs a
  human decision with `--extends-budget`. A cycle is spent only by a recorded resolver report on a new
  target SHA: a clean rebase, a human answer and a fix on the same target spend none, and fixes on
  one target are bounded by `retry_policy.max_developer_retries`. The budget is derived from the
  append-only `reports/resolver-events/` (`cycle-spent`, `same-target-fix`, `human-decision`,
  `scope-change`, `exhausted`), so a lost session or a resume never resets it.
- A resolver that finds incompatible requirements writes a checkpoint whose `blockers` name the
  concrete incompatibility and the options. The human answer is recorded with `integration
  resolver-event --kind human-decision` before `dispatch resume --trigger human-decision`, and the
  same dispatch continues: no new developer starts and neighbouring batches keep running. A scope
  change is a regular newly approved dispatch (`--kind scope-change` only records it); the original
  brief is never rewritten. After the budget is exhausted only this task stops and the branch and
  evidence stay: an integration incompatibility continues with the same resolver, the ticket's own
  defect (`resolver.cause: task-defect`) returns to a regular developer.
- A conflict-resolver report carries a top-level `resolver` block (preserved requirements of both
  sides, human-decision event ids, target and resolved SHA, cause, exact changed files, commits mapped
  to the plan entry). An accepted resolution takes the narrow route: no repeat code-review, but QA and
  CI or local-QA of the new candidate/target pair are still required.

Records live under `reports/integration/`, `reports/integration-evidence/`, `reports/resolver/` and
`reports/resolver-events/` in the existing `reports` directory, so no ledger schema change or
migration is involved.

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
  write role's declared allowed paths, read from the immutable dispatch and risk-assessment records;
- `QA failure rate`: the share of a batch's decided QA dispatches whose coordinator decision was not
  `accept`, from the coordinator's recorded decision rather than a QA role's own outcome claim.

The baseline is evidence for later targets, not a hidden limit. It must not prescribe a provider,
model, external runtime behavior, or hard-coded concurrency or token number.

Use the accompanying [pilot guide](pilot.md) to record the first observation period with the same
counting rules and missing-data treatment across batches.
