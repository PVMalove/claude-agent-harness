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

The coordinator must not mark a high-risk batch complete until both the Standards and Spec reports
are present; a missing report is a blocker for the batch.

The completion report records the fixed diff and originating requirement as evidence, then returns
separate Standards and Spec findings with severity, residual risks, and blockers. The two reports
remain independent rather than being collapsed into one score. The completion report records no
production changes and no integration action for this read-only role.

For every approved verification command, run the installed bounded wrapper:

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
