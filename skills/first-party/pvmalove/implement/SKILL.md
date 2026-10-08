---
name: implement
description: "Coordinate one backend ticket through the approved architect, developer, review, QA, and publish handoffs."
disable-model-invocation: true
---

# Implement

**Objective:** Coordinate exactly one ticket through `architect → developer → code-review → qa → publish`, then offer `/to-pull-requests`. This session **is** the coordinator: it creates and observes dispatches but never implements the ticket itself.

Language contract: agents communicate with each other and write free-text protocol/state evidence in
English. Every completion report addressed to this coordinator is in Russian and includes
`"report_language": "ru"`; preserve commands, paths, IDs, and quoted evidence verbatim.
This binds the text this session writes, not only the text it reads: translate the ticket into
English before it reaches a batch or a worker prompt. `definition_of_done` and `prohibited_changes`
are rejected outright when they carry Cyrillic, and a worker prompt carrying the untranslated
ticket body is the same contract violation the coordinator cannot see.

## Route

This is the opt-in `backend-orchestration` route. Confirm that its installed coordinator is usable:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch status
```

If the command is unavailable or cannot read its state, stop and tell the developer to run `/fast-implement` instead.
A `ledger_busy` answer is not unavailability: another coordinator operation holds the ledger lock,
so repeat the command after its `retry_after_seconds`.
Do not infer or repair an opt-in capability. Process one ticket to a terminal batch state before
beginning another.

If another batch holds the same unfinished ticket, branch or worktree, or the `concurrency_budget`
is exhausted, inspect `batch list --open`, the conflicting dispatch,
and its decision packet. Tell the developer which batch is blocking and how to finish it normally.
If its worker can no longer produce a report, show a copyable `batch abandon` command with the
actual batch ID, the verified operator name, `--approved-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"`,
and a reason grounded in the observed failure. Present this as an operator action requiring their
explicit approval; never supply `--approved-by` as though they already approved, or execute the
command for them without that approval. `ledger clean` only removes orphaned evidence, and
`ledger reset` does not clear an active batch.

## Coordinator contract

Resolve the tracker ticket, issue branch and blockers without opening a second batch for the same
work. Ticket status is part of that pre-flight, before `batch create` and before the issue branch
exists: check the ticket's blockers whatever its current label (any still open → stop, name them,
and make sure it carries `status::blocked`); otherwise replace its `status::*` label with
`status::in-progress` exactly as `/fast-implement` Phase 1 steps 2–3 do, and confirm it is the
only `status::*` label. A failed label write is a blocker, not a warning. Before `batch create`, run the batch-level preflight with bounded expected files, services and
diff size. Pin the writer's explicit scope with `--allowed-path` (repeat it; a path or glob inside
the developer's write ceiling): batches with overlapping files run in parallel in their own
worktrees, and a change outside the scope is rejected. If it rejects the ticket, split it with `/to-tickets`; never ask an architect to discover
whether an oversized ticket should have been split. When creating the batch with `batch create`,
specify `--required-gate review --required-gate qa`: the full implement pipeline explicitly sets
both independent Standards/Spec review and serialized clean-room QA gates. Before dispatching the
write-role (`developer`) worker, verify that these mandatory gates are recorded in the batch's
`required_gates`; halt early if missing rather than sending a worker into a risk-gate omission.

For each handoff, run `dispatch preflight`, show `batch decision-packet`, then create an approved
immutable brief. Pass the brief's `report_staging_path` to the worker verbatim; a role that has to
guess where its report belongs writes it outside the project. The coordinator never writes feature code or repairs state by hand. A report is
evidence, not permission to advance. Architect precedes developer; accepted candidate proceeds
through the required review/QA/publish gates. A write-role worker stopped early before making changes
returns a truthful `outcome: blocked` report bound to the verified checkout commit with empty
`changed_files` and unrun checks; the coordinator decision packet returns an explicit recovery route
(`retry`, `block`, `abandon`) without registering a candidate or weakening `completed` report validation.

Before every write-role dispatch, the immutable brief carries an ordered commit plan. Each entry
names one independently reviewable logical change, its expected files and the DoD items it covers;
use one entry only when the entire approved change is inseparable. The coordinator derives one entry
per DoD item; to use the architect's plan instead, pin it on the architect accept with
`batch decide --decision accept --commit-plan-file <path>`. A recovery preserves the accepted plan,
or replaces it with a newly approved plan that explains the changed boundary. The developer's
completion report maps every created commit to the entries it closes; a mapping that is not
one-to-one needs `dod_coverage` and `divergence_justification`, and a report with a `not_covered`
item can be accepted only by `override-warning` with a note other than `none`, or returned with
`retry`. A report whose `changed_files` lie outside the brief's `write_paths` is recorded with a
`scope_warnings` entry and is accepted only the same way. Do not collapse unrelated implementation, tests, documentation, or type-only repairs into a
recovery commit merely because they are staged together.

