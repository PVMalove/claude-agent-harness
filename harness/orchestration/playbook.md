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

## Configuration validation and pinned evidence

Runtime configuration passes through `normalization.v1` in memory before strict canonical
validation. Only the enum fields listed in `config_boundary.NORMALIZATION_FIELDS` support
unambiguous trimming and case folding. Documented aliases are `manual-all` → `manual_all` and
`low-risk` → `low_risk` for `approval_policy`, and `in_process` → `in-process` for assignment
transport. Each applied rule emits a structured warning with its field ID and version. IDs,
models, paths, commands, extensions, ledger records, reports and approvals are never normalized.
Explicit nulls, duplicate JSON keys, nonfinite numbers, secrets and ambiguous values are errors.
Fuzzy spelling suggestions are diagnostics only.

Inspect a configuration without writing it:

```bash
python .harness/orchestration/coordinator.py config validate --repo . --json
python .harness/orchestration/coordinator.py config validate --repo . --canonical --json
python .harness/orchestration/coordinator.py config validate --repo . --fix --dry-run --json
python .harness/orchestration/coordinator.py config validate --repo . --fix --json
```

`--canonical` rejects spelling that requires normalization. `--fix` writes only a valid authored
configuration through the shared transaction store, with expected-byte concurrency protection
and a backup; `--dry-run` previews the diff. Headless validation never waits for input and returns
exit code 2 for an invalid configuration. Runtime loading never writes the configuration file.

Each new dispatch binds a complete canonical config, project and role snapshot to its approval
digest, with separate source and canonical hashes. Checkpoints and manual or clean-room QA reports
use that snapshot. Invalid or changed live configuration blocks a subsequent transition while
accepted evidence remains saved. Missing or corrupt snapshots allow only unaccepted evidence
capture with a remedy; they never reconstruct approval from current files. Runtime state-store
reconciliation and recovery UI are separate follow-up work.

## Lifecycle

The coordinator records exactly one current state for each batch. A role may report progress or a
blocker, but it cannot transition its own batch or silently widen its brief. The installed
`coordinator.py` implements this contract's lifecycle and keeps its local state under
the gitignored `.harness/orchestration/state/` directory.

`BatchState` and `DispatchState` define separate, closed English vocabularies. The ledger and
coordinator validate states on read and before writing either a record or its audit event. Unknown
values, translations, whitespace and case variants are refused. The enum checks the vocabulary;
the transition table and approval guards still determine whether a transition is allowed.

| State | Coordinator action and entry condition | Allowed next state |
| --- | --- | --- |
| `planned` | Immutable scope drafted; initial planning approval is still required. | `awaiting-approval`, `blocked`, `failed`, `paused`, `completed` (no implementation), `abandoned`; rewind preserves `planned` |
| `awaiting-approval` | A fresh dispatch or a report decision awaits approval. | `active`, `blocked`, `failed`, `paused`, `completed`, `abandoned` |
| `active` | An approved dispatch is executing its immutable contract. | `awaiting-approval`, `blocked`, `failed`, `paused`, `completed` (no implementation), `abandoned`; human rewind |
| `paused` | An explicit `batch auto-decide` recorded a stop. Observation alone never pauses. | Human `resume-stop` or `rewind`, or `abandoned` |
| `blocked` | A dependency, authority or proof is missing. It retains its work. | Human recovery, `failed`, `paused`, `abandoned`, `completed` (no implementation) |
| `failed` | Work could not produce an acceptable result. It remains recoverable. | Human recovery, `paused`, `abandoned`, `completed` (no implementation) |
| `completed` | Accepted publish, or explicit `batch not-required` with `completion_kind: no-implementation`. | terminal |
| `abandoned` | Explicit human refusal with reason and `last_accepted`, through either abandon entry point. | terminal |
| `not-required` | Legacy no-implementation terminal record; new commands write `completed`. | terminal |

A legacy `failed` record with an explicit human `abandoned` decision is also terminal. It can be
superseded without rewriting the source; an arbitrary failed batch cannot. Recoverable blocked,
failed and paused batches retain their ticket, branch, worktree and concurrency slot.

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

**Failure domains.** Every retry routing record also carries `failure`: exactly one ADR 0019 domain
(`environment_preparation`, `infrastructure`, `policy`, `tooling`, `code_test`, `timeout`,
`cancelled`, `unknown`) with its evidence (`domain`, `method`, `matched`, `source`). The `method` is
`structured` (a field of the report, a stage or the runtime), `heuristic` (a text pattern that never
proves `code_test`), `approver` (the human or policy named it) or `fallback`, which is always the
domain `unknown`. A review finding or a warning/blocker severity, an explicit reviewer retry, a
failed check, an open carried item, a confirmed project defect in QA preparation, a conflict-resolver
report with `resolver.cause` `task-defect` and a moved candidate are `code_test`. A moved candidate
is `code_test` by decision: the ledger proves that the work itself changed, so the route stays a
developer retry that spends the budget, as before failure domains existed. A hint of the
`retry_reason_classifier` plugin is no proof: it never replaces a structured fact of the report,
and a hint alone leaves the domain `unknown`. A structured `tooling_blocker` is `tooling`, a confirmed QA environment failure is
`environment_preparation`, a recorded critical `context_pressure` is `infrastructure`, and
`block-bypass` is `policy`. Whatever nothing structured proves is `unknown`, never `code_test`.
Only a `code_test` retry spends `retry_policy.max_developer_retries`: an `infrastructure`,
`environment_preparation`, `policy`, `tooling`, `timeout`, `cancelled` or `unknown` retry never does.
A code-review, qa, verification or publish report whose domain is not `code_test` is refused a
developer retry: `batch decide --decision retry` answers with the failure and its remedy and writes
nothing (under `approval_policy: auto` it is the stop `no-automatic-route`/`unknown-reason`, whose
evidence keeps the `failure`; the same stop ends an automatic developer retry whose domain is
`unknown`, because that retry spends no budget that would bound it). The
approver proves a code defect by `--reason-category code`, `requirements` or `candidate-change`, or
takes the developer retry by `--retry-role developer`; either is `approver` evidence and spends the
budget. A decision recorded before failure domains existed has no `failure` and counts as before.

| Reporting stage | `accept` | `retry` | `block` / `fail` | `abandon` |
| --- | --- | --- | --- | --- |
| architect | developer | new architect | terminal | `abandoned` |
| developer | risk assessment | `verification` on the registered candidate when the report is `blocked` with an operational reason; otherwise `developer-retry` (new candidate, then a new risk assessment; it spends the budget only for a `code_test` domain) | terminal | `abandoned` |
| verification | risk assessment | `developer-retry` for a `code_test` domain; otherwise refused | terminal | `abandoned` |
| code-review | qa | new code-review on the same candidate only if the report is `blocked`, the reason is `verification-infrastructure`, `transport` or `context-pressure`, there is no finding on either axis, no failed check and the candidate is unchanged; otherwise `developer-retry` for a `code_test` domain, else refused | terminal | `abandoned` |
| qa | publish | new qa on the same candidate under the same conditions (QA stays read-only); a defect or a new candidate means `developer-retry`; a preparation failure follows the QA preparation rules below; a cause that is not `code_test` is refused a developer | terminal | `abandoned` |
| publish | completed | new publish on the same accepted SHA for `verification-infrastructure`, `transport` or `context-pressure`; `developer-retry` when the candidate must change for a `code_test` domain, else refused | terminal | `abandoned` |

**QA preparation failures.** When the project declares `qa_preparation`, the QA report carries `qa_stages`
(per-stage command, result, exit code, sanitised diagnostics, `failed_stage`, `code_checks_started`
and, for a failed preparation, a `diagnosis`). A failed preparation stops the run before any gate
command: the gate commands are recorded as `not_run`, never as failed checks. Only after that failure
the runner runs the project's `qa_environment_probes` and `qa_project_file_checks` in the same
checkout, as independent facts recorded as `environment-probe` and `project-file-check` stages.
Neither an exit code nor a log keyword decides a category alone, and the preparation stage alone
proves no infrastructure cause; log signatures are only corroboration.

- `infrastructure` (confirmed by a failed environment probe while every project-file check passes,
  no code check started): the report is `blocked` and says the code was not verified. A `retry` needs no `--reason-category`: it routes `same-candidate-rerun` with
  `verification-infrastructure`, a new qa dispatch on the same SHA, and spends no
  `retry_policy.max_developer_retries`. The coordinator decides it by hand once the environment is ready.
  The stages decide, not the runner's category: the routing record's `failure` is
  `environment_preparation` (method `structured`, source `qa_stages`) whenever the probe failed, every
  project-file check passed and no code check started, even when the log names no cause the runner
  knows and its `diagnosis.category` is `unknown`. An offline `uv sync` (`No solution found when
  resolving dependencies`, `Packages were unavailable because the network was disabled`) is exactly
  that case. The same log with a failing offline lock check, or with no probe and no project-file
  check, is not: the first is a `project-defect` (`code_test`), the second needs triage (`unknown`).
  The runner's own `diagnosis.category` stays in the report unchanged, so it can differ from the
  recorded domain.
- `project-defect` (a failing project-file check, or a defect log signature such as an incompatible
  lock file that agrees with a tracked project file the output names): the report is `failed`; a `retry` routes `developer-retry` with `code`.
- `unknown` (no probe or project-file check configured, contradicting facts, or no confirmation):
  the report is `blocked` and needs coordinator triage. `retry` without
  `--reason-category` is refused and nothing is retried; the coordinator names the cause
  (`verification-infrastructure` for a same-SHA rerun, or a developer category) or blocks the batch.
- A failing gate stage is an ordinary code-check failure: findings kept, route `developer-retry`.

`code`, `requirements` and `candidate-change` route to `developer-retry`; only the three operational
categories, besides `tooling` and `block-bypass` below, may re-run a read-only stage on the same SHA,
and only with empty findings, an unchanged candidate and no scope or requirement blocker. An
`unknown` reason, and a contradictory or unsupported one, never proves a code defect: a developer
stage re-runs `developer-retry` without spending the budget, and a code-review, qa, verification or
publish stage is refused (see "Failure domains").

A retry that routes to `developer-retry` records, as `retry_item_ids`, the closed list of carried
items its developer brief will carry, possibly empty. For a retried code-review report, the list
holds the open coordinator findings, that review's Standards and Spec findings, and the open
incomplete items handed to the developer; for a retried qa, publish or verification report, the same
without review findings. For a retried developer work report, it holds its own brief's items, none
of which was accepted. A non-empty list records the route `fix-forward`. A fix-forward is still a
developer-retry: `next_action` is `developer-retry` and, when its failure domain is `code_test`, it
spends one `retry_policy.max_developer_retries`. The developer adds new commits on top of the brief's
`snapshot_commit` and rewrites none of them. A developer `tooling-retry` keeps its own route and
records the same list.

