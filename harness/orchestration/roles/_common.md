# Common role contract

Every role receives an immutable handoff brief from the coordinator. The brief names the ticket,
declared backend zone, acceptance criteria, prohibited changes, issue branch, worktree, and required
verification commands. A role does not amend the brief; material new information is escalated for a
new coordinator decision.

Each role returns one completion report: its output, changed files, checks run and results, residual
risks, and blockers. A write role also reports the commit SHA that contains its work. The coordinator
records the selected provider and model separately; manifests never choose either.

Write work happens only on the handoff's issue branch and isolated worktree, only inside the declared
zone. Protected branches and `integration/*` are never direct write targets. A batch has one active
writer; role handoffs are sequential. A commit is evidence only after the required checks pass and its
SHA is included in the completion report.

Escalate instead of guessing when the requested zone is unclear or overlaps another batch, required
proof cannot be produced, a risk trigger applies without a stated gate, or the work needs credentials,
an irreversible action, or a policy decision. A blocked or failed attempt is not retried in place: the
coordinator creates a new dispatch with a new immutable brief.
