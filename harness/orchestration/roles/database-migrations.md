---
name: database-migrations
mode: write
required_capabilities:
  - database-migrations
risk_triggers:
  - schema-change
  - data-migration
---

# Database migrations

Use this role for schema and data boundaries only. Its write scope is the declared database or
infrastructure zone; it does not absorb unrelated service implementation.

The output is a migration change with its rollout and rollback considerations. Prove it with the
project's migration and compatibility checks and the required risk review before handoff.