A `developer-retry` or `fix-forward` route also checks the integration base: `batch decide` and
`batch decision-packet` fetch `origin/<integration_ref>`. When its tip has moved past the pinned
`integration_base_commit` and the candidate the retry continues (a retried developer report's own,
else the latest accepted one) does not contain it yet, the route is `rebase-fix-forward`: the
routing record adds `rebase_target_commit` (that tip) and `integration_base_commit` (the base it
moved from), and keeps `retry_item_ids`. It is still a developer-retry: `next_action` is
`developer-retry` and, when its failure domain is `code_test`, it spends one
`retry_policy.max_developer_retries`. The decision only proposes
the target. The developer-retry brief carries it as `rebase_target_commit`, bound into the
transition as `rebase_target_sha`, and that dispatch always needs an explicit approval with the
transition digest, under every `approval_policy` except `auto`; no policy except `auto` approves a
rebase target (see "Automatic path"). The developer
rebases the candidate onto exactly that target and fixes on top of it in the same dispatch. An
accept of its report, or of a later retry whose candidate sits on that target, pins
`integration_base_commit` to the target. A fetch
error refuses the retry with a remedy; a `tooling-retry` and a candidate that already contains the
tip propose no target.

A batch created on an issue branch that already holds commits of an earlier batch of the ticket
(its `branch_start_commit` is a strict descendant of `integration_base_commit`) measures its own
changes from `branch_start_commit`: the `changed_files` and `commit_map` of its developer and publish
reports, their `scope_warnings`, a checkpoint and `risk assess` (`--base-commit`, `review_scope`).
A superseding batch measures from `integration_base_commit`, as does a batch whose start is not a
descendant of it (after an accepted rebase, or a resolver batch). A brief and a Context Package
(`--base-commit`) stay on `integration_base_commit`. The rule does not tell accepted commits from
unreviewed ones: commits an abandoned batch left on the branch are not reviewed by the new batch.

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
Context Package freshness, and, by default, needs its own explicit human approval under
`manual_all`. The earlier brief, report and blocker stay untouched as audit evidence. A retry never
uses an empty or fictitious commit, a changed candidate always needs a new risk assessment before
review or QA, and `block` or `fail` never start a retry by themselves. `--retry-role developer`
forces a developer retry where a same-candidate re-run would otherwise be routed.

A separate project opt-in, `infrastructure_retry_policy: {"enabled": true}`, permits only a
confirmed infrastructure retry. It preserves the role, operation, candidate SHA, commands, access,
runtime/model/effort and scope. The opt-in, preparation/probe/file-check commands and
`attention_policy.max_infrastructure_retries` (default 2, including explicit 0) are snapshot into
the approval-bound brief. Historical briefs stay manual; live configuration cannot expand this
approval. The policy spends no developer retry budget and never grants native permissions.

For a blocked QA preparation report, both its independent diagnosis and fresh readiness checks
must confirm infrastructure and the same candidate. `report complete --dispatch <id>` resumes the
policy decision and the new dispatch after recovery; it returns the existing successor on replay.
For an unsent QA/publish operation, `dispatch retry-infrastructure --dispatch <id>` requires the
recorded probe-confirmed denial and fresh readiness, then cancels the unsent brief and creates a
new dispatch. Each decision records `policy:infrastructure-retry` and preserves previous evidence.
An unchanged denial does not launch work. Unknown causes, changed boundaries or exhausted budgets
stop the policy and raise attention. Unsupported/unverified access and arbitrary Git failures
require manual recovery. Git operations without an approved dispatch stay manual. An explicitly
selected native access mode needs actual runtime-access proof; health and checkout attestation do
not supply it. See the project guide for the smoke procedure, the verified native support row and
the remaining unverified runtime matrix.

`block-bypass` means a read-only role (code-review, qa or verification) worked around a hook or tool
block instead of stopping with `tooling_blocker`. Only an approver names it, and none of that report
is evidence: its findings, failed checks and outcome do not route the retry, and only a moved
candidate still sends it to `developer-retry`. Its route is `bypass-rerun`: a new dispatch of the
same stage on the same SHA (verification on its registered candidate) with no new candidate commit.
`batch decide` requires a `--note` naming the violation, the report is never accepted or
warning-overridden, and the re-run dispatch always needs an explicit approval, under every
`approval_policy` except `auto`. A `bypass-rerun` spends no `retry_policy.max_developer_retries`. `block-bypass`
is refused for an architect, developer or publish report, which is still retried with a developer
reason category (`code`, `requirements`, `candidate-change`) or blocked.

A read-only report (architect, verification, code-review or qa) may list `incomplete_items`: brief
items the role left undone, each with its reason and target role. Such a report is never accepted
automatically, and a plain `accept` or `override-warning` of it is refused. The approver takes one
of two decisions. `--decision accept --carry-incomplete` (or `override-warning` with the flag)
records a `carry-over` routing record that names the `carried_item_ids` and is never applied to
`next_action`: the batch moves as on any accept of that stage, and the work brief of each item's
target role carries the item until a dispatch of that role whose brief carried it is accepted or
warning-overridden. An open item for code-review sends the candidate to code-review even when risk
assessment matched no trigger. The flag is refused when an item targets the reporting role itself.
`--decision retry --narrowed` re-runs the same stage on the same SHA with a brief that carries only
the report's items, under the route `narrowed-retry`, or `tooling-retry` (category `tooling`) when
any item carries a `tooling_blocker`. The brief's Definition of Done stays the batch's: the
narrowed scope is the carried items alone. A narrowed retry spends no
`retry_policy.max_developer_retries`; its dispatch is approved under `approval_policy`; it is
refused for a developer or publish report, a report without items, together with
`--reason-category` or `--retry-role developer`, and whenever a finding or a warning/blocker
severity, an open carried item, a failed check or a moved candidate demands another route. A retry
without `--narrowed` ignores the items and routes as above.

A code-review `blocker` can never be accepted. While `retry_policy.max_developer_retries` still
allows a developer retry, it takes `retry` or `abandon`; once that budget is exhausted, `retry` is
refused and the blocker takes `block`, `fail` or `abandon`, after which the work is split or
re-planned in a new batch. A `tooling-retry` neither spends this budget nor is refused by it.

`abandon` is a decision on a completion report, alongside `accept`, `accept-conditional`,
`override-warning`, `retry`, `block` and `fail`. It needs explicit approval and a non-empty
`--reason`, moves the batch to the terminal `abandoned` state and marks unfinished dispatches
`abandoned`. It keeps the worktree, candidate, briefs, reports, Context Packages and audit records,
closes no issue and opens no PR. It removes only leftovers that are not evidence: the staged
copies of reports in the agent inbox and the QA queue entries of dispatches that will never run.
The batch records `abandoned.last_accepted` (the newest accepted stage and candidate), so a fresh
batch can be created on the same branch and candidate. It is never a fallback for `block`, `fail`
or `retry`, and it is never inferred from silence, a missing self-report or a capacity error.
An approved but unsent dispatch has its own cancel path; cancelling that brief is not abandoning the
whole batch.

After a forced abandon, `batch create --supersedes <batch>` with `--approved-by` and
`--approved-at` (a human; a `policy:` approver is refused) plans a superseding batch for the same
ticket and issue branch. It refuses, with a remedy and without writing anything, a source that is
not `abandoned`, one whose `abandoned.last_accepted` is `null`, and another ticket or issue branch.
For a `null` source that itself superseded a batch, the remedy names that batch to supersede again.
The new batch and its immutable plan carry the same `supersedes` link, and its
`coordinator_decisions` hold one `supersede` decision with route `supersede`. With the same
Definition of Done, the abandoned batch's accepted architect is carried by reference together with
its pinned commit plan, and the batch starts at the developer stage; another Definition of Done
carries nothing and runs the architect stage again. The first developer starts at the last accepted
candidate (`start_commit`): as an initial developer when it descends from the integration base
pinned at create, otherwise as a developer-retry with that base as `rebase_target_commit`, which
always needs an explicit approval. A superseding batch abandoned without an accepted developer
records its own `start_commit` as the `abandoned.last_accepted` candidate, so a chain of
superseding batches keeps the accepted candidate. Risk assessments, reviews, QA, carried items
and operator decisions are never copied: they stay with the abandoned batch and are reached through
`supersedes.batch_id`, and risk assessment, review and QA run again on the new candidate. The
`supersede` decision spends no developer retry.

## Recovery route table

Every `retry` and `abandon` decision of `batch decide` records its recovery route as
`routing.route`, one value of the closed set `RECOVERY_ROUTES`. The route is set in the same step
that sets `next_action`, from the same structured evidence, and never from free text. `batch abandon`
(a batch that will never have a report) and `batch resume` record no route. `report-completion` is
not recorded by `batch decide`: `report submit` names it in its `completion` object when the policy
chain after a recorded report stops, and the coordinator completes that chain with
`report complete --dispatch <dispatch-id>`. `supersede` is not recorded by `batch decide` either:
`batch create --supersedes` records it once in the new batch's own `coordinator_decisions`.

`RecoveryRoute` supplies `RECOVERY_ROUTES`, and `RetryReason` supplies the reason categories.
Classifier hints and explicit categories are parsed before routing; recorded categories and routes
are validated on read. `unknown` is an explicit reason, never a fallback for an invalid literal.
These enums preserve the routing guards, candidate binding and retry budgets in this table.
`carry-over` is recorded by an `accept` or
`override-warning` with `--findings-file` and by `batch carry-over`; its routing record names the
`carried_item_ids` and is never applied to `next_action`, which moves through risk assessment as on
any developer accept; `batch carry-over` itself moves a `next_action` of `qa`, set by an earlier
assessment, to `code-review`. An `accept` or `override-warning` of a read-only report with
`--carry-incomplete` records `carry-over` the same way, and `next_action` moves as on any accept of
that stage. The coordinator chooses a route by this table:

