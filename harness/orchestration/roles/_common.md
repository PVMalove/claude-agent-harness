# Common role contract

Every role receives an immutable handoff brief from the coordinator. The brief names the ticket,
declared backend zone, acceptance criteria, prohibited changes, issue branch, worktree, and required
verification commands. A role does not amend the brief; material new information is escalated for a
new coordinator decision.

Use English for all agent-to-agent protocol text: handoff notes, checkpoints, state evidence,
dependency explanations, and messages to the next worker. Treat `.harness/orchestration/state/` as a
machine-readable audit trail and keep any free-text coordination fields in English. Completion
reports addressed to the coordinator must be written in Russian and include `"report_language": "ru"`.
Do not translate commands, paths, commit IDs, test names, or quoted source evidence.

Write the completion report, and any checkpoint, to the absolute `report_staging_path` named in the
brief (`.harness/scratch/inbox/<dispatch_id>.json`) and hand that same path to
`report submit --file`. Never resolve a relative reporting path against the current directory and
never invent a location of your own: a payload written outside the repository or its worktrees —
a home-directory folder, the system temp — is rejected, and the coordinator cannot find evidence
that is not in the project.

Each role returns one completion report: its output, changed files, checks run and results, residual
risks, and blockers. A write role also reports the commit SHA that contains its work. The coordinator
records the selected provider and model separately; manifests never choose either.

Start with the Context Package and one startup probe: run `git rev-parse --show-toplevel`, `git branch
--show-current`, and `git rev-parse HEAD` in the runtime's current directory. Report that canonical
worktree through `dispatch self-report --dispatch <dispatch_id> --model <model> --worktree <top-level>` when the project requires worker
attestation. This is the only startup discovery needed before role-specific files; after the probe,
work from the package rather than navigating to a guessed relative repository path.

The Context Package's `starting_files`, `symbol_graph`, and `related_tests` are the working set for
the role's task: read those first. Repository search is scoped to Context Package paths and reserved
as a last resort, not the default way to build understanding. When the package is insufficient — a
needed file or symbol is missing from it — escalate a blocker naming that file or symbol rather than
reading the repository blindly. This is a working discipline, not a dispatch-creation gate: Context
Package freshness enforcement at `dispatch create` stays in shadow mode, gated by its own separate,
explicitly authorized pilot.

Evidence stays bounded: a command's full output never returns to the model's dialogue, only a
truncated summary. Read a long log through the existing
`python harness/orchestration/advisory.py summarize-log --file <log>` rather than in full, read files
in ranges, and re-run a failing test only by its specific node id, never the whole suite. A write role
with iterative TDD (`developer`, `database-migrations`, `messaging-integration`) that exceeds a
planned trigger (TDD-cycle volume or accumulated log volume) brings the work to a natural boundary,
commits, and requests a checkpoint instead of continuing in a bloated session. A read-only role
(`architect`, `qa`, `code-review`) never spans a dispatch across worker sessions this way; it keeps
its own output bounded by the same means above and, if genuinely exceeded, escalates a blocker
instead.

Write work happens only on the handoff's issue branch and isolated worktree, only inside the declared
zone. Protected branches and `integration/*` are never direct write targets. A batch has one active
writer; role handoffs are sequential. A commit is evidence only after the required checks pass and its
SHA is included in the completion report.

Escalate instead of guessing when the requested zone is unclear or overlaps another batch, required
proof cannot be produced, a risk trigger applies without a stated gate, or the work needs credentials,
an irreversible action, or a policy decision. A blocked or failed attempt is not retried in place: the
coordinator creates a new dispatch with a new immutable brief.
