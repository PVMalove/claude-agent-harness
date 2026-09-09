---
name: developer
mode: write
required_capabilities:
  - backend-development
risk_triggers:
  - api-public-contract
  - transactions
  - authorization-security
  - concurrency-retry
---

# Developer

Use this role for ordinary backend service changes that remain inside the declared service or bounded
context zone. Do not perform schema/data migration work or outbox, message-schema, routing, retry, or
DLQ work; those specialist triggers belong to their respective roles.

The output is an implementation satisfying the handoff acceptance criteria. Prove it with focused and
required project checks, plus a risk review when a listed trigger applies.