| Situation | Route | Who approves | Evidence |
| --- | --- | --- | --- |
| An architect report is retried without `--narrowed`, whatever the reason category except `tooling` | `architect-retry` | A human decides the retry with `batch decide`; the new architect dispatch is approved under `approval_policy` | Dispatch ID and `report_sha256` of the architect report. Not `same-candidate-rerun`: it is not conditioned on a reason category and pins no candidate |
| A developer report is `blocked` with an operational reason, no finding and no failed check | `verification` | A human decides the retry; the read-only verification dispatch is approved under `approval_policy` | Dispatch ID, `report_sha256`, the `candidate_registrations` entry with its `source_report_sha256`, and for `context-pressure` the critical `context_pressure` record |
| A code-review, qa or publish report is `blocked` with an operational reason, no finding on either axis, no failed check and an unchanged candidate | `same-candidate-rerun` | A human decides the retry; the new dispatch on the same SHA needs its own approval under `approval_policy` | Dispatch ID, `report_sha256`, the unchanged `candidate_commit`, and for `context-pressure` the critical `context_pressure` record |
| A qa report is `blocked` because its `qa_stages` show a failed preparation that the stages themselves prove to be the environment (a failed `environment-probe`, every `project-file-check` passed, so the routing record's failure domain is `environment_preparation`, whatever the runner's `diagnosis.category`) and `code_checks_started: not_started` | `same-candidate-rerun` | A human decides the retry once the environment is confirmed ready; the new qa dispatch on the same SHA needs its own approval under `approval_policy`; it spends no `retry_policy.max_developer_retries` | Dispatch ID, `report_sha256`, the report's `qa_stages` with its stage results and diagnosis signals, the unchanged `candidate_commit`; the earlier brief, report and artifact stay as audit evidence |
| A verification report is retried without `--narrowed`, whatever its outcome or reason category except `tooling` and `block-bypass`, and its failure domain is `code_test` (a failed check, or an approver's `--reason-category code`, `requirements` or `candidate-change`, or `--retry-role developer`) | `developer-retry` | A human decides the retry; it spends one `retry_policy.max_developer_retries`. Any other failure domain is refused and spends nothing | Dispatch ID and `report_sha256` of the verification report. A verification report is never re-run on the same SHA, except by `tooling-retry`, `bypass-rerun` or `narrowed-retry` |
| A conflict-resolver report is retried and its `resolver.cause` is not `task-defect` | `same-candidate-rerun` | A human decides the retry; the new resolver dispatch is a fix on the same target and is bounded by `retry_policy.max_developer_retries` | Dispatch ID and `report_sha256` of the resolver report and its `resolver` block |
| A warning/blocker severity, a failed check, a moved candidate, a `code`, `requirements` or `candidate-change` reason, or `--retry-role developer` (the failure domain is `code_test`), and the closed list of carried items is empty: no review finding, no open coordinator finding, no open incomplete item for the developer and no item of a retried developer brief | `developer-retry` | A human decides the retry; it spends one `retry_policy.max_developer_retries`. A developer report with an `unknown` or operational reason also routes `developer-retry` but spends no budget; a read-only report with such a reason is refused | Dispatch ID, `report_sha256`, the axis, check or candidate change that decided it, and an empty `retry_item_ids` |
| An additional commit is needed: a retry routes to a developer-retry whose closed list of carried items is not empty (the retried review's Standards and Spec findings, open coordinator findings, open incomplete items for the developer, or the items of a retried developer brief); a `tooling-retry` keeps its own route and carries the same list | `fix-forward` | A human decides the retry with `batch decide`; the developer-retry dispatch is approved under `approval_policy`; it spends one `retry_policy.max_developer_retries` when its failure domain is `code_test` | Dispatch ID, `report_sha256`, the `retry_item_ids` the brief carries, and the brief's `snapshot_commit` as the candidate the new commits continue |
| The integration base moved ahead: a retry routes to a developer-retry (`developer-retry` or `fix-forward`) of a developer, verification, code-review, qa or publish report while `origin/<integration_ref>` has moved past the pinned `integration_base_commit` and the candidate the retry continues does not contain that tip yet | `rebase-fix-forward` | A human decides the retry with `batch decide`; the developer-retry dispatch that carries the proposed tip as `rebase_target_commit` always needs an explicit approval (`--approved-by` with the transition digest), under every `approval_policy` except `auto`; it spends one `retry_policy.max_developer_retries` when its failure domain is `code_test` | Dispatch ID, `report_sha256`, the routing record's `rebase_target_commit` and `integration_base_commit`, the `retry_item_ids` the brief carries, and the `rebase_check` of the retry report |
| After fix-forward: a code-review is dispatched for the candidate of an accepted `fix-forward` or `rebase-fix-forward` developer-retry that continued a candidate an earlier code-review of the batch judged (the review was accepted, or retried to a developer) | `fix-forward` | No separate decision: `dispatch create --role code-review` without `--delta-review-of` chooses a delta or a full review itself, and the code-review dispatch is approved under `approval_policy` with that choice bound into its transition | The brief's `delta_review_scope`: `prior_review` (dispatch ID, `report_sha256`, reviewed candidate), `developer_dispatch_id`, `delta_base`, `delta_commits`, `reviewed_copies`, `closure` and the `escalations` that make it `full`; the transition's `delta_review_sha256`; the QA dispatch's `candidate_commit`, the new SHA |
| A hook, the safety classifier or the ledger blocked a legitimate command: a `blocked` report carries `tooling_blocker` (tool, exact command, message), with no finding, no failed check and an unchanged candidate | `tooling-retry` | A human decides the retry after confirming the false positive and filing a bug ticket against the tool; the new dispatch of the same stage (same SHA; a developer continues its last commit) is approved under `approval_policy`; no `retry_policy.max_developer_retries` is spent | Dispatch ID, `report_sha256`, the `tooling_blocker`, and the `candidate_commit` (for a developer, its last commit) |
| A code-review, qa or verification role worked around a hook or tool block (another command form, tool, script file, `eval`, interpreter or a split command): the approver names `block-bypass`, and the candidate is unchanged | `bypass-rerun` | A human decides the retry with a `--note` naming the violation and never accepts or warning-overrides the report; the new dispatch of the same stage on the same SHA always needs an explicit approval (`--approved-by`), under every `approval_policy` except `auto`; no `retry_policy.max_developer_retries` is spent | Dispatch ID and `report_sha256` (evidence of the violation only, never of its findings or checks), the `--note`, and the unchanged `candidate_commit` |
| The coordinator finds a defect in a clean developer report whose Definition of Done is met inside its allowed paths | `carry-over` | The approver of the `accept` (`batch decide --findings-file`); after a policy auto-accept the coordinator itself (`batch carry-over`, `policy:carry-over`) while no code-review dispatch exists for the candidate; the code-review dispatch is approved under `approval_policy` | Dispatch ID and `report_sha256` of the accepted developer report, the candidate, and the `carried_items` item IDs. No developer retry is spent before review |
| A read-only report (architect, verification, code-review or qa) lists `incomplete_items` that no item targets back at the reporting role, and the approver hands them on | `carry-over` | The approver of the `accept` or `override-warning` with `batch decide --carry-incomplete`; never a policy, since such a report is never auto-accepted; each target role's dispatch is approved under `approval_policy` | Dispatch ID and `report_sha256` of the read-only report and the `carried_item_ids`; each carried item's `source` names the report, its role, its target role and the reason. No retry is spent |
| A read-only report lists `incomplete_items` and none carries `tooling_blocker`, with no finding or warning/blocker severity, no open carried item, no failed check and an unchanged candidate | `narrowed-retry` | A human decides `batch decide --decision retry --narrowed`; the new dispatch of the same stage on the same SHA is approved under `approval_policy`; no `retry_policy.max_developer_retries` is spent | Dispatch ID and `report_sha256` of the retried report, the `carried_item_ids` the new brief carries, and the unchanged `candidate_commit` (none for an architect) |
| A read-only report lists `incomplete_items` and at least one carries `tooling_blocker` (for example, the safety classifier interrupted the role on that item), with the same absence of structured evidence | `tooling-retry` | A human decides `batch decide --decision retry --narrowed` after confirming the false positive and filing a bug ticket against the tool; the new dispatch of the same stage on the same SHA is approved under `approval_policy`; no `retry_policy.max_developer_retries` is spent | Dispatch ID, `report_sha256`, each item's `tooling_blocker`, the `carried_item_ids` and the unchanged `candidate_commit` |
| `batch decide --decision abandon` on any completion report | `abandon` | A human only, with a non-empty `--reason`; never a policy | Dispatch ID, `report_sha256` and `abandoned.last_accepted` |
| A proven worktree/model/adapter startup failure resumes in the same batch, with a new same-contract dispatch | `environmental-restart` | Human `batch resume-stop`; fresh human dispatch approval; bounded infrastructure attempts and `manual_all` afterward | Sealed recovery event, immutable source dispatch/snapshot/digest, clean checkout proof, retired dispatch IDs; unchanged handoff and developer counter |
| A stage report is `blocked` by an infrastructure failure although the work looks complete (commits match the plan) | `accept-conditional` | Human `batch decide --decision accept-conditional --infrastructure-failure` with a note; a later code-review or qa independently covers it | Decision with `routing.route`, the stage and candidate, and an open condition closed by the covering accept; publish refused while open; unchanged developer counter |
| A human selects a reached earlier gate, or resumes the pending report boundary of a no-route pause | `rewind` | Human `batch rewind --to` or `resume-stop`; fresh approvals; no budget reset and a new writing attempt spends developer retry budget | Sealed event binds before/after state, target, preserved writer SHA, superseded evidence and human approval; unchanged Git history |
| After a forced abandon (a dead end), the work resumes in a new batch for the same ticket and issue branch from the abandoned batch's `abandoned.last_accepted` record: `batch create --supersedes <batch>` | `supersede` | A human only, with `--approved-by` and `--approved-at` on `batch create`; a `policy:` approver is refused. The new batch still needs `batch approve`, its dispatches are approved under `approval_policy`, and a first developer-retry that carries a rebase target always needs an explicit approval except under `auto`; no `retry_policy.max_developer_retries` is spent | The superseded batch ID and its `abandoned.last_accepted`; for the same Definition of Done, the carried architect reference (`dispatch_id`, `report`, `report_sha256`, `commit_plan_sha256`); the `start_commit` and the `rebase_target_commit`. No risk, review, QA or operator decision evidence is copied |
| `report submit` recorded the report but its policy chain stopped (`completion.failed_step`: `policy-decide`, `risk-assess` or `next-dispatch`) | `report-completion` | No human approval: the coordinator runs `report complete` itself; it replays only the `auto_accept_policy` decision recorded at submit, and a step that needs a human stops with that step's remedy | Dispatch ID, `report_sha256`, the submit `completion` object and the `report complete` steps |
| Ledger busy: `ledger is locked by another operation`, or a `ledger_busy` answer from `dispatch status`, or a `qa run` failure `QA dispatch <id> was not released` | `report-completion` | No approval: repeat `dispatch wait`/`dispatch status`, run `report complete` when a recorded report's chain stopped, and never remove the lock by hand; a lock that stays held goes to `ledger release-lock`, which refuses a live owner. `qa run` waits up to 30 seconds for a busy lock before it releases its own QA lease and queue entry. If the lock frees after a failure, the dispatch is `approved` again: run `qa run` for it again. If the lock stays held, the dispatch stays `working`: after the lease expires, a coordinator runs `qa clear-stale-lease`, which also returns that dispatch to `approved`, and then runs `qa run` again | Lock owner (`pid`, `host`, `acquired_at`, `held_seconds`) and the `ledger release-lock` verdict; after a released `qa run` failure, its QA attempt record; after `was not released`, the lease owner and `expires_at` in the error and `restored_dispatch_state` in the `qa clear-stale-lease` answer and recovery record |

`batch decision-packet` shows the route before the decision is recorded: its `route_preview` holds
the `retry` routing record computed as `batch decide` computes it (it takes the same
`--reason-category` and `--retry-role` flags) and the `abandon` route; it writes nothing. The preview
does not check `retry_policy.max_developer_retries`: once that budget is exhausted it still shows a
`developer-retry`, `fix-forward` or `rebase-fix-forward` route, which `batch decide --decision retry`
then refuses. Like `batch decide`, it fetches `origin/<integration_ref>`, so it previews the proposed
`rebase_target_commit`.
When the retry route
cannot be computed, for example because the configured retry-reason classifier extension fails or
`origin/<integration_ref>` cannot be fetched, the
packet still renders and `route_preview.retry` is `{"route": null, "refused": ..., "remedy": ...}`
with the error `batch decide --decision retry` refuses with. With `--findings-file`,
`route_preview["carry-over"]` holds the carry-over record that `batch decide --findings-file` on the
pending developer report, or else `batch carry-over`, would record, or the same
`{"route": null, "refused": ..., "remedy": ...}` refusal. For a pending read-only report that lists
`incomplete_items`, the packet shows them as `incomplete_items` and, without `--findings-file`,
`route_preview["carry-over"]` holds the record `--carry-incomplete` would record or its refusal;
`--narrowed` previews the narrowed retry route. Every `batch decide`
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

After a fix-forward, the coordinator chooses the scope of the next code-review itself (issue #625).
`dispatch create --role code-review` without `--delta-review-of` computes `delta_review_scope` when
the candidate is the `commit_sha` of the batch's latest accepted developer work report, that report
is a developer-retry whose chain of directly retried developer-retry attempts was routed
`fix-forward` or `rebase-fix-forward`, and the newest code-review before the chain completed its
report on the chain's first `snapshot_commit` and was accepted or retried to a developer. That
review is the prior review. The delta is `git diff <delta_base> <candidate_commit>`: `delta_base` is
the prior review's candidate; after a rebase in the chain, it is the parent of the first commit
after the last rebase target that is not a reviewed copy. A reviewed copy resolves through the
chain's `rebased_from` pairs, each with a matching `git patch-id`, to a commit the prior review
judged. The scope is `full` with one or more `escalations`, each `{reason, evidence}`:
`new-risk-trigger` (a trigger of the candidate's risk assessment or of the delta's commits and files
that the prior review's assessment did not match), `file-outside-carried-items` (a delta file
outside the files of the developer-retry's carried items; a `review-finding` counts the
`review_scope` of its review), `patch-id-mismatch` (a pair of a chain report's `rebase_check`),
`dropped-commit` (a `dropped` entry of a chain report's `rebase_check`: the candidate lost a change
the prior review judged), or `no-new-commits`. Otherwise it is `delta`: the brief also carries the
developer-retry's `review-finding` items and its `incomplete-item` entries for the developer, and
the review judges both axes on the delta and accounts for every carried item. A `full` brief is an
ordinary full review with the section as audit evidence. Either way the brief keeps the full
`review_scope`, the proposal shows the section and the transition binds it as `delta_review_sha256`;
an explicit `--delta-review-of` keeps the test-only delta-review and records
`delta_review_scope: null`. Accepting either review moves the batch to `qa`, and clean-room QA runs
on the new candidate SHA with the full `verification_commands`. A retry of a delta-review hands on
the carried items it did not mark `closed`, and its own findings take the next `review-finding`
numbers.

A later recovery route adds its `RECOVERY_ROUTES` value and its row here in the same change.

## Automatic path (`approval_policy: auto`)

`approval_policy: auto` runs a batch from `batch approve` to the accepted publish without a human
approval. The policy applies only while the project config and the batch plan both choose `auto`
and the batch records no `auto_stop`. The project config must also set
`human_approval_gate: "trusted"` and `worker_attestation_required: true`; config validation and
`harness health` refuse any other combination. For `auto`, this section overrides the "Who
approves" column of the recovery route table.

Every approval that the policy gives is one hashed record in `batch.auto_decisions`: `sequence`,
`kind`, `dispatch_id`, `approved_by: "policy:auto"`, `approved_at`, `rationale` and `evidence`.
Ledger validation recomputes each `record_sha256` and checks that each record matches the approval
that it names. An explicit `--approved-by` is still a human approval under `auto`.

### Approvals

The coordinator session omits `--approved-by` and `--approved-at`. The policy then approves:

| Step | Record `kind` | Evidence |
| --- | --- | --- |
| `batch approve` | `batch-approve` | `plan_sha256` of the immutable plan, `scope_preflight_status`, `definition_of_done_items` |
| `dispatch create`, milestones included: `publish`, `risk-trigger`, `risk-reassessment-required`, `bypass-rerun`, `rebase-fix-forward`, `rebase-target` | `dispatch` | `transition_digest`, `brief_sha256`, `lifted_milestones`, `route_preview` and `reason_category` of the previous decision, its `report_sha256` |
| `dispatch resume --trigger` for a planned continuation (not `human-decision`) | `continuation` | `trigger`, `checkpoint_id`, `continuations_spent`, `max_continuations` |
| `batch auto-decide`, and an accept of the `report submit` or `qa run` chain | `decision` | `decision`, `report_sha256`, `route_preview`, `reason_category`, `basis`, `accepted_risks`, `commit_plan_sha256`, `bug_ticket`, `carried_item_ids` |
| `batch carry-over` | `carry-over` | `report_sha256`, `carried_item_ids` |

### Decisions

The chain of `report submit` and `qa run` accepts a clean report as before. For every other
pending report, the coordinator session runs `batch auto-decide --batch <id>`. The command
computes the decision from ledger facts under the ledger lock. The decision then passes every
check of `batch decide`, and the coordinator records it as `policy:auto`. The session passes only
the inputs that need judgement:

| Input | Use |
| --- | --- |
| `--commit-plan-file <path>` | An architect report that the policy accepts. The policy pins the plan if its `expected_paths` lie inside `allowed_paths` and it covers every Definition of Done item. Otherwise the path stops with `deterministic-gate-failed`. If the command cannot read the file as a JSON object, it refuses and records nothing. |
| `--findings-file <path>` | A developer work report that the policy accepts: the coordinator findings go to code-review as carried items. |
| `--bug-ticket <ticket>` | A `tooling-retry`. Before the command, the session creates or reuses a bug ticket for the blocking tool through the tracker CLI. Without it, the command refuses and records nothing. |
| `--block-bypass --note <text>` | The role worked around a hook or tool block. The note names the violation. |

Review reports may include `review.recommendation: accept|retry`. The project setting
`auto_retry_min_severity` defaults to `info` and is frozen into each batch; a human can override it
for one batch with `batch create --auto-retry-min-severity warning` (or `error` / `blocker`). At
the `warning` threshold, lower-severity findings remain in the original report and ledger, while an
`accept` recommendation with passed checks and no warning/blocker axis permits auto-accept. A finding at or
above the threshold, a reviewer `retry` recommendation, a failed required check, or any warning/blocker axis
still routes to retry. The default `info` threshold retries on info findings as before.
Warning/blocker axes always prevent plain accept, including at the `error` and `blocker`
thresholds. `error` is a threshold between warning and blocker; the report contract uses
`info`, `warning` and `blocker` findings.

The decision table:

1. A conflict-resolver report is outside the automatic path: the command refuses, and a human
   decides the report.
2. The policy accepts a clean report: `outcome: completed`, `blockers.status: none`, every check passes,
   no `not_covered` item, no carried gap, no scope warning, no review finding or warning/blocker
   severity, no review axis that names blockers, no incomplete item for the reporting role, and no
   `--block-bypass`. The record lists
   the report risks, its `risk_triggers` and the matched triggers of the candidate as
   `accepted_risks`. Incomplete items for later roles are carried. The accept of the publish
   report completes the batch.
3. The policy retries any other report. The reason category is `block-bypass` for a read-only
   stage and `code` for an architect or developer with `--block-bypass`. A report whose only
   evidence is incomplete items for its own role gets a narrowed retry. A completed developer
   report with a `not_covered` item, a scope warning or a carried gap gets `requirements`. A
   blocked report with a critical `context_pressure` record gets `context-pressure`. Otherwise the
   structured report data decides. The route is the `route_preview` of `batch decision-packet`.

The policy never chooses `override-warning`, `accept-conditional`, `block`, `fail` or `abandon`.

### Stops

A closed list of conditions stops the automatic path. `batch auto-decide` and `batch auto-report`
detect them in this order: integrity, budget, route.

| `category` | `reason` | Source |
| --- | --- | --- |
| `integrity-failure` | `stale` | Attention `stale-dispatch`, `stale-evidence` or `retry-queued-too-long` |
| `integrity-failure` | `model-mismatch`, `worktree-mismatch` | A self-report with `match: false` |
| `integrity-failure` | `harness-snapshot-changed` | The installed harness runtime differs from the pinned snapshot |
| `integrity-failure` | `deterministic-gate-failed` | The commit plan gate, or a failed revalidation of the pending report or its brief |
| `integrity-failure` | `ledger-validation-failed` | Ledger validation fails; the command shows the stop and records nothing |
| `budget-exhausted` | `retry_policy.max_developer_retries` | A developer-retry beyond the budget |
| `budget-exhausted` | `continuation_policy.max_continuations`, `continuation_policy.max_rate_limit_resumes` | A checkpointed or rate-limited dispatch with a spent budget |
| `budget-exhausted` | `attention_policy.max_infrastructure_retries`, `tooling-retry-repeated` | Attention `infrastructure-retry-repeated` or `tooling-retry-repeated` |
| `no-automatic-route` | `unknown-reason` | The route has reason category `unknown`, or is a developer-retry whose failure domain is `unknown`, or the coordinator refuses a developer retry because no code defect is proven (the stop keeps the `failure`), or attention `unknown-reason` |
| `no-automatic-route` | `abandon-dead-end` | The coordinator refuses the retry route, or the batch is `blocked` or `failed` |
| `no-automatic-route` | `supersede-dead-end` | The batch is `abandoned`; only a human continues with `batch create --supersedes` |
| `no-automatic-route` | `qa-environment-failure` | The QA gate failed on its environment (a read-only file system, or permission denied outside the repository) with no code-defect sign; the evidence names the remedy: a writable temporary directory, then `batch rewind --to qa` |

The coordinator records the stop once as the hashed `batch.auto_stop` (`category`, `reason`,
`detected_at`, `detected_by`, `evidence`). A stop records no decision and weakens no validation.
`batch auto-decide` records a `paused` transition and a sealed recovery event even when no report
exists. Repeating it while paused is idempotent. `batch auto-report` only observes and renders.
After human recovery, effective approval policy is `manual_all`: no report chain, infrastructure
shortcut or recognized 429 grants policy approval. The original `auto_stop` remains immutable.

### Final report

The coordinator records the final report once as the hashed `batch.auto_report`: at the accept of
the publish report, or with the stop. The report lists every `policy:auto` decision with its route,
reason and evidence, the accepted risks, the carried findings and their state, the retries and the
spent budget, the Definition of Done coverage per commit plan, the review and QA results, the
`candidate_commit`, and the stop when there is one. `batch auto-report --batch <id>` renders the
recorded report. Without a recorded report, it renders live evidence with `recorded: false` and a separate
`observed_stop`. It never changes state or audit. After recovery it also exposes `historical_report`
while reporting current progress; that old sealed stop is not the current outcome. After a refusal,
observe with `batch auto-report`, then explicitly record a detected stop with `batch auto-decide`.

No policy opens or merges a pull request. The coordinator shows the final report to the human
and may propose `/to-pull-requests`. A pull request needs an explicit human confirmation, and
auto-merge is forbidden.

## Operator recovery

Inspect `batch auto-report`, `batch decision-packet`, `dispatch status` and `ledger validate` before
you recommend any recovery or abandon. These commands are read-only. For each dispatch,
`dispatch status` reports a `startup` assessment: `worker_state`, `classification`,
`restart_allowed` and `unknowns`. The worker states are `absent` (the adapter could not start the
runtime), `reported` (the worker returned its final report), `rejected-at-startup` (the
coordinator refused its self-report, so the process may still run), `stopped` (the operator
confirmed the stop, or bound local process evidence proves exit), `live` and `unknown`. Silence, a timeout or a stop acknowledgement never
proves that a worker stopped. Capacity, rate-limit and service-unavailable are separate
classifications.
For a `docker` dispatch, `dispatch status` and `dispatch wait` also ask the container engine about
the container named by the receipt's `launch_id` and show it as `worker`: `state` (`running`,
`exited`, `missing` or `unknown`), `exit_code` and the `teardown` result. Only `exited` and `missing`
prove that the worker stopped. A live dispatch whose worker exited without a report becomes `stale`
at once and raises the existing `stale-dispatch` attention. A container worker cannot write the
ledger, so when its staged report is on disk the recommended action is `report submit --file <path>`
first (`dispatch wait` returns it as `staged_report`) and `dispatch cancel --runtime-stopped` only for
an unusable report; the engine never changes a lifecycle state. An engine CLI that did not run or did
not answer is `unknown`, never `missing`. A `dispatch status` without `--dispatch` asks the engine
about at most four dispatches per read; the rest show `not_observed`. The first proof is
recorded permanently as `worker_exit`. After a dispatch is `reported`, `cancelled`, `abandoned` or
`failed`, the same commands remove that dispatch's worker, sidecar and network by their labels, always
after `worker_exit` is persisted. A worker that is still running is never stopped or removed: stop
it explicitly with the container engine. `harness cleanup <repo>` previews and, with `--apply`,
removes the container resources of this repository that no open dispatch owns; it skips a running
worker, an open dispatch (even one with `worker_exit`) and a terminal dispatch whose exit evidence is not recorded yet (run
`dispatch status` first), and it never touches another repository's resources.
`ledger validate` is read-only: it validates the generation, checksums, audit, graph and current
batch contracts; it never repairs, migrates or resets a ledger. A legacy generation receives its
version's graph/audit validation; mutations require the normal explicit migration.

Use current UTC for a concrete human decision:

```bash
python <main-repo>/.harness/orchestration/coordinator.py --repo <main-repo> batch resume-stop \
  --batch <id> --approved-by <operator> --approved-at <UTC> --note "worker directory fixed"
python <main-repo>/.harness/orchestration/coordinator.py --repo <main-repo> batch rewind \
  --batch <id> --to code-review --approved-by <operator> --approved-at <UTC> --note "repeat independent review"
```

`resume-stop` restarts only a worker whose outcome is known: `absent`, `reported`,
`rejected-at-startup` or `stopped`. For a refused self-report the existing contract holds: the
human approval retires that dispatch and the ledger rejects its late writes. When `dispatch
status` shows `live` or `unknown`, `resume-stop` refuses and changes nothing: stop the worker
through its runtime and repeat the command with `--runtime-stopped`. A missing self-report is not
a failure `resume-stop` confirms; stop the worker and record `dispatch cancel --runtime-stopped`
(below). This is the first step when a worker did not start and no exit is proven: `resume-stop`
has no recorded stop to resume and `dispatch send` refuses an already sent dispatch. When bound
process evidence proves the exit, `batch resume-stop --restart` replaces both steps with one
human decision. `resume-stop` accepts a structured worktree/model mismatch or a classified startup
failure. Without structured evidence, a conservative text heuristic may name an infrastructure cause (capacity,
rate limit, timeout, network, runtime crash, resources, service-unavailable or service outage) in
the blocked report's `blockers` or the runtime error. Any sign of a test, lint, type, conflict,
scope or requirements problem wins, and unknown text stays unknown. The sealed event records the
match as `classification` with `method: heuristic`; it only proposes the cause and the human
approval still decides. No report, or a blocked report with no changed files, failed checks or
review findings, is eligible. Branch, known SHA and tracked/untracked cleanliness must match.
The old dispatch is retired; the next dispatch has a new ID, context and transition digest under a
fresh human approval, optionally covered by the explicit `--restart` recovery decision. Role, purpose, scope, candidate and fix-forward handoff remain fixed. It
spends an infrastructure attempt, not another developer retry. Repeating the same recovery before
its next dispatch is created changes nothing. A budget/no-route pause with a pending report can
resume that report boundary for a human decision; this does not invent a retry or accept its work.
Unknown failures, drift or dirty files need an ordinary decision or human rewind. When the
infrastructure budget is exhausted, `resume-stop` refuses and the batch stays in its stop state for
a human choice: wait, repair the launch, adjust the attention policy, rewind, or a separate
abandon decision. Recovery never resets the counters.

`dispatch send` checks each `--adapter-arg` option against the adapter's `dispatch --help` before
the handoff. It refuses an unsupported option and leaves the dispatch approved and unsent. A help
call that fails or times out is a `limitation` in the send result's `adapter_preflight`, not a
capability claim. To diagnose an app-server runtime without a worker, run:

```bash
python <main-repo>/.harness/orchestration/coordinator.py --repo <main-repo> dispatch runtime-probe \
  --dispatch <id> --runtime-command <runtime> --runtime-arg=app-server
```

The probe sends `initialize`, `model/list`, `thread/start` and one `turn/start` on an ephemeral
read-only thread without tools. It is `ready` only when the brief's model is listed, the
`thread/start` model, `cwd` and reasoning effort match the brief, and the turn completes. A
capacity, rate-limit or service error after `thread/start` is a classified failure, not READY.
The probe writes nothing to the ledger, starts no worker and never replaces the worker's model
self-report.

`rewind --to architect|developer|code-review|qa|publish` selects a reached gate. `verification`
requires an active registered candidate; `resolve-conflict` requires a resolver boundary. It cannot
skip forward or reopen completed/abandoned work. Before the first accepted gate, it preserves the
initial `batch approve` requirement. Superseded dispatches, candidate registrations, risk, review
and QA remain historical and cannot authorize new work. A superseded architect plan is retained
in the recovery event; a new architect must be accepted before coding. Git HEAD and commit objects
stay unchanged. Known current writer progress supplies the new startup SHA. A new code attempt
spends the remaining developer retry budget when its dispatch is created; rewind never resets
continuation, rate-limit, resolver or attention counters.

A rewind to the writer that retires risk assessments leaves `risk_reassessment_required` true and
sets `risk_reassessment_pending_next`: the next completed or blocked writer report (the one whose commit `accept-conditional`
or a verification retry then registers) binds its candidate to the
requirement (`risk_reassessment_candidate`) and clears the marker. Only `risk assess` of that
candidate clears it; a rewind to `verification` binds the registered candidate at once, a rewind to
`code-review` or later clears the requirement, and QA stays refused while it is open.

If the stopped writer made commits or left pending files without submitting a report or
checkpoint, use the explicit preservation route. First inspect its registered issue worktree,
full HEAD SHA, branch, and tracked/untracked changes. Stop the actual worker and record
`dispatch cancel --runtime-stopped` with human approval. Then obtain human approval to continue
that exact unfinished checkout and name every pending path:

```bash
python <main-repo>/.harness/orchestration/coordinator.py --repo <main-repo> batch rewind \
  --batch <id> --to developer --preserved-commit <full-HEAD-SHA> \
  --preserved-file <pending-path-1> --preserved-file <pending-path-2> \
  --approved-by <operator> --approved-at <UTC> --note "continue inspected unfinished work"
```

Omit `--preserved-file` for a clean checkout. A resolver uses `--to resolve-conflict` with its
registered resolver boundary. Preservation requires the latest dispatch to be a cancelled work
writer with a recorded runtime stop. HEAD must descend from its immutable startup snapshot;
committed changes and every pending path must fit its write scope. A missing or extra path,
foreign checkout/branch, symlink, live worker or forward gate is refused without changing files
or ledger. Ordinary rewind still requires known progress and a clean checkout.

The sealed recovery event records the source dispatch, original snapshot, preserved HEAD,
checkout proof, file content/mode hashes and staged/unstaged diff hashes. The next writer brief
carries `preserved_progress`, bound through the recovery event in its fresh transition digest.
Preflight, proposal, brief creation, send and initial self-report recheck that checkout. If it
changed, inspect it and make a new recovery decision; preserve the old evidence. Once startup
succeeds the worker can continue editing, and historical ledger validation retains the handoff
as evidence rather than treating it as a live file lock. Preservation neither accepts a report
nor bypasses review/QA; fresh human dispatch approval and the existing retry budgets still apply.

`batch decide --decision accept-conditional --infrastructure-failure --note <cause>` lets the
pipeline move past a stage whose report is `blocked` by an infrastructure failure while the work
itself looks complete: a developer report whose commits match the plan, for example, when the
handoff to review failed. A human attests the cause; a policy never does. The report must be
`blocked` and carry no failed check, and every other check of an ordinary `accept` still applies
(commit plan, scope, definition of done, carried items). The decision is recorded as
`accept-conditional` and leaves an open condition on the batch; the pipeline advances exactly as
after `accept`. An unconditional `accept` of a later gate independently covers it: code-review or
qa for architect, developer and verification; qa for code-review and qa. Publish and completion
refuse while a condition is open. A conditional qa never counts as green QA evidence, so publish
still needs a real QA run. A conditional publish needs the remote branch to already point at the
candidate, which closes its condition at once. A rewind retires the conditions of superseded
dispatches. Prefer `resume-stop` when the failed stage can simply be restarted.

A timeout does not prove that a worker stopped. Stop it through the runtime first, then record:

```bash
python <main-repo>/.harness/orchestration/coordinator.py --repo <main-repo> dispatch cancel \
  --dispatch <id> --runtime-stopped --approved-by <operator> --approved-at <UTC> --reason "runtime stopped"
```

Cancel without `--runtime-stopped` still accepts only approved unsent work. Retired dispatches
reject late worker writes. Recovery never silently resolves unrelated attention or runs a worker.

New recovery commands run on the installed runtime, even for an old pinned batch. When needed,
the human recovery event binds the original runtime hash to the installed control-plane hash and
stores its verified snapshot. Later commands follow that audited epoch. Immutable plan, brief and
report hashes retain their original values; missing or invalid audit prevents this upgrade.

An early recognized 429 can preserve a writer's clean immutable startup SHA before a checkpoint.
The coordinator records separate startup evidence binding SHA, brief, scope, DoD and dependencies.
It creates no checkpoint and invents no checks or remaining DoD. Unknown progress needs a real
checkpoint or newly scoped dispatch. Retry windows and both continuation budgets still apply;
each resumed session must self-report its actual model and selected checkout and send heartbeat.
Read-only work and publish retain their separate new-dispatch/QA/publish contracts.

If a writer session exits before its first checkpoint, the operator may use
`dispatch resume --trigger startup-failure --runtime-stopped` with human approval and a note.
`--file` must restate exactly `dispatch_id`, `definition_of_done`, `dependencies`, `write_paths`
and `prohibited_changes` from its immutable brief. The clean checkout must remain at its known
startup SHA. This spends continuation budget, records separate startup evidence, and requires
new self-report/heartbeat. It cannot bypass a paused batch or changed progress. After a resume
awaiting its first self-report, repeating the command is refused without spending another attempt.

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
  files, `commit_map` and, from its `tooling_blocker`, only `uncommitted_files`; the decision's
  dispatch, role, route and reason category; each finding's axis, severity and summary, without
  its quoted evidence;
- the starting files narrow to the files a finding names or the developer report lists in
  `changed_files` or `tooling_blocker.uncommitted_files`;
- a large document stays only as its `sections` index.

`retry_start.context_estimate` records the threshold and the estimates before and after the compact,
and the preflight's decision packet repeats it as `retry_context_estimate` with
`retry_context_warning`. The preflight writes nothing and never blocks the dispatch: when the compact
cannot reach the threshold, the warning names both numbers and the dispatch proceeds. Read-only roles
(code-review, QA) always start new independent sessions.

A `tooling-retry` developer restart inherits the uncommitted work of a blocked commit. Its HEAD is
pinned to the developer's last commit. Its `dispatch preflight` also compares the worktree's
uncommitted paths with the blocked report's `tooling_blocker.uncommitted_files`; coordinator state
and tool sandboxes do not count. The preflight passes only when both lists are equal and every path
is inside the batch `allowed_paths` (a batch recorded before explicit scopes: inside its zone). A
clean worktree with no list passes as before. The preflight refuses an extra, missing or
out-of-scope file and names each discrepancy.

## Approvals bound to the transition digest

Before any dispatch exists, `dispatch propose` renders the canonical transition and its
`transition_digest` (SHA-256 of: batch ID, previous dispatch ID and role, reason category, next
role/action and purpose, candidate SHA, base SHA, review scope, verification commands, Context
Package ID and required gates) and writes no brief. It registers the shared Context Package the brief
would pin, so the package ID is part of what the human sees. `dispatch create` with an explicit
approval must pass that digest as `--transition-digest`; the coordinator recomputes the transition
from the ledger and refuses on any difference, so a changed scope, candidate, role, verification
command, reason category or Context Package needs a new proposal and a new approval. A shared
package whose frozen memory source changed since registration is not reused: the proposal
registers a new package with current pointers, so an approval of the old one no longer matches.
A brief that carries a verification plan binds the plan's digest as `verification_plan_sha256`
(added only then, so a transition without a plan keeps its digest): a changed policy, candidate or
impact set after the proposal needs a new proposal, and a plan edited after approval no longer
matches its approval.
The digest is stored in the approval and in the immutable brief, together with the transition
itself, and ledger validation re-derives it. A policy approval (`milestone`, `low_risk`, `auto`) is derived from the transition
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
model and exact recorded checkpoint SHA. The original brief and full commit-plan evidence boundary remain
unchanged. Startup recovery of initial developer work may pin preserved progress before its first
completed report; it still needs report acceptance, risk assessment and downstream gates.
A `context-pressure` retry needs a critical record for the reported dispatch.

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

For a stale dispatch, `recommended_human_action` names the read-only evidence commands and the
choices: keep waiting, or stop the runtime and record `dispatch cancel --runtime-stopped` before
`batch resume-stop` or `batch rewind`. `batch abandon` remains a separate human decision.

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
`acquired_at`, and where the platform has `/proc` its `pid_namespace` and `boot_id`, plus the
coordinator `operation` when known). A busy lock fails a write command with a remedy to repeat it;
`dispatch wait` and `dispatch status` keep polling instead (`dispatch status` answers
`ledger_busy`). `coordinator.py --repo . ledger lock-status` shows the lock without taking it: its
owner, `held_seconds`, `remaining_seconds` until the timeout release, the verdict and whether a
release is available now. A lock that stays held is released only by
`coordinator.py --repo . ledger release-lock`, which refuses a lock whose owner process is alive or
runs on another host, releases a dead owner's lock, and releases only once it is stale a lock
without an owner record or an owner from another PID namespace or boot (`owner-in-other-environment`:
a sandboxed worker's pid names another process outside its sandbox); concurrent releases are
serialised. Only a human operator may run `ledger release-lock --force --approved-by
--approved-at --reason`: it releases the lock whatever the verdict and writes an integrity-checked
record to `lock-audit/` in the state directory; no policy, `auto` included, ever runs it. Nobody
removes the lock or any state file by hand.

When a new fact appears after dispatch, the coordinator appends a new coordinator decision before
acting on it. The decision records the dispatch ID, fact and evidence, impact on scope or risk,
chosen action, and author/time. The original brief remains immutable. If the fact changes the
scope, DoD, assignment, or required proof, the current dispatch is ended and the changed
work is planned and approved as a new dispatch.

## Observable external workers

Select the shipped example explicitly: `--adapter .harness/orchestration/adapters/claude_code.py`.
It is a runtime adapter with `dispatch --help`; the `claude` or `codex` executable itself is not
an adapter. This example targets Claude Code. `--adapter-arg=--runtime-executable=<path>` selects
its Claude Code executable. Help validation must confirm the stream/session/manual-permission
options before launch; another runtime needs its own compatible adapter.

Send from the Harness console to mount the selected worker view before launch. Standalone use
requires an explicitly selected `--adapter-arg=--visibility=background`; absence of a visible
console never silently becomes a hidden launch. The adapter retains native `manual` permissions,
local hooks and the brief's tool list. It adds no auto-approve or permission-bypass flag.
`--adapter-arg=--worker-sandbox=disabled` applies only to this invocation and requires the brief's
already approved `unsandboxed` access plan; it never edits global settings. Required isolation
still needs a suitable native provider. Prepare a changed mode before propose/approval.

The adapter probes its filesystem resources and an unauthenticated HEAD to the runtime endpoint.
`--adapter-arg=--endpoint=<url>` selects a credential-free endpoint within the approved network
hosts. An HTTP response proves reachability, not model authentication or readiness. Probe the
same Git, ledger, inbox and configured caches again through the worker's actual native tool before
task work: `python .harness/orchestration/adapters/claude_code.py probe --repo <main-repo>
--brief <absolute-brief-path>` (retain any explicit state directory and endpoint).
A successful parent probe does not prove access inside the worker's native sandbox. On EROFS or
EPERM, stop before implementation and name the exact resource and context; prepare the environment
instead of repeating the identical launch. Keep all checks honest when a layer is unverified.

`dispatch send` reserves the handoff once, releases the ledger lock before external/native launch,
and merges the returned receipt into the latest status without overwriting self-report or report.
The versioned `harness-worker-v1` receipt binds dispatch/launch, process identity and local event
paths. Linux observation checks host, boot ID, PID namespace and process start time; a missing
bound process proves exit, while a reused PID, another host/namespace, unavailable probe or legacy
receipt alone cannot authorize restart. A supervisor's durable `terminal.json`, bound to the same
dispatch/launch and shared log directory, proves exit even when `/proc` identity is unavailable;
an absent, malformed or differently bound terminal record supplies no proof.
`liveness.startup_timeout_seconds` is pinned from
`execution_policy.worker_startup_timeout_seconds` (120 by default), independently of heartbeat
staleness. `dispatch wait` returns `startup_failed` for proven early exit and `startup_timeout`
for a missed first self-report; timeout alone never proves termination. After a first self-report,
proven process exit with no recorded report returns `worker_exited_without_report` without changing
the ledger: the work and a staged report file may exist, so inspect the staged report and the branch
before choosing a decision. `startup.classification` is `worker-exited-without-report`; it proves
no environment failure, so `resume-stop` does not treat it as one.

A worker must write the coordinator state: at least `.harness/orchestration/state` with its
`.coordinator.lock` directory and ledger records, and the report inbox. `report submit` takes that
lock, `dispatch heartbeat` does not, so heartbeats can succeed while a submit fails. Grant the
write through the runtime's own sandbox settings (Claude Code: `sandbox.filesystem.allowWrite`
with the state and inbox directories) before launch. A refused lock returns
`sandbox_denied: ...` with the exact path, not a traceback. Classify it as infrastructure, not as
a task error; deliver the staged file with the relay below only after the cause is named. The
adapter probe takes and releases the real lock (a lock held by another operation counts as
writable) and reports `ledger_lock`; it proves only the adapter's own context and does not
replace the worker-side probe above.

For proven early exit under `auto`, observe `batch auto-report`, then record its
`runtime-unavailable` stop with `batch auto-decide`. The coordinator does not invent a role report.
One explicit `batch resume-stop --restart` human decision may retire that failed dispatch and
approve one successor under the unchanged role, SHA, model, access, tools, commit plan and work
contract. It does not send the successor. Drift needs an ordinary new proposal/approval; every
later transition remains `manual_all`. Repeating the combined operation returns the same successor
and does not spend another infrastructure attempt. The original stop and retry budget remain.

`dispatch observe --dispatch <id> --offset <previous-next_offset>` reads bounded existing events.
The Textual worker screen uses the same local reader. Reconnect, filters, history and closing the
screen neither start/stop a runtime nor call a model. Never add progress prompts, heartbeat summaries,
extra agents or log copies to model context for display. Native permission requests use the runtime
control protocol: a human can deny or allow the exact request once; the response preserves input
and changes no persistent permissions or batch approval. Secret values and thinking blocks are
excluded before event storage as well as from display; the example bounds its event file to 10 MiB
and the UI retains 500 lines. Native input is kept only in supervisor memory; the local pending
record contains a redacted preview and digest. A stream gap is visible; a missing stream is not a dead worker.
Process/exit, ledger lifecycle, staged report and report acceptance remain separate facts.
The shared theme, TopBar, Footer, section menu, RU/EN and F1/F2 belong to the existing console;
container/recovery navigation reuses that shell rather than defining another lifecycle controller.

Architect retries carry `retry_reason` in the immutable brief, including the previous dispatch,
source and decision digest, bound to the transition approval. A human's non-empty `--note` is
preserved exactly; explicit blank/whitespace/`none` is refused before recording a decision. For
older decisions or structured retries without an operator note, the sealed routing rationale is
carried with source `decision.routing.rationale`, never labelled a human explanation. Existing
unknown-reason attention remains required unless that exact human decision supplied an explanation.
No external prompt file or temporary-directory handoff participates.

## Role order, liveness, and transport

The architect step is not optional and not implicit: no `developer` dispatch exists for a batch until
that batch already carries an `architect` completion report the coordinator has accepted. The
installed `coordinator.py` refuses to create the brief otherwise, so a manual CLI call cannot skip it
either.

A dispatched role is not assumed to be alive because it was sent. Its first action after receiving
its brief is a model self-report: it names the model it is actually running, and the coordinator
compares that against `resolved_model` in the immutable brief. A runtime probe never fills a
missing self-report. For architect, developer,
conflict-resolver, database-migrations, messaging-integration and code-review, and for other roles
when the project enables `worker_attestation_required`, that same first action proves the canonical Git top-level, branch and
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

The `backend-orchestration` capability installs standalone stdlib adapters at
`.harness/adapters/claude_worker.py` and `.harness/adapters/codex_worker.py` through `harness init`
or `harness adopt`. If that capability is already selected, use the ordinary managed lifecycle:

```bash
python /path/to/agent-harness/harness/bin/harness.py diff /path/to/project
python /path/to/agent-harness/harness/bin/harness.py update /path/to/project
```

For an approved `external` dispatch, choose the adapter for its assigned runtime. Use the main
repository path for both the coordinator and adapter; send selects the worker checkout as CWD:

```bash
python /path/to/project/.harness/orchestration/coordinator.py --repo /path/to/project dispatch send --dispatch <dispatch-id> --adapter /path/to/project/.harness/adapters/claude_worker.py
```

For a Codex dispatch, use:

```bash
python /path/to/project/.harness/orchestration/coordinator.py --repo /path/to/project dispatch send --dispatch <dispatch-id> --adapter /path/to/project/.harness/adapters/codex_worker.py
```

Python adapters are invoked through the coordinator's Python interpreter, so executable mode is
not required. Both expose `dispatch --help`, `--repo`, `--brief`, `--dry-run` and
`--extra-prompt-file`; binary overrides are `--claude-bin` and `--codex-bin`. A retry prompt file
can be passed with `--adapter-arg=--extra-prompt-file --adapter-arg=/absolute/path/to/retry.txt`.

Adapters pass the lifecycle prompt through stdin and return after detached startup. An early
exit, including exit zero, is a launch failure with a bounded log tail. They reject `qa`,
`purpose: publish`, a transport other than `external`, and absent or empty `allowed_tools`.
Claude uses the brief's model, effort and native tool allowlist; its only additional directory is
the incoming reports directory. Codex uses `codex exec` with the brief's model and effort,
`read-only` for read-only roles and `workspace-write` for writers. Its prompt carries the tool
allowlist; this is not a native Codex tool filter. Neither adapter grants `danger-full-access`.
The worker still must self-report, send heartbeats and submit its report. An adapter does not
accept reports or select the next role. QA and publish keep their coordinator boundaries.

Managed updates refresh unchanged adapters. Unknown targets and symlinks are protected; local
regular managed edits require an explicit force option. Capability removal and uninstall retain
modified and unknown adapters. Console settings show missing/current/outdated status and require
a fresh preview and explicit confirmation; they preserve capability selection and never enable force.

When the project authors `access_policy`, the resolved access plan is pinned in the brief and bound
to the approval digest. `dispatch send` blocks the handoff, with a reason and a concrete preparation
action, when the selected `runtime_access` implementation cannot prove and natively apply the plan
for that exact worker; it never falls back to `inherit`. A blocker is a question for the human, not
something to work around: do not edit the brief or the config to make the check pass.

An `approved` dispatch that has not yet been sent may be cancelled with recorded approval and reason.
The brief remains immutable evidence, its batch returns to `awaiting-approval`, and the coordinator
creates a new approved brief only after the corrected assignment is reviewed. Unsent cancellation
is distinct from `batch abandon`, which requires a separate explicit human decision for the batch.

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
  commands. `batch create` freezes the developer and review lists of the first
  `verification_profiles` entry whose `paths` cover every allowed path, else the general lists, and
  freezes `retry_verification` when configured: a developer-retry brief then runs its `lint`
  commands and one `test_command` over the Context Package's `related_tests`, or the developer list
  when there are none;
- `verification plan`: `verification_plan`, built by `dispatch propose` from the batch's frozen
  `verification_plan_policy` (`max_related_tests` and stage templates `{stage_id, kind, argv,
  timeout_seconds}`, at least one of kind `pytest`) and the ChangeImpact of `base..candidate`
  (ADR 0023, sections 1, 2 and 6). `batch create` freezes the project's `repo_map_policy` beside
  the policy, because it shapes the import graph. It is present only on a developer-retry, code-review or QA brief of a batch that configured the
  policy; a first developer has no diff, and a batch without the policy keeps the brief it always
  had. Each stage adds `selection` (`full`, `focused` or `failed-only`), `nodeids` and
  `environment_fingerprint`. The pytest stage is `focused` over every related test of the import
  graph, without a first-N cutoff, and widens to `full` instead of failing when the impact is
  unknown: a changed `conftest.py`, `pyproject.toml`, `uv.lock`, `Makefile`, `.github/**`, verify
  scripts, capability catalog or skill, a removed or renamed module that others import, a
  non-Python file, a dynamic import, an unavailable parser, a Python file the graph cannot read
  (hidden by the Repo Map policy or not parsed) that names an impacted module, or more related
  tests than `max_related_tests`. On a docker dispatch the pinned image digests are part of every
  stage's `environment_fingerprint`. A QA plan is always `full`. The plan's digest is bound into the transition as
  `verification_plan_sha256`; `verification_commands` stays the list a role reports in `checks_run`.
  `dispatch propose` and `dispatch create` for code-review and QA refuse with `impact_not_covered`
  (exit code 2, the uncovered tests listed) when the plan's pytest stage is not `full` and does not
  list a related test; that is a machine check, not something a report replaces;
- `dependencies and assumptions`: known blockers, required inputs, and their owner;
- `coordinator approval`: approving person, timestamp, and the approved concurrency decision;
- `commit plan` (developer work): ordered entries `{id, summary, expected_paths, covers}`, where
  `covers` names the Definition of Done items an entry implements. It is one entry per item unless
  the operator pinned the architect's plan with `batch decide --decision accept --commit-plan-file`;
- `commit plan divergence` (code-review): how the last accepted initial or rebase developer report
  diverged from its plan, or `null`;
- `carried items`: one shared channel keyed by the kind of source,
  `{"coordinator-finding": [...], "review-finding": [...], "incomplete-item": [...]}`, each item
  `{item_id, source, summary, files, expected_evidence}`, or `{}`. A code-review or developer work
  brief carries every open coordinator finding. A developer-retry brief carries exactly the closed
  list its retry decision recorded as `retry_item_ids`: after a retried code-review, also that
  review's Standards and Spec findings; after a retried developer report, that brief's own items.
  Any work brief carries, as
  `incomplete-item`, the open items a read-only report handed to its role with
  `--carry-incomplete`, and a narrowed retry's brief carries only the items of the report it
  retries. An `incomplete-item` `source` names `dispatch_id`, `report_sha256`, the reporting `role`,
  the `target_role` whose brief carries it, the `route` (`carry-over`, `narrowed-retry` or
  `tooling-retry`), the `reason` and its `reason_category` (`tooling` for an item a tool blocked,
  otherwise `null`); its `summary` is the brief item and its `files` are empty. A non-empty section
  is bound into the transition as `carried_items_sha256`;
- `rebase target`: `rebase_target_commit`, the commit a developer-retry rebases onto, bound into the
  transition as `rebase_target_sha`: the integration tip of a `rebase-fix-forward` retry, or the
  `supersedes.rebase_target_commit` of a superseding batch while the snapshot does not contain it
  yet. It is `null` on every other brief, including the developer dispatch of a legacy stale-base
  record;
- `delta review scope` (code-review): `delta_review_scope`, the coordinator's delta or full choice
  after a fix-forward (`mode`, `route`, `prior_review`, `developer_dispatch_id`, `delta_base`,
  `delta_commits`, `reviewed_copies`, `closure`, `escalations`), bound into the transition as
  `delta_review_sha256`, or `null` on every other brief, including a test-only delta-review.

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

## Delivering a worker report after a tool refusal

A worker that cannot submit keeps its original `report_staging_path` bytes and returns the exact
command/error. The coordinator may deliver that existing file through `report submit --file <path>
--relayed-by <coordinator> --relay-reason <verified-refusal> --source-sha256 <original-file-sha256>`.
The recorded relay binds author dispatch/role, sender, reason, source path/hash and time in audit;
it does not alter worker JSON. Missing file, model/worktree attestation or invalid report remains
a blocker. The coordinator never creates those facts on the worker's behalf. QA still uses its
clean-room report boundary. No new developer run or developer retry is spent solely to deliver a file.

Read the submit result before advancing: `completion` means the report is already recorded and
needs `report complete`; a pending `auto` policy decision needs `batch auto-decide`. Submit can
already continue its own policy chain, so do not call auto-decide unconditionally. Repeating an
identical relayed file is idempotent; a different file/hash cannot overwrite an immutable report.
`fail` and `not_run` stay unchanged. A later baseline comparison is separate evidence and does not
turn missing `make verify` or native-isolation checks into passes.

## Completion report

Each dispatched role returns one completion report. The coordinator accepts the batch only after
the report is complete and its evidence is independently sufficient for the role's contract.

Report prose is Russian (`report_language: ru`); machine-read values stay exact English enums.
`outcome` is `completed | blocked | failed`. `blockers` and `risks`, including each composite
review axis, are objects with exactly `status` and `details`. For absent evidence write
`{"status": "none", "details": ""}`; for present evidence write
`{"status": "present", "details": "<Russian description>"}`. The same objects are required in
checkpoints and continuation risks. A `none` status requires empty details; `present` requires
non-empty details. Put other explanations in `output`. Policies read only the structured status;
they never infer the absence of a blocker from its Russian description. `report submit` rejects
string fields, translated statuses and malformed objects before writing ledger evidence, and
returns the allowed format as its remedy. Correct the staging file and resubmit for the same
dispatch; this refusal preserves the active approval policy. Old report shapes are not accepted
and immutable evidence is never rewritten.

The report must include:

- dispatch ID, ticket, role, and outcome (`completed`, `blocked`, or `failed`);
- a concise output summary mapped to the brief's Definition of Done;
- `commit SHA` containing the work, required for every write role; a read-only role records
  `not applicable — read-only role` and must not create a production commit;
- the exact `changed files`, or an explicit `none` for read-only work;
- a `checks run` list with every exact command, result, and relevant evidence; “done” is not a
  check result;
- structured `risks`, with `status: present` and details for unverified edge cases and deferred decisions;
- structured `blockers`, with `status: none` and empty details when absent;
- the next coordinator action, including the required independent gate when applicable;
- for a developer brief with a `commit_plan`: a `commit_map` of `{commit_sha, plan_entry_id}` pairs
  covering every created commit. An initial or rebase report may map one commit to several entries
  and one entry to several commits; when that mapping is not one-to-one it also carries
  `dod_coverage` (one record per Definition of Done item, either its covering commits or
  `not_covered` with a reason) and a `divergence_justification` naming what was merged, split or
  added and why. A developer-retry report maps each new commit to one distinct entry and carries
  neither field. Under a non-null `rebase_target_commit` it also lists every previous-candidate
  commit (after `git merge-base <snapshot_commit> <rebase_target_commit>`, up to
  `snapshot_commit`) exactly once, as `{commit_sha, rebased_from}` for its rebased copy, which
  inherits the original's plan entry, or as `{rebased_from, dropped}` with a non-empty reason; every
  commit after the target appears once, a rebased copy or a new commit. A missing, repeated or
  unknown previous-candidate commit is refused, and so are a copy that is its own original (a merge
  of the target instead of a rebase) and `rebased_from` and `dropped` without a target. `report submit`, `batch decision-packet` and the accept decision return `rebase_check`:
  the target, the old base, each pair with `patch_id_match` (`git patch-id --stable`), the dropped
  commits and the `patch_id_mismatches`. A mismatch is a conflict resolved with changes: it is
  shown for delta-review and neither refuses the report nor makes it unclean;
- for a developer-retry brief with carried items: `carried_item_closure`, one record per carried
  item, either `{item_id, commits}` (the commits of its retry chain that close it) or
  `{item_id, not_closed}` (a non-empty reason). The retry chain is this dispatch and the earlier
  attempts that handed it the same closed list (a retried developer report or a developer
  `tooling-retry`): its commits count from the `snapshot_commit` of the chain's first attempt that
  this dispatch's `snapshot_commit` still descends from (from the rebase target under one, and from
  the target of a not yet accepted `rebase-fix-forward` attempt of the chain), so an item an
  earlier attempt closed names that attempt's commit, or its rebased copy after a rebase. After a
  `rebase-fix-forward` attempt the chain owns two kinds of commits: the new commits of its attempts,
  and the rebased copies whose `rebased_from` in the `commit_map` of the attempt's report names a
  commit the chain created. A copy of a copy is traced through any earlier rebase to its original.
  The rebased copy of a commit that existed before the chain (at or below the `snapshot_commit` of
  its first attempt) is refused with the same remedy as its original. A legacy stale-base rebase
  maps no `rebased_from`, so its closure still counts every commit above the batch target (the one
  exception). A completed report must carry it, a blocked or failed one may, and no other report
  may. A missing, unknown or repeated item, an empty
  reason, an empty commit list, an unresolvable SHA or a commit the chain did not create is refused,
  with or without a `commit_plan`. A completed report recorded before issue #503 without the field
  is still decided: every item is `omitted` and a carried gap.
  A `not_closed` item is a carried gap: no policy accepts the report, plain `accept` is refused,
  and only `override-warning` with a note other than `none` (recorded as `carried_items_gap`) or
  `retry` decides it. Without a rebase target (the brief's non-null `rebase_target_commit`, or the
  batch's target on a legacy stale-base record with `base_rebase_required`), the report's
  `commit_sha` must descend from `snapshot_commit`. A retry that rewrote that history (amend, squash or reset) is
  refused; its remedy is to recover the commits from `git reflog` and re-apply the fix as new
  commits;
- for a code-review brief with carried items: `review.carried_items`, one
  `{item_id, status: closed | open | unverified, evidence}` per item the brief carried. An omitted,
  `unverified` or `open` item is a carried gap: the report is never clean, no policy accepts it, plain
  `accept` is refused, and only `override-warning` with a note other than `none` (recorded as
  `carried_items_gap`) or `retry` decides it. An `open` item is `code` evidence for the retry route;
- for a qa report of a project that declares `qa_preparation`: `qa_stages`, written only by `qa run`
  and never by hand: `stages` (one `{stage, command, result, exit_code}` per executed command, with
  `diagnostics` on a failed one; `stage` is `preparation`, `environment-probe`, `project-file-check`
  or `gate`), `failed_stage` (`preparation`, `gate` or `null`), `code_checks_started` (`started`,
  `not_started` or `unknown`) and, for a failed preparation, a `diagnosis` (`category`, `signals`,
  `basis`). A gate command the run never reached has the `checks_run` result `not_run`; it is
  neither a pass nor a failed code check. A project that does not declare `qa_preparation` keeps the
  earlier report shape without `qa_stages`;
- for a role a tool blocked: `outcome: blocked` and `tooling_blocker`, exactly the non-empty strings
  `tool`, `command` (as invoked) and `message` (verbatim), each at most 1600 characters. It is valid
  only on a `blocked` report and is the only evidence of the `tooling` reason category. A developer
  whose commit the tool blocked adds `uncommitted_files`, the non-empty, unique list of paths it
  left uncommitted, each normalized as Git prints it and inside its write zone.
- for a read-only role (architect, verification, code-review or qa) that left part of its brief
  undone: optional `incomplete_items`, one `{brief_item, reason, target_role}` per undone item, in
  English, each text non-empty and at most 1600 characters. `target_role` is the reporting role
  itself (for a narrowed retry) or a later role: architect → architect, developer, code-review or
  qa; verification → verification, code-review or qa; code-review → code-review or qa; qa → qa.
  An item a tool kept the role from also carries its own `tooling_blocker` of the shape above, on
  any outcome. A writing role never reports the field. A report with a non-empty list is never
  accepted automatically; a plain `accept` or `override-warning` of it is refused.

Optional `lessons` and `used_memory` are lists of non-empty strings; empty lists and omission are
valid. `lessons` records historical observations, never confirmed truth: memory indexes them only
with explicit `completion_report` source/path authorization, after sanitization, with status
“не подтверждено человеком” and the ordinary FTS ranking. `used_memory` lists the identifiers of
used hits as a weak signal: the coordinator neither resolves them nor uses them as an acceptance
gate, and memory never indexes them. These fields do not change required proof or ledger version.

For the high-risk triggers in the `code-review` manifest, Standards and Spec are separate
read-only reports. They must both be present before the coordinator accepts the batch; one combined
rating cannot replace either report.

Each `checks_run` entry uses the exact enum `pass | fail | not_run` as its `result`.
`passed`, `not-run`, translated spellings, and `not_run_architect_read_only` are invalid.
An architect work brief with no verification commands reports `checks_run: []`.
Neither `fail` nor `not_run` proves a successful check. Submission rejects invalid values with a
remedy before recording; correct the staging file and submit it again for the same dispatch.
Reports are validated again before a decision, so old non-canonical evidence is also refused.
Do not rewrite immutable evidence; recover an already recorded report or stopped batch through
the coordinator's existing human-approved recovery workflow.

Use this shape so missing proof is visible:

```markdown
# Completion report: <dispatch-id>

- Outcome: <completed|blocked|failed>
- Output: <what was produced>
- Commit SHA: <sha|not applicable — read-only role>
- Changed files: <exact paths|none>
- Checks run:
  - `<exact command>` — <pass|fail|not_run>, <evidence>
- Risks: <status: none|present; details: empty|residual risks>
- Blockers: <status: none|present; details: empty|blocker description>
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

A change outside the approved scope is a warning, not a rejection (issue #633). A completed
developer report whose `changed_files` lie outside the brief's `write_paths` is recorded by
`report submit`; `batch decision-packet` lists those paths as `scope_warnings`. Such a report is never
auto-accepted, plain `accept` is refused, and it is accepted only by `override-warning` with a note
other than `none` and an explicit `--approved-by`. The decision records `scope_warnings`, and the
override attaches a coordinator finding, so the next code-review brief carries the paths as an item
to settle. An architect `accept` with `--commit-plan-file` passes when the plan's `expected_paths`
lie outside the batch `allowed_paths`; the packet (with `--commit-plan-file`) and the decision record
the same `scope_warnings`. The worker's own ban on writing outside `write_paths`, the checkpoint
`changed_files` check and non-developer write roles stay strict.

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

`IntegrationCheckResult` defines the exact `passed / failed` results of an aggregate pair check.
It is distinct from the command-check enum `ReportCheckResult` (`pass / fail / not_run`): CI and
local-QA producers map their accepted or completed run explicitly to the integration enum. Both
linked input and stored results are validated; aliases, translations and whitespace are refused.

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
- `integration local-qa --record <id> --ci-condition absent|unavailable|unusable --reason <text>`
  (issue #536) is the fallback when combined-result CI cannot verify the pair. It pins an immutable
  request (pair, reason, full `verification_commands`), runs the gate runner in an isolated
  clean-room checkout of the exact candidate through the shared FIFO QA lane, and links
  `local-qa` evidence with `verification: verified` only while the pair is still current. It never
  edits the issue branch, the terminal source batch or accepted reports and never fixes code. A
  failed check is a retained finding (`state: failed`); infrastructure trouble is a separate
  `state: unavailable` that needs an explicit `--retry` and stops at `max_infrastructure_retries`
  (`state: exhausted`). Hand-linked `local-qa` stays `unverified`.
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

- `integration next --ticket T --branch B [--pull-request N]` (issue #537, ADR 0017) is the strictly
  read-only step of a PR continuation: it writes no Git, ledger, dispatch or PR and never asks the
  tracker. It classifies `status`, the evidence, open resolver batches and the budget events into
  `unavailable`, `resolver-open`, `refresh`, `route-failure`, `human-decision`, `confirm-pr`,
  `verify` or `handoff`. Old evidence permits entering PR preparation but is never QA of a new
  candidate. A failed check of the current pair (a collector-recorded CI failure or a failed
  generated local-QA gate, never an operational fallback) routes to the same resolver when the pair
  was refreshed or resolved (inside its two-cycle budget, which a human answer or a CI wait never
  resets) and to the regular developer with review and QA when the pair is the original one. The
  completed source batch is terminal (`batch decide` refuses it), so that route is a new batch of the
  same ticket and issue branch through the ordinary `/implement` pipeline, followed by `integration
  prepare --batch <new batch>`; the failed evidence of the earlier record stays history.
  `integration resolve` accepts such a failed check of a refreshed pair (`resolver.trigger:
  verification-failure`) even though the branch is already on the target. `collect-ci` results carry
  `next` (`wait` for a pending check, otherwise `local-qa` with the `--ci-condition` to use).
  `/to-pull-requests` asks the human for a separate confirmation bound to the exact candidate/target
  pair, verifies the opened PR with CI or the local-QA fallback, and hands over the verified pair
  and QA source for a manual merge; it never merges, and a newly detected target repeats
  actualization and verification.

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
