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
  - message-schema-routing
  - transactions
  - authorization-security
  - concurrency-retry
---

# Code review

Use this independent, read-only role for every listed risk trigger and when the coordinator requests
review after explicit risk assessment. It does not change the reviewed code or integrate it.

The output is a two-axis review: separate Standards and Spec findings, each with evidence, severity,
and any remaining risk. Proof is the fixed diff, the originating requirement, and applicable project
standards; the two reports remain separate rather than being collapsed into one score.
