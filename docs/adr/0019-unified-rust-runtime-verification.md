# ADR 0019 — Unified Rust runtime and incremental verification
Status: PROPOSED. Parent epic #784 (absorbed duplicate #783). Owner decision 2026-10-20.

## Decision
Implement Rust immediately as the final execution kernel; retain Python for AI skills, workflows and policy. Single coordinated migration and cutover: do not create a temporary Python executor or multiple authoritative ledger writers. Parallelize work only across versioned, stable boundaries.

## Interfaces
Rust crates: core/contracts, ipc, state, exec, container, qa, reconcile. Python bridge: harness/orchestration/runtime_client.py. IPC envelope contains protocol_version, operation_id, batch_id, dispatch_id, candidate_sha, plan_digest, operation, deadline, payload. Outcomes include execution_id, revision, phase, status, structured failure domain/code/retryability, evidence references. Validate approved plans server-side; do not trust agent-supplied approval flags.

## Invariants
- SQLite WAL transactional operational state, one writer authority, CAS revisions, consistent reads, serialized QA admission, independent batch writes.
- Preserve historical immutable audit and evidence. Migration snapshots, checksums, manifest, acceptance comparison, rollback and forensic preservation; no mixed-writer period.
- Reconcile actual process identity, cgroups/Windows jobs, OCI IDs/labels and persisted operation ID after SIGKILL, timeout, restart and lost IPC response; no duplicated starts.
- Unknown or infrastructure failures cannot automatically turn into code defects or exhaust developer budget.
- Native approval, SHA/image/plan digests, read-only source, temp/cache/evidence mounts, scoped secrets/egress, SELinux/rootless Podman and Windows boundaries stay enforced. Trusted supervisor must run outside untrusted agent sandbox.
- Final clean-room QA requires the full approved checks on exact final SHA; no partial/failed-only result authorizes publish.

## Verification modes
- Focused Developer: changed files + repo_map dependency graph + Context Package related_tests, focused linters and type checks; conservative escalation on unknown dependencies.
- Review: independently executed focused proof, not reuse of developer self-report.
- Failed-only: exact pytest failed node IDs or per-worktree pytest --lf --lfnf=none; --ff is failed-first, not failed-only. Diagnostic step only.
- Full QA: complete required gate, including sequential context-coupled clean-room E2E; any new candidate SHA invalidates prior full QA pass.
- Stage evidence: command_id, candidate_sha, plan/image digest, environment fingerprint, pass/fail/not_run, exit, duration, stderr/stdout refs. No unproven cross-run success cache.

## Failure taxonomy
environment_preparation, infrastructure, policy, tooling, code_test, timeout, cancelled, unknown. Structured proof required. Lost acknowledgement must not duplicate operation. A failed step does not mark remaining unexecuted steps as failed.

## Test matrix
SIGKILL Python/Rust; stale locks across PID namespaces; PID reuse; OCI crash; SELinux denial; read-only /tmp; offline uv sync; tampered audit; concurrent batches; orphaned QA lease; rewind risk-state dead end (#781); changed SHA; crash mid-transaction; lost IPC response.

## Existing work reconciliation
#634 and #709 from integration/container-executor; #774 #777 #778 #779 from integration/orchestration-ledger; #781 state-machine bug. Completed issues #615 #666 #756 #757 #758 #769 #780 are historical prerequisites. Do not reset/force-merge existing branches. Reconcile their acceptance criteria to this final Rust state model and make only one cutover.

## Measurement and acceptance
Measure before/after total implement elapsed time, full-suite invocations, failed-only rerun time, coordinator p50/p95/p99, audit verification overhead, crash recovery and cross-platform compatibility. Full QA still required. Agent-written code must pass contract, property, integration and fault-injection tests before PR.

## Discovery
harness/orchestration/{coordinator.py,qa_lane.py,runtime_access.py,container_executor.py,ledger/lifecycle.py,ledger/ledger_ops.py,workflow/risk.py,workflow/recovery.py,workflow/dispatch.py,contract.py}, harness/gate_runner/gate_runner.py, scripts/verify.py, scripts/test_clean_room.py, harness/repo_map/, tests/orchestration/, .github/workflows/verify.yml.

## Workflow
Use grill-with-docs to challenge remaining unknowns without reopening the fixed Rust/one-cutover decision; to-spec reconciles existing epic #784 (absorbed #783); to-tickets produces result-dependent slices; implement respects owner approval.