# Reconstruct decision history

Reached only from `SKILL.md`'s optional "Reconstruct decision history" phase, after the owner has
opted in. Recovers the architectural decisions embedded in a codebase's history — not just the
practical facts Audit already gathered — and writes them up as ADRs, graded by how well the evidence
actually supports each one.

## Investigate, don't transcribe

Pull from whatever sources actually exist for this repository; skip a source that doesn't apply
instead of guessing at it, and say so in the report rather than silently omitting it:

- **Agent session history**, if any is reachable — local transcripts, a team-shared session log. Most
  repositories won't have this; that's a normal, reportable absence, not a gap to fill in.
- **Pull/merge request history** — title, description, review discussion, proposed alternatives, and
  the actual merged diff — through whatever the repository's host actually is (`gh` for GitHub, `glab`
  for GitLab, or the platform already in use). Read local git history directly when there is no host
  to query.
- **Current code**, used only to confirm a reconstructed decision is still actually reflected — never
  as the source of *why* a decision was made.

Read-only throughout. No code changes, no new files, no commits — including ADR files — until the
owner has seen the closing report and separately authorized which ones to write; this mirrors Audit's
own read-only rule and this skill's standing rule of confirming before durable writes.

For a repository with a long history, forking a sub-investigation per source type (sessions vs.
PR/MR history) is reasonable. Let each fork's result return as its own notification — never poll or
schedule a wakeup to wait on one that's already running.

## Evidentiary discipline

A proposal discussed in a session, or an idea raised in review, is not evidence that it was decided —
only that it was considered. Weigh sources by strength, strongest first:

1. **Current code / merged diff** — what actually shipped.
2. **The merge/close outcome of a PR or MR** — including review comments that changed the outcome.
3. **A session's proposal or discussion** — weakest; shows a path considered, not a path taken.

When sources agree, cite the strongest one. When they conflict — a session proposed A, the merged
diff shows B — the merged diff/current code wins: record B as the decision, and record A as
considered and not adopted. When two comparably strong sources conflict and neither settles it, do
not guess; report the contradiction and name which source has the stronger claim, in the
Contradictions section of the closing report.

## What to reconstruct

Components and their responsibilities, how they interact, key abstractions and interfaces, data
flows, dependencies, constraints, and conventions binding future work. Call out, separately, any
decision specific to AI agent / orchestration / tool / memory / session / RAG / LLM integration
architecture, when the repository has any.

## When a decision earns an ADR

Not every change. All three must hold:

1. **Hard to reverse** — changing course later has a real cost.
2. **Surprising without context** — a future reader would wonder why it's built this way.
3. **A real trade-off** — genuine alternatives existed and one was chosen for specific reasons.

If the repository already has an ADR or decision-record convention (a `docs/adr/` directory, another
location, its own template), reconstruct into *that* convention — its location, numbering, and shape
— rather than importing a foreign one; add Evidence and Confidence as extra fields on top of it only
if that template doesn't already carry equivalent rigor. Otherwise, default to `docs/adr/NNNN-slug.md`,
sequential, scanning for the highest existing number.

## Confidence

Grade every decision:

- **CONFIRMED** — history, a merged PR/MR, review, or the current code directly supports it.
- **INFERRED** — a conclusion drawn from combining sources, none of which states it outright.
- **UNKNOWN** — asserted somewhere, but no source actually supports it.

Never promote a guess to a fact. When the reason behind a decision genuinely cannot be found, write
"Reason not established from available sources" rather than inventing one.

## ADR template

```md
# {ID}. {Title}

**Status:** {accepted | superseded by ADR-NNNN | ...}
**Confidence:** {CONFIRMED | INFERRED | UNKNOWN}

## Context
{What situation or problem this decision responds to.}

## Decision
{What was actually decided — the current, adopted state.}

## Alternatives considered
{What else was on the table, and why it wasn't chosen — omit if none surfaced.}

## Why
{The reasoning behind the decision, or "Reason not established from available sources."}

## Consequences
{Non-obvious downstream effects — omit if there aren't any worth naming.}

## Evidence
{Which sources support this: session(s), PR/MR #, review comment, diff, current implementation —
named specifically enough that someone could go re-check them.}
```

Adapt field names to the repository's own template where one already exists; the content above is
what must be present, not a fixed heading format. Write ADRs in the repository's existing
documentation language; ask if it shows no clear signal.

## Closing report

Before writing anything to disk, report:

- **Executive summary** — the reconstructed architecture in brief.
- **Architecture decisions found** — the list, independent of the ADRs drafted for them.
- **Draft ADRs** — the full set, ready to write once authorized.
- **Rejected approaches** — what was considered and why it lost, where that's known.
- **Contradictions** — where sources disagree and neither settles it.
- **Unknowns** — what can't be reconstructed from what's available.
- **Recommendations** — decisions or areas worth documenting going forward because they're
  currently under-recorded.

Write only the ADRs the owner explicitly authorizes from this report, into the location and format
resolved above.
