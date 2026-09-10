---
name: messaging-integration
mode: write
required_capabilities:
  - messaging-integration
risk_triggers:
  - outbox
  - message-schema-routing
  - retry-dlq
---

# Messaging integration

Use this role for outbox, message schema or routing, retry, and dead-letter queue boundaries. Its
write scope is the declared messaging or infrastructure zone; it does not absorb unrelated service
implementation.

The output is a compatible messaging change with delivery and failure-handling consequences stated.
Prove it with the project-required contract, routing, and failure-path checks and the required risk
review before handoff.
