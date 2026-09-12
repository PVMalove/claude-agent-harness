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
