# Role manifests and isolated backend batches

Portable role manifests will be one Markdown file per role under `harness/orchestration/roles/`; each project will map roles to agents, models, fallbacks, budgets, and verification commands in `.harness/orchestration.json`. `developer`, `database-migrations`, and `messaging-integration` may write only in declared zones, while `architect`, `qa`, and `code-review` are read-only. A batch owns one issue branch and worktree, and concurrent batches cannot overlap a service, bounded context, or infrastructure zone.

## Consequences

The harness must validate both configuration layers and make role boundaries observable before dispatch. Cross-role work inside one batch is a sequential handoff, not simultaneous writes. Code review becomes mandatory for API contracts, migrations, messaging/outbox, transactions, authorization/security, and concurrency/retry.
