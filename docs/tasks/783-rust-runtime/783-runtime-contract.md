# Spec #783.A — Rust Execution Kernel and single cutover
Status: proposed. Parent epic #783; ADR 0019.
Rust is the final runtime, never an interim Python executor. Python owns skills and AI policy; Rust owns process/container execution and operational state after one coordinated cutover.
## Boundaries
Crates core, ipc, state, exec, container, qa, reconcile. Python permanent bridge harness/orchestration/runtime_client.py.
Versioned requests contain operation_id, batch_id, dispatch_id, candidate_sha, approved_plan_digest, deadline and resource scope. Server verifies approval against persisted records, not user-supplied flags. Local IPC authenticated by peer identity.
State SQLite WAL + CAS revisions + transactional leases and outbox. One authoritative writer. Immutable prior audit/evidence retained and validated, migration snapshot+manifest+rollback tested.
Process identity includes OS start identity and container digest/labels, not PID alone. Restart/lost IPC receipt idempotent. Fedora/SELinux rootless Podman, Linux cgroups, Windows job objects.
Structured failure domains: preparation, infrastructure, policy, tooling, code_test, timeout, cancelled, unknown. Infra failure never consumes developer budget.
## Acceptance
- SIGKILL Python or Rust / reboot, lost ACK and duplicate command cannot double-start worker or strand QA.
- Cross-batch writes concurrent, QA lane serial, consistent reads and tamper-detecting full audit.
- No agent auto-approval, network expansion, host secrets, Docker socket or silent runtime switch.
- Migration from historical ledger checks parity, preserves evidence, runs once without dual writer, supports lossless rollback/recovery.
- Reconcile #634 #709 #774 #777 #778 #779 #781 to the final architecture without rewriting intermediate Python systems.
## Delivery
Use integration/harness-runtime-reliability for planning. Existing issue integration branches are not force-reset. Full Linux + Windows CI, fault injection and final independent QA before one cutover.