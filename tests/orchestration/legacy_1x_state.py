# mypy: ignore-errors
# The driver runs the code of the last 1.x release, not this tree: type-checking it against this
# tree would check the wrong code.
"""Write a state directory with the code of the last 1.x release.

``python -I legacy_1x_state.py <release tree> <repo> <worktrees>`` prints the ids of the batches
and dispatches it created as one JSON object. ``release_1x.build_1x_state`` runs it.

The state holds:

* one batch for every ``DispatchState`` that ``harness-runtime migrate`` admits (all except
  ``dispatched`` and ``working``): its one dispatch is in that state. The records are written by
  ``LifecycleLedger`` of the release, as ``runtime_store_support.build_state`` writes them with
  this tree;
* one batch that repair rule #781 changes on import: it owes a risk reassessment and names no
  candidate;
* four batches the coordinator of the release created: one planned under ``approval_policy:
  auto`` (``auto``), two with an approved dispatch that the 2.0.0 coordinator sends later
  (``send`` and ``work``), and one whose architect dispatch reported (``reported``).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ADMITTED = (
    "proposed",
    "approved",
    "blocked",
    "failed",
    "reported",
    "checkpointed",
    "rate_limited",
    "abandoned",
    "cancelled",
)
APPROVAL = ["--approved-by", "Malove", "--approved-at", "2026-09-17T00:00:00+00:00"]
TICKETS = {"auto": "#901", "send": "#902", "work": "#903", "reported": "#904"}


def plan_fields(batch_id, ticket):
    return {
        "batch_id": batch_id,
        "created_at": "2026-10-01T00:00:00+00:00",
        "base_commit": "1" * 40,
        "integration_ref": "integration/example",
        "branch_start_commit": "2" * 40,
        "ticket": ticket,
        "branch": f"feature/issue-{ticket.removeprefix('#')}-example",
        "worktree": "/var/projects/example/.harness/.sandboxes/worktrees/example",
        "zone": None,
        "allowed_paths": ["a/**"],
        "definition_of_done": ["The example is done."],
        "prohibited_changes": [],
        "developer_verification_commands": ["make test"],
        "verification_commands": ["make test"],
        "required_gates": ["review", "qa"],
        "dependencies": [],
        "approval_policy": "manual",
        "communication_policy": {
            "agent_to_agent_language": "en",
            "coordinator_report_language": "ru",
        },
        "scope_preflight": {"status": "clean", "outside_allowed_paths": []},
        "harness_runtime_sha256": "3" * 64,
        "write_paths": ["a/**"],
    }


def write_records(lifecycle, ledger, template, number, state, *, owes_reassessment=False):
    """One batch with one dispatch in ``state``, written record by record. ``template`` holds the
    brief and the status of an approved dispatch the coordinator created: the read commands need
    every field of a real brief."""
    batch_id = f"batch-{number:08x}-0000-4000-8000-000000000000"
    dispatch_id = f"dispatch-{number:08x}-0000-4000-8000-000000000000"
    ticket = f"#{800 + number}"
    generation = ledger.records_root()
    brief = lifecycle.DispatchRecord.from_dict(
        {
            **template["brief"],
            "dispatch_id": dispatch_id,
            "batch_id": batch_id,
            "ticket": ticket,
            "state": "approved",
        }
    )
    entry = {
        "dispatch_id": dispatch_id,
        "state": state,
        "brief_sha256": hashlib.sha256(
            lifecycle._canonical(brief.to_dict()).encode("utf-8")
        ).hexdigest(),
    }
    if state == "reported":
        report = {"dispatch_id": dispatch_id, "outcome": "completed", "output": "done"}
        ledger.write_immutable(
            generation / "reports" / f"{dispatch_id}.json", report, artifact=True
        )
        entry["report"] = f"reports/{dispatch_id}.json"
        entry["report_sha256"] = hashlib.sha256(
            lifecycle._canonical(report).encode("utf-8")
        ).hexdigest()
    fields = plan_fields(batch_id, ticket)
    reassessment = (
        {"risk_reassessment_required": True}
        if owes_reassessment
        else {}
    )
    ledger.write_record(lifecycle.PlanRecord(batch_id, dict(fields)))
    ledger.write_record(
        lifecycle.BatchRecord(batch_id, "active", [entry], None, {**fields, **reassessment})
    )
    ledger.write_record(brief)
    ledger.write_record(
        lifecycle.DispatchStatusRecord.from_dict(
            {**template["status"], "dispatch_id": dispatch_id, "state": state}
        )
    )
    return batch_id, dispatch_id


def main(argv):
    tree, repo, worktrees = (Path(value) for value in argv[1:4])
    sys.path.insert(0, str(tree))
    from harness.orchestration import coordinator
    from harness.orchestration.core import workspace
    from harness.orchestration.ledger import lifecycle

    config = repo / ".harness" / "orchestration.json"
    # A key the release counts as an authored config, so the budget below holds; an authored
    # config under `auto` needs worker attestation.
    settings = {
        "provider_profiles": {},
        "assignment_plans": {},
        "concurrency_budget": 99,
        "verification_commands": [],
        "auto_retry_min_severity": "blocker",
        "worker_attestation_required": True,
    }

    def run(*words):
        args = coordinator.parser().parse_args(["--repo", str(repo), *words])
        return args.handler(args)

    def create_batch(name):
        number = TICKETS[name].removeprefix("#")
        return run(
            "batch",
            "create",
            "--ticket",
            TICKETS[name],
            "--branch",
            f"feature/issue-{number}-golden",
            "--worktree",
            str(worktrees / f"worktree-{number}"),
            "--allowed-path",
            "**",
            "--integration-ref",
            "master",
            "--definition-of-done",
            "read the state of 1.x",
            "--prohibited-change",
            "secrets",
            "--expected-file",
            "services/x.py",
            "--expected-service",
            "core",
            "--expected-changed-lines",
            "10",
        )["batch_id"]

    def approved_dispatch(batch_id):
        run("batch", "approve", "--batch", batch_id, *APPROVAL)
        shape = [
            "--batch",
            batch_id,
            "--role",
            "architect",
            "--runtime",
            "claude",
            "--purpose",
            "work",
            "--model",
            "sonnet",
            "--effort",
            "high",
        ]
        proposal = run("dispatch", "propose", *shape)
        created = run(
            "dispatch",
            "create",
            *shape,
            "--transition-digest",
            proposal["transition_digest"],
            *APPROVAL,
        )
        return created["brief"]

    ids = {}
    config.write_text(json.dumps({**settings, "approval_policy": "auto"}), encoding="utf-8")
    ids["auto"] = create_batch("auto")
    config.write_text(json.dumps(settings), encoding="utf-8")
    for name in ("send", "work"):
        brief = approved_dispatch(create_batch(name))
        ids[name] = brief["batch_id"]
        ids[f"{name}_dispatch"] = brief["dispatch_id"]
    brief = approved_dispatch(create_batch("reported"))
    dispatch_id = brief["dispatch_id"]
    launched = run("dispatch", "send", "--dispatch", dispatch_id)
    run(
        "dispatch",
        "self-report",
        "--dispatch",
        dispatch_id,
        "--model",
        "sonnet",
        "--worktree",
        launched["worker_worktree"],
    )
    report = {
        "dispatch_id": dispatch_id,
        "ticket": brief["ticket"],
        "role": "architect",
        "outcome": "completed",
        "output": "done — café",
        "commit_sha": "not applicable — read-only role",
        "changed_files": [],
        "checks_run": [
            {"command": command, "result": "pass", "evidence": "n/a"}
            for command in brief["verification_commands"]
        ],
        "risks": {"status": "none", "details": ""},
        "blockers": {"status": "none", "details": ""},
        "next_coordinator_action": "decide",
        "report_language": "ru",
    }
    path = workspace._prepare_agent_inbox(repo) / f"{dispatch_id}.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    run("report", "submit", "--file", str(path))
    ids["reported"] = brief["batch_id"]
    ids["reported_dispatch"] = dispatch_id

    ledger = lifecycle.LifecycleLedger(repo / ".harness" / "orchestration" / "state")
    generation = ledger.records_root()
    template = {
        directory: json.loads(
            (generation / directory / f"{ids['send_dispatch']}.json").read_text(encoding="utf-8")
        )
        for directory in ("dispatches", "dispatch-status")
    }
    template = {"brief": template["dispatches"], "status": template["dispatch-status"]}
    for number, state in enumerate(ADMITTED, start=1):
        ids[f"state_{state}"], ids[f"state_{state}_dispatch"] = write_records(
            lifecycle, ledger, template, number, state
        )
    ids["repair_781"], _ = write_records(
        lifecycle, ledger, template, len(ADMITTED) + 1, "approved", owes_reassessment=True
    )
    print(json.dumps(ids, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
