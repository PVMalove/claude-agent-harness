# Orca runtime adapter

`orca_adapter.py` is the optional runtime boundary for a project that selected
`backend-orchestration`. It does not select a provider, approve a dispatch, merge a pull request,
or run verification commands itself.

## Dispatch contract

Run the installed adapter from a project only after the coordinator has written an immutable JSON
brief and explicitly filled `coordinator_approval.approved_by` and
`coordinator_approval.approved_at`:

```text
python .harness/orchestration/orca_adapter.py dispatch --repo . --brief approved-dispatch.json --run <orca-run-id>
```

The brief identifies a role, matching `access` mode, one configured zone, its issue branch and
isolated-worktree display name, Definition of Done, prohibited changes, project verification commands,
required gates, dependencies, and approval. The `--run` value is the coordinator-owned Orca Run that
records the task and supervised worker. A write role
must repeat exactly the allowed paths of its one configured zone in `write_paths`; a read-only role
must not receive write paths. The adapter rejects secret-shaped fields before it invokes Orca.

The adapter reads agent, model, and fallback only from `.harness/orchestration.json`. Each provider
profile therefore declares an `agent` identifier in addition to capabilities, `default_model`,
fallback, and known limitations. Neither role manifests nor the adapter source names a provider or
model.

## Runtime and records

After validation, the adapter uses Orca's `orchestration task-create --run` followed by supervised
`orchestration worker-start --worktree new-top-level --setup run`. It names the new isolated worktree
after the approved issue branch and supplies that branch as its base; it never accepts the project base
branch or an `integration/*` branch. It checks Orca's active
worker list before creating a task, so the project `concurrency_budget` remains a dispatch gate.

Each successful dispatch writes a new JSON record under `.harness/orca-dispatches/` by exclusive
file creation. The record preserves the original brief, resolved profile/agent/model, Orca task and
worker identifiers when returned, dispatch ID, and launch outcome. A repeat always creates a new ID
and record; it cannot overwrite an earlier record.
