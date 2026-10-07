---
name: code-review
mode: read-only
required_capabilities:
  - code-review
risk_triggers:
  - api-public-contract
  - schema-change
  - data-migration
  - outbox
  - queues
  - message-schema-routing
  - transactions
  - authorization-security
  - concurrency-retry
  - retry-dlq
---

# Code review

Use this independent, read-only role as the mandatory two-axis review gate for every listed risk
trigger: API/public contracts, schema/data migrations, outbox/queues, transactions,
authorization/security, concurrency/retry, and retry/dead-letter queues. Other changes may use
this role after an explicit risk assessment. It must not change production code or integrate the
reviewed branch.

Review the diff and the Context Package, not the whole tree; an insufficient package is a blocker to
escalate, not a reason to walk the repository at large.

A deviation from the letter of the brief is a finding, not automatically a retry. When the
implemented form is functionally equivalent or safer than the one the brief named, report it as a
`warning` with that assessment stated, so the coordinator can record `override-warning` and keep
the candidate. Reserve a retry for a deviation that changes behaviour, scope or risk: a retry costs
the batch a developer-retry budget and, once the candidate is rebuilt, can land on a larger and
less reviewable diff than the one it replaced.

A brief whose `commit_plan_divergence` is not `null` records how the accepted initial or rebase
developer report diverged from its commit plan: merged commits, split entries, unclosed entries, and
the developer's justification. Check that every commit boundary is still independently reviewable
and that the justification holds; report a boundary that cannot be reviewed on its own as a
Standards `warning`.

A brief whose `carried_items` is not empty hands this review obligations an earlier decision
recorded, keyed by source: each `coordinator-finding` names a defect the coordinator found when it
accepted the developer report, with its `summary`, `files` and `expected_evidence`. The items'
files belong to the working set even outside `review_scope`. Account for every carried item, next to
the two axes, in `review.carried_items` as `{"item_id", "status", "evidence"}`: `closed` when the
candidate resolves it and the evidence shows that, `open` when the defect is still there, and
`unverified` when it could not be checked. Do not restate a carried item as an axis finding. An
omitted, `open` or `unverified` item keeps the report from being clean: it is never accepted
automatically, and a retry of a report with an `open` item routes to a developer retry.

An `incomplete-item` entry of `carried_items` whose `source.target_role` is `code-review` is a
brief item an earlier read-only role left undone for this review. Account for it in
`review.carried_items` like any carried item: `closed` when this review did the item, and
`unverified` when it did not, with the item listed again in `incomplete_items`. Do not mark it
`open`: `open` claims a code defect and routes the retry to a developer. A defect the item reveals
is an ordinary finding on its axis. An `incomplete-item` whose `source.target_role` is `developer`
reaches only a delta-review brief and follows "Delta-review after a fix-forward" below instead.
When part of this review's own brief stays undone, list each undone item in `incomplete_items`
under the common contract; its `target_role` is `code-review` (a narrowed review on the same
candidate) or `qa`.

When the approved verification cannot run at all (unavailable Bash/WSL wrapper, transport failure,
rate limit, context limit), report `outcome: blocked` with empty `findings` and severity `none` on
both axes, and state the operational cause in `blockers`. Never invent a finding to explain an
infrastructure stop: the coordinator routes such a report to a new review of the same candidate, and
routes any finding, warning or blocker severity to a developer retry.

The coordinator must not mark a high-risk batch complete until both the Standards and Spec reports
are present; a missing report is a blocker for the batch.

The completion report records the fixed diff and originating requirement as evidence, then returns
separate Standards and Spec findings with severity, residual risks, and blockers. The two reports
remain independent rather than being collapsed into one score. The completion report records no
production changes and no integration action for this read-only role.

For every approved verification command, run the installed bounded wrapper. A code-review work
brief may carry the project's focused `review_verification_commands`; this is independent proof,
not a substitute for the full QA gate that later receives `verification_commands`:

```bash
python .harness/skills/qa-gate/scripts/test_summary.py -- bash -lc '<approved command>'
```

Keep the original approved command (not the wrapper invocation) in `checks_run`; use the wrapper's
bounded summary as its evidence. A failing summary gives the sanitised local log path for the
specific diagnostic; do not paste raw passing output into the review report.

## Delta-review

A dispatch brief carrying `delta_review_of` (a prior, retried code-review dispatch id) and
`delta_review_axis: spec` is a delta-review: the coordinator has already verified that the prior
Standards verdict was Clean, the prior Spec verdict was Warning or Blocker, and the fix diff since
that candidate touches only test/fixture paths and matches none of this role's risk triggers.
Standards must be reported exactly as inherited — `severity: clean`, `findings: []`, and
`inherited_from` set to `delta_review_of`; Spec is always analysed again. Any production, security,
schema, or public-contract change requires a full independent review. This dispatch is always a new,
independent session: it never resumes the prior review's session.

## Delta-review after a fix-forward

A code-review brief without `delta_review_of` may carry `delta_review_scope`: the coordinator found
that the candidate comes from an accepted `fix-forward` or `rebase-fix-forward` developer-retry on a
candidate an earlier review of the batch judged, and chose the review's scope itself. The section
names the prior review (`prior_review`: dispatch id, `report_sha256`, reviewed candidate, base and
risk assessment), the developer-retry, `delta_base`, the `delta_commits`, the `reviewed_copies` and
the retry's `closure`.

With `mode: delta`, the new commits match no risk trigger the prior review did not see, change no
file outside the carried items, and every rebased copy kept its original's `git patch-id`. Review
both axes on `git diff <delta_base> <candidate_commit>` only, plus the closure of every carried
item; the prior review's report is the evidence for the rest of the candidate, so do not review that
again or restate its findings. A copy listed in `reviewed_copies` is already reviewed. Keep
`review.scope` equal to the brief's full `review_scope`, and report both axes with your own severity
and findings, never `inherited_from`. In delta mode `carried_items` also holds the prior review's
`review-finding` items and the developer's `incomplete-item` entries the fix-forward closed. Account
for each one in `review.carried_items` like any carried item: `closed` when the delta closes it as
its `expected_evidence` asks (the `closure` names the closing commits), `open` when the defect is
still there or the developer's item is still undone, and `unverified` when it could not be checked.
Unlike an item handed to this review, a developer's `incomplete-item` may be `open`: the
developer, not this review, owes it, and its retry goes back to a developer. A defect the delta
adds is an ordinary finding on its axis. A retry of this review hands every item it did not mark
`closed` to the next developer-retry, together with its own findings.

With `mode: full`, the section lists the `escalations` that ruled a delta out (`new-risk-trigger`,
`file-outside-carried-items`, `patch-id-mismatch` or `no-new-commits`, each with its evidence).
Review the whole candidate as an ordinary full review; the section is audit evidence only and adds
no carried item.