Follow the configured approval policy. Under `manual_all`, every transition needs explicit approval:
show the decision packet, ask, and wait. Under `low_risk`, a clean completed report of a batch whose
allowed paths lie inside `low_risk_paths` is accepted by the coordinator with an audited policy decision, and the next eligible dispatch
may already be approved. Under `milestone`, clean reports outside QA, publish and risk milestones
are also accepted automatically; stop for the remaining milestone decisions. Continue from the
recorded `next_action` without asking the operator to repeat a policy decision. Blockers, failed
checks, risk triggers, review findings and publish still require the applicable manual decision.
Never write `--approved-by` on the operator's behalf or
narrate a decision they did not make. `human_approval_gate: tty` requires confirmation on the
operator's terminal for transitions that still require human approval. When choosing a recovery
route for `retry` or `abandon`, show the decision packet's `route_preview` (with the
`--reason-category` or `--retry-role` you intend to pass to `batch decide`) and follow the
Recovery route table in `.harness/orchestration/playbook.md` (situation → route → who approves →
evidence).

A worker that works around a hook or tool block (another command form, tool, script file, `eval`,
interpreter or a split command) breaks the protocol: never accept or warning-override that report,
and record the violation in `--note`. For an architect or developer (publish included), decide
`retry` with a developer reason category (not `tooling`) or `block`. For a read-only code-review, qa
or verification role, pass `--reason-category block-bypass` to `batch decision-packet` and, once its
`route_preview.retry.route` is `bypass-rerun`, to `batch decide --decision retry`: the same stage
re-runs on the same SHA with no new candidate and no developer retry spent, and its new dispatch
always needs explicit approval. A report that stops with `tooling_blocker` instead is confirmed
before its retry: check that its `command` is legitimate under the brief (allowed paths and tool policy) and
that its `message` refuses that command. For a false positive, file or reuse a bug ticket against
the tool through the tracker CLI (tool, command, message, dispatch ID), name the ticket in `--note`,
and run `batch decide --decision retry` once `route_preview.retry.route` is `tooling-retry`; it
re-runs the same stage on the same SHA (a developer continues its last commit) and spends no
`retry_policy.max_developer_retries`. A command that is not legitimate is no false positive: retry
with the developer reason category its evidence supports. Resolve the `tooling-retry-repeated`
attention, raised by the third consecutive tooling retry on one candidate, only once the tool is
fixed.

When you find a defect in a clean developer report whose DoD is met inside its allowed paths, do not retry
it: accept it with `batch decide --findings-file <path>`, or after a policy auto-accept run
`batch carry-over --batch <id> --findings-file <path>` before its code-review dispatch exists. The
finding travels into the code-review brief as a carried item (route `carry-over`), and the one
developer retry is spent after review. Retry a developer report without accept only for an unmet
DoD item or an out-of-scope change.

Each worker records a model self-report and is observed by the event-driven watchdog; those facts
are evidence, never a reason to edit an immutable brief.

Use the brief's shared Context Package ID across architect, developer and resumed worker sessions
when the pinned base/candidate is unchanged. Read only its starting files and directly referenced
symbols before escalating. A changed candidate creates one new shared package for its review, not a
role-specific duplicate. Continuations and retries stop at the project budget; a 429 is not an
exception to that budget.

Run `dispatch wait` between send and report. `stale`, model/worktree mismatch, changed harness
snapshot, and a failed deterministic gate are blockers for the coordinator, not prompts for broad
LLM recovery. In-process and external transports preserve the same brief and evidence contract.

A busy ledger (`ledger is locked by another operation`, or `ledger_busy` from `dispatch status`) is
transient: repeat `dispatch wait` or `dispatch status`. When a relayed `report submit` result
carries `completion`, the report is already recorded: never ask the worker to submit it again. Run
its `command` (`report complete --dispatch <dispatch-id>`) yourself and repeat it until no step
fails; it is idempotent. When the failed step needs a human, follow its remedy instead. Never remove
the ledger lock or any state file by hand; a lock that stays held goes to
`coordinator.py --repo . ledger release-lock`, which refuses a lock whose owner process is alive.

## Worker prompt

