# `triage` speaks a namespaced `workflow::*`/`hitl`/`afk` taxonomy; epic grouping is a native sub-issue link, not a label

`triage` (`skills/first-party/pvmalove/triage/`) uses a first-party state model instead of the upstream canonical five roles, because the canonical model can't express two things this repo needs: that a ticket is *currently being worked on* (`ready-for-agent`/`ready-for-human` have no notion of "in progress"), and that an epic is still being scoped rather than ready to pick up (both get flattened into the same `ready-for-*` state as an actionable ticket). The taxonomy:

- **Category** — `bug`/`enhancement`, unchanged from upstream.
- **Workflow state** (`workflow::*`) — `specs` (epic/large task still being decomposed, too early for development), `ready` (fully specified, pickable), `in-progress` (an active session has it), `blocked` (a dependency, or missing info from the reporter).
- **Execution mode** — `hitl`/`afk`, orthogonal to workflow state; not required while an issue carries `workflow::specs`, since execution mode isn't decided until decomposition.
- **Context labels** — `out-of-scope` (this project's `wontfix`, applied by `/triage` at close), `task-report::required` (see below).

Epic grouping is **not a label**. A ticket decomposed from an epic is linked to it as a native GitHub sub-issue — the same mechanism `/wayfinder` already uses for its map/ticket relationship (`docs/agents/issue-tracker.md#wayfinding-operations`). `/to-spec` applies `workflow::specs` to a freshly published epic issue; `/to-tickets` links each decomposed ticket to it as a sub-issue and applies `workflow::ready` + `hitl`/`afk` (or `workflow::blocked` if the ticket is gated by another still-open ticket from the same decomposition).

`task-report::required` is not part of `triage`'s state machine — it's `/implement`'s behavior. `/to-spec` and `/to-tickets` apply it by default when they create a ticket; `/implement` posts a completion-report comment on the issue (for a GitHub/GitLab ticket, when it opens the PR — `/implement` doesn't close the issue itself, that happens on merge) or folds the summary into the local tracker's `**Workflow:** done` update, unless told to skip it.

`/to-tickets` sets `workflow::blocked` on a ticket gated by another open ticket in the same decomposition; `/implement` is the one that checks and clears it, at the moment it starts work on that specific ticket — not `triage`, and not a separate sweep pass over the decomposition. `/implement` also sets `workflow::in-progress` once it actually starts working a ticket.

**Rejected**: layer `workflow::*`/`hitl`/`afk` as context labels on top of an untouched vendor `triage`, the way `task-report::required`/`out-of-scope` sit outside the required axes. Rejected — workflow state is exactly what `triage`'s own state machine manages; a label bolted on the side would leave two parallel state machines (the canonical one inside `triage`, `workflow::*` outside it) instead of one that speaks the taxonomy natively.

**Rejected**: keep a separate `epic::<slug>` label alongside `workflow::specs` (one for stage, one for group membership). Rejected in favor of a single grouping pattern for the whole repo — the same native sub-issue link `wayfinder` already uses, with no duplicate label-per-epic to create ad hoc.

**Rejected**: replace the category pair (`bug`/`enhancement`) with a `type::*` axis. Rejected — the problem this taxonomy solves is pipeline-state visibility, not category; the canonical pair already does its job.

**Rejected**: have `triage` sweep the decomposition to clear `workflow::blocked` once its blockers close. Rejected in favor of checking at the moment `/implement` starts work on a specific ticket — no separate step that something has to remember to run, and the check happens exactly when it's needed.
