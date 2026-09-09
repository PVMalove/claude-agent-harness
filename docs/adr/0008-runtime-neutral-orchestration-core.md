# Runtime-neutral orchestration core

The harness will define a portable orchestration core for roles, assignments, batches, and handoffs, with Orca supplied only as an optional runtime adapter. A human approves each dispatch; configuration resolves the role, agent, and model and permits a one-run override. This preserves runtime portability and keeps authority over cost, security, and parallel work with the developer rather than an autonomous scheduler.

## Considered Options

- Make Orca mandatory — rejected because the harness supports several runtimes and its workflow rules must remain usable without Orca.
- Let agents dispatch work freely — rejected because a token/cost budget and concurrent backend changes need an explicit owner.