Every worker prompt is this template, filled from the `dispatch send` result and the CLI invocation
used to send it. The brief and Context Package carry the task; the prompt also carries the mandatory
lifecycle. Fill `<coordinator CLI>` with the Python executable, absolute `coordinator.py` path and
absolute `--repo` (plus `--state-dir` when supplied) used for that dispatch. Quote paths for the
worker's shell. Keep this same ledger address even when the worker runs in another worktree.
Copy the heartbeat interval from `dispatch send.heartbeat.every_seconds` verbatim.

```text
You are the <role> worker for dispatch <dispatch_id>.
Brief: <brief path from dispatch send>
Report staging path: <report_staging_path from dispatch send, verbatim>
Coordinator CLI: <coordinator CLI>
Before task work, run git rev-parse --show-toplevel, git branch --show-current and git rev-parse HEAD
in your runtime's current directory. Confirm your actually active model and the probed Git top-level:
<coordinator CLI> dispatch self-report --dispatch <dispatch_id> --model "<actual active model>" --worktree "<probed Git top-level>"
Proceed only after a successful self-report; escalate a mismatch or unavailable model identity.
Immediately after self-report and at least every <heartbeat.every_seconds> seconds while working, run:
<coordinator CLI> dispatch heartbeat --dispatch <dispatch_id>
Context Package <context_package_id>: start from its starting_files, symbol_graph and
related_tests. For a starting file with non-empty sections, read only the start_line–end_line
ranges the task needs.
Work within the brief; escalate a blocker for anything the brief and the package leave out.
Before your final reply, write the completion report JSON to the exact report staging path using
the common and role-specific report contract, with report_language: ru, then run:
<coordinator CLI> report submit --file "<report_staging_path>"
Completion means the report is recorded in the ledger. Include the submit result in your final reply.
If submission fails before recording, return the command and error as a blocker; keep the report file.
If the result includes completion, relay it to the coordinator for report complete; the report is
already recorded, so submit it only once. Chat text alone does not complete the dispatch.
```

A developer retry adds two lines from the `retry_start` of its `dispatch preflight`:

```text
Retry handoff: <retry_start.handoff, verbatim JSON>
Retry starting files: <paths from retry_start.starting_files>
```

Use this lifecycle for every new or resumed worker session, including developer-retry. A continuation
that stops at a checkpoint follows the checkpoint protocol instead of submitting a completion report.
The coordinator treats a final reply without recorded completion or a valid checkpoint as an
unfinished handoff and requests the missing protocol from the same available worker session.

Large documents (the role catalog, `playbook.md`, `backend-orchestration.md`, `git-workflow.md`) and
prior reports reach a worker only as Context Package section ranges or through the retry handoff.
The work a prompt asks for is exactly its brief plus, for a retry, the handoff's blocking findings.

A developer retry is always a new session started from its compact handoff, never a continuation of
the previous developer session's history. The preflight's decision packet carries the retry's
`retry_context_estimate` and `retry_context_warning` as evidence; they never gate the dispatch. Code-review and QA stay new
independent sessions; only their prompt follows this template. The playbook's "Developer-retry
handoff" defines the handoff, the smart-zone threshold and the compact.

## Authoritative guidance

This is a short coordinator contract, not a second orchestration manual. Full rules are module-owned guidance:

- `.harness/orchestration/playbook.md` owns lifecycle, authority, immutable brief, completion
  evidence, parallelism, and metric rules.
- `.harness/orchestration/roles/` owns each role's boundary, required proof, and specialist trigger.
- `.harness/docs/backend-orchestration.md` owns setup, project configuration, CLI procedure, and
  operational recovery.
- `docs/agents/git-workflow.md` owns issue-branch, commit, push, and PR boundaries.

Follow those files rather than duplicating or weakening their rules here. In particular, do not
invent token metrics: use only provider- or runtime-observed telemetry and preserve missing-data
notes.

## Wrap-up

After an accepted publish, record the integration link before the branch is merged or deleted:
`python .harness/orchestration/coordinator.py --repo . integration prepare --ticket "#<ID>" --branch "<issue-branch>"`
(add `--batch <batch-id>` when several batches published the branch). It writes only an immutable
integration record and is safe to repeat; if it refuses, report its remedy to the developer and do
not work around it. Never edit the completed batch or its reports by hand.

Then offer `/to-pull-requests <ticket>`. Do not invoke it automatically, open
or merge a PR, write to an integration branch, or close the ticket in this skill. Leave
`status::in-progress` on the ticket: closing it and moving its unblocked dependents to
`status::ready` belong to `/to-pull-requests` after the merge.
