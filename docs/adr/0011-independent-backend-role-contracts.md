# Independent backend role contracts

`architect` produces a read-only decision brief and `qa` returns reproducible findings without changing tests or fixtures. `code-review` is a high-risk gate with separate Standards and Spec reports. `database-migrations` and `messaging-integration` replace `developer` only at their specialist triggers; mixed work is sequential inside one batch with one active writer.

## Consequences

The author of a change cannot supply the sole architecture, QA, or review evidence for it. A role manifest must state the role's output contract and trigger, and the dispatcher must reject simultaneous writers in one batch.
