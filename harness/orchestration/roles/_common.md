# Common role contract

Every role receives an immutable handoff brief from the coordinator. The brief names the ticket,
declared backend zone, acceptance criteria, prohibited changes, issue branch, worktree, and required
verification commands. A role does not amend the brief; material new information is escalated for a
new coordinator decision.

Before the first English handoff, every worker must read
[Technical English](../../docs/technical-english.md).

Use English for all agent-to-agent protocol text: handoff notes, checkpoints, state evidence,
dependency explanations, and messages to the next worker. Treat `.harness/orchestration/state/` as a
machine-readable audit trail and keep any free-text coordination fields in English. Completion
reports addressed to the coordinator must be written in Russian and include `"report_language": "ru"`.
Do not translate commands, paths, commit IDs, test names, or quoted source evidence.

Write the completion report, and any checkpoint, to the absolute `report_staging_path` named in the
brief (`.harness/.sandboxes/scratch/inbox/<dispatch_id>.json`) and hand that same path to
`report submit --file`. Never resolve a relative reporting path against the current directory and
never invent a location of your own: a payload written outside the repository or its worktrees —
a home-directory folder, the system temp — is rejected, and the coordinator cannot find evidence
that is not in the project.

Each role returns one completion report: its output, changed files, checks run and results, residual
risks, and blockers. A write role also reports the commit SHA that contains its work. The coordinator
records the selected provider and model separately; manifests never choose either.

Start with the Context Package and one startup probe: run `git rev-parse --show-toplevel`, `git branch
--show-current`, and `git rev-parse HEAD` in the runtime's current directory. Report that canonical
worktree through `dispatch self-report --dispatch <dispatch_id> --model <actual-active-model>
--worktree <top-level>` before task work in every new or resumed session. Model self-report is always
required; `worker_attestation_required` controls the additional worktree check. Use the coordinator
CLI and its absolute repo/state paths from the prompt even when your current directory is another
worktree. After a successful self-report, send `dispatch heartbeat --dispatch <dispatch_id>`
immediately and at least every `liveness.heartbeat_every_seconds` from the brief while working.
Escalate a mismatch or unavailable model identity instead of copying the expected model as evidence.
This is the only startup discovery needed before role-specific files; after the probe,
work from the package rather than navigating to a guessed relative repository path.

Before a final reply, submit the JSON completion report with `report submit --file
<report_staging_path>` and verify that it was recorded. Chat text alone does not complete a dispatch.
If submission fails before recording, preserve the staged report and relay the command and error as
a blocker. A result with `completion` means the report is already recorded: relay that result to the
coordinator for `report complete`, rather than submitting again. A planned checkpoint is the separate
continuation protocol, not a completion report.

The Context Package's `starting_files`, `symbol_graph`, and `related_tests` are the working set for
the role's task: read those first. A starting file with non-empty `sections` is a large document
seeded as a section index: read only the `start_line`–`end_line` ranges the task needs, not the
whole file. Repository search is scoped to Context Package paths and reserved
as a last resort, not the default way to build understanding. When the package is insufficient — a
needed file or symbol is missing from it — escalate a blocker naming that file or symbol rather than
reading the repository blindly. This is a working discipline, not a dispatch-creation gate: Context
Package freshness enforcement at `dispatch create` stays in shadow mode, gated by its own separate,
explicitly authorized pilot.

Project-memory precedents come only from the supplied Context Package. Roles must not invoke
`harness memory search`, the installed `memory/search_cli.py`, or the Python memory-search API.
An available Bash tool or installed memory resource grants no exception. Escalate a blocker naming
the missing ADR or precedent to the coordinator instead.

Evidence stays bounded: a command's full output never returns to the model's dialogue, only a
truncated summary. Read a long log through the existing
`python .harness/orchestration/advisory.py summarize-log --file <log>` rather than in full, read files
in ranges, and re-run a failing test only by its specific node id, never the whole suite. A write role
with iterative TDD (`developer`, `database-migrations`, `messaging-integration`) that exceeds a
planned trigger (TDD-cycle volume or accumulated log volume) brings the work to a natural boundary,
commits, and requests a checkpoint instead of continuing in a bloated session. A read-only role
(`architect`, `qa`, `code-review`, `verification`) never spans a dispatch across worker sessions
this way; it keeps its own output bounded by the same means above and, if genuinely exceeded,
escalates a blocker instead.

Context pressure is measured by the provider or runtime, never by your own estimate. When the
coordinator records a `critical` observation for your dispatch, a write role finishes the current TDD
cycle to a green boundary (every approved verification command passing), commits and requests a
checkpoint; a role that cannot reach one returns a structured blocker (`outcome: blocked`) that names
the context pressure. A continuation starts only from that checkpoint, in a new session that attests
its model again. Recording pressure changes no routing or approval by itself.

Write work happens only on the handoff's issue branch and isolated worktree, only inside the declared
zone. Protected branches and `integration/*` are never direct write targets. A batch has one active
writer; role handoffs are sequential. A commit is evidence only after the required checks pass and its
SHA is included in the completion report.

Never work around a hook, the safety classifier, the ledger or another tool that blocks a legitimate
action: do not repeat the blocked action in another command form, through another tool, a script
file, `eval` or another interpreter, or by splitting the command. Following the remedy the tool
itself names (such as the bounded summary wrapper) is not a workaround. Stop instead and return
`outcome: blocked` with a `tooling_blocker` of exactly three non-empty strings: `tool` (the hook or
tool that blocked, `safety-classifier` for an interruption by the safety classifier), `command`
(the exact command or action as it was invoked) and `message` (the tool's verbatim message, bounded
like check evidence). Only this field lets the coordinator classify the stop as `tooling`; free
text in `blockers` never does. Report a check the block kept from running as not run, never as
`fail`.

A read-only role (`architect`, `verification`, `code-review`, `qa`) that leaves part of its brief
undone lists each undone item in the completion report's `incomplete_items`, not in `blockers`:
`{"brief_item", "reason", "target_role"}`, in English, because the item reaches a later brief as
agent-to-agent protocol text. `brief_item` names the item as the brief states it, `reason` says why
it is undone, and `target_role` names the role that can finish it: the reporting role itself, or a
later role its own contract lists. When a tool kept the role from an item (for example, the safety
classifier interrupted the action), the item also carries its own `tooling_blocker` of three
non-empty strings, `tool`, `command` and `message`, under the rules above. Report the work that is
done as usual; the report is never accepted automatically while it lists an item. A writing role
never reports `incomplete_items`.

A brief's `carried_items` may hold `incomplete-item` entries; every role, developer included,
follows the same receiving rule. When an item's `source.role` is the role's own role (`source.route`
`narrowed-retry` or `tooling-retry`), the dispatch is a narrowed retry: do only the carried items,
and do not repeat the rest of the assignment. Otherwise, do each carried item within the ordinary
assignment. In both cases, name each `item_id` in `output` with the evidence that it is done, as its
`expected_evidence` asks. A read-only role lists an item that is still undone in `incomplete_items`
again; a writing role reports it as a blocker. A developer-retry whose brief carried items maps them
in `carried_item_closure` instead, as the developer contract describes.

Escalate instead of guessing when the requested zone is unclear or overlaps another batch, required
proof cannot be produced, a risk trigger applies without a stated gate, or the work needs credentials,
an irreversible action, or a policy decision. A blocked or failed attempt is not retried in place: the
coordinator creates a new dispatch with a new immutable brief. A developer retry starts from the
compact handoff in its prompt, the playbook's "Developer-retry handoff", as its only record of the
earlier attempt; the previous session's raw history never carries over.
