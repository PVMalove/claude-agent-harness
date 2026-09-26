"""Миграция ledger между версиями схемы: сценарий clean-room из `scripts/test_clean_room.py`."""

import hashlib
import json
import sys
from types import SimpleNamespace


def run(ctx: SimpleNamespace) -> None:
    """Миграция ledger между версиями схемы.

    Читает из контекста: `coordinator_run`, `test_root`.
    Передаёт дальше: `legacy_batch`, `legacy_batch_id`, `legacy_plan`.
    """
    coordinator_run = ctx.coordinator_run
    test_root = ctx.test_root
    # State created before the versioned ledger existed remains a supported migration input.  The
    # public CLI must validate a complete replacement generation before atomically selecting it,
    # leave damaged input untouched, and never reset a live batch without an explicit confirmation.
    legacy_ledger_state = test_root / "legacy-ledger-state"
    legacy_batch_id = "batch-00000000-0000-0000-0000-000000000123"
    legacy_plan = {
        "batch_id": legacy_batch_id,
        "created_at": "2026-09-14T10:00:00+00:00",
        "base_commit": None,
        "ticket": "#ledger",
        "branch": "feature/issue-123-versioned-ledger",
        "worktree": "issue-123-versioned-ledger",
        "zone": "backend",
        "definition_of_done": ["migrate the lifecycle state"],
        "prohibited_changes": ["do not merge"],
        "developer_verification_commands": ["echo test"],
        "verification_commands": ["echo test"],
        "required_gates": ["none"],
        "dependencies": ["none"],
    }
    legacy_batch = {
        **legacy_plan,
        "state": "planned",
        "dispatches": [],
        "risk_assessments": [],
        "risk_escalations": [],
        "risk_reassessment_required": False,
    }
    (legacy_ledger_state / "batches").mkdir(parents=True)
    (legacy_ledger_state / "plans").mkdir(parents=True)
    (legacy_ledger_state / "batches" / f"{legacy_batch_id}.json").write_text(
        json.dumps(legacy_batch, indent=2) + "\n", encoding="utf-8"
    )
    (legacy_ledger_state / "plans" / f"{legacy_batch_id}.json").write_text(
        json.dumps(legacy_plan, indent=2) + "\n", encoding="utf-8"
    )
    migrated = coordinator_run(
        "--state-dir", str(legacy_ledger_state), "ledger", "migrate"
    )
    if migrated.returncode != 0:
        sys.exit("ledger rejected a valid legacy state: " + migrated.stderr)
    migrated_record = json.loads(migrated.stdout)
    if migrated_record["version"] != 3 or not migrated_record["generation"].startswith(
        "generation-"
    ):
        sys.exit("ledger migration did not select the current versioned generation")
    ledger_status = coordinator_run(
        "--state-dir", str(legacy_ledger_state), "ledger", "status"
    )
    if (
        ledger_status.returncode != 0
        or json.loads(ledger_status.stdout)["generation"]
        != migrated_record["generation"]
    ):
        sys.exit("ledger status did not expose the selected generation")
    migrated_batch = (
        legacy_ledger_state
        / "generations"
        / migrated_record["generation"]
        / "batches"
        / f"{legacy_batch_id}.json"
    )
    if (
        not migrated_batch.is_file()
        or migrated_batch.read_bytes()
        != (legacy_ledger_state / "batches" / f"{legacy_batch_id}.json").read_bytes()
    ):
        sys.exit("ledger migration did not preserve the immutable legacy batch record")
    if (
        coordinator_run(
            "--state-dir", str(legacy_ledger_state), "ledger", "reset"
        ).returncode
        == 0
    ):
        sys.exit("ledger reset did not require explicit confirmation")
    reset = coordinator_run(
        "--state-dir", str(legacy_ledger_state), "ledger", "reset", "--confirm", "RESET"
    )
    if (
        reset.returncode != 0
        or json.loads(reset.stdout)["generation"] == migrated_record["generation"]
    ):
        sys.exit("ledger reset did not atomically select a fresh generation")

    # A ledger schema change (Issue #138 added context-packages, Issue #139 added checkpoints) is
    # itself a migration: a generation an older harness version already selected is a valid, but
    # stale, migration source -- readable only by an explicit `ledger migrate`, never implicitly
    # upgraded.
    def write_audit_record(generation_dir, audit_id, action, details):
        record = {
            "audit_id": audit_id,
            "at": "2026-09-09T15:24:00+00:00",
            "action": action,
            "details": details,
        }
        canonical = (
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        record["record_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        (generation_dir / "audit" / f"{audit_id}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    stale_schema_state = test_root / "stale-schema-ledger-state"
    stale_schema_generation = stale_schema_state / "generations" / "generation-legacy01"
    v1_record_directories = (
        "batches",
        "plans",
        "dispatches",
        "dispatch-status",
        "risk-assessments",
        "reports",
        "qa-lane",
        "qa-artifacts",
        "audit",
    )
    for directory in v1_record_directories:
        (stale_schema_generation / directory).mkdir(parents=True)
    stale_schema_batch_id = "batch-00000000-0000-0000-0000-000000000456"
    stale_schema_plan = {
        **legacy_plan,
        "batch_id": stale_schema_batch_id,
        "branch": "feature/issue-138-stale-schema",
    }
    stale_schema_batch = {
        **stale_schema_plan,
        "state": "planned",
        "dispatches": [],
        "risk_assessments": [],
        "risk_escalations": [],
        "risk_reassessment_required": False,
    }
    (stale_schema_generation / "batches" / f"{stale_schema_batch_id}.json").write_text(
        json.dumps(stale_schema_batch, indent=2) + "\n", encoding="utf-8"
    )
    (stale_schema_generation / "plans" / f"{stale_schema_batch_id}.json").write_text(
        json.dumps(stale_schema_plan, indent=2) + "\n", encoding="utf-8"
    )
    write_audit_record(
        stale_schema_generation,
        "audit-legacy01",
        "generation-created",
        {"purpose": "initialize"},
    )
    stale_pointer = {
        "version": 1,
        "generation": "generation-legacy01",
        "selected_at": "2026-09-09T15:24:00+00:00",
    }
    (stale_schema_state / "ledger.json").write_text(
        json.dumps(stale_pointer, indent=2) + "\n", encoding="utf-8"
    )

    blocked_status = coordinator_run(
        "--state-dir", str(stale_schema_state), "ledger", "status"
    )
    if blocked_status.returncode != 0 or not json.loads(blocked_status.stdout).get(
        "stale_schema"
    ):
        sys.exit(
            "ledger status did not report a stale-schema generation: "
            + blocked_status.stdout
        )
    blocked_list = coordinator_run(
        "--state-dir", str(stale_schema_state), "batch", "list"
    )
    if blocked_list.returncode == 0:
        sys.exit(
            "coordinator used a stale-schema generation instead of requiring an explicit ledger migrate"
        )

    schema_migrated = coordinator_run(
        "--state-dir", str(stale_schema_state), "ledger", "migrate"
    )
    if schema_migrated.returncode != 0:
        sys.exit("ledger rejected a valid schema upgrade: " + schema_migrated.stderr)
    schema_migrated_record = json.loads(schema_migrated.stdout)
    if (
        schema_migrated_record["version"] != 3
        or schema_migrated_record["generation"] == "generation-legacy01"
    ):
        sys.exit("ledger schema upgrade did not select a new current-schema generation")
    upgraded_batch_path = (
        stale_schema_state
        / "generations"
        / schema_migrated_record["generation"]
        / "batches"
        / f"{stale_schema_batch_id}.json"
    )
    if (
        not upgraded_batch_path.is_file()
        or json.loads(upgraded_batch_path.read_text(encoding="utf-8"))
        != stale_schema_batch
    ):
        sys.exit(
            "ledger schema upgrade did not preserve the older-schema batch record as evidence"
        )
    for new_directory in ("context-packages", "checkpoints"):
        if not (
            stale_schema_state
            / "generations"
            / schema_migrated_record["generation"]
            / new_directory
        ).is_dir():
            sys.exit(
                f"ledger schema upgrade did not create the new {new_directory} record directory"
            )
    already_current = coordinator_run(
        "--state-dir", str(stale_schema_state), "ledger", "migrate"
    )
    if (
        already_current.returncode != 0
        or json.loads(already_current.stdout)["migrated"]
    ):
        sys.exit(
            "ledger migrate was not idempotent once the current schema was already selected"
        )

    # A generation selected by the immediately prior schema version (Issue #138's context-packages,
    # lacking only the checkpoints directory Issue #139 added) is the same stale-but-valid source,
    # one version closer to current.
    stale_v2_state = test_root / "stale-v2-ledger-state"
    stale_v2_generation = stale_v2_state / "generations" / "generation-legacy02"
    v2_record_directories = (
        "batches",
        "plans",
        "dispatches",
        "dispatch-status",
        "risk-assessments",
        "context-packages",
        "reports",
        "qa-lane",
        "qa-artifacts",
        "audit",
    )
    for directory in v2_record_directories:
        (stale_v2_generation / directory).mkdir(parents=True)
    write_audit_record(
        stale_v2_generation,
        "audit-legacy02",
        "generation-created",
        {"purpose": "initialize"},
    )
    v2_pointer = {
        "version": 2,
        "generation": "generation-legacy02",
        "selected_at": "2026-09-14T15:24:00+00:00",
    }
    (stale_v2_state / "ledger.json").write_text(
        json.dumps(v2_pointer, indent=2) + "\n", encoding="utf-8"
    )
    v2_migrated = coordinator_run(
        "--state-dir", str(stale_v2_state), "ledger", "migrate"
    )
    if v2_migrated.returncode != 0:
        sys.exit(
            "ledger rejected a valid v2-to-v3 schema upgrade: " + v2_migrated.stderr
        )
    v2_migrated_record = json.loads(v2_migrated.stdout)
    if (
        v2_migrated_record["version"] != 3
        or v2_migrated_record["generation"] == "generation-legacy02"
    ):
        sys.exit(
            "ledger v2-to-v3 upgrade did not select a new current-schema generation"
        )
    if not (
        stale_v2_state
        / "generations"
        / v2_migrated_record["generation"]
        / "checkpoints"
    ).is_dir():
        sys.exit(
            "ledger v2-to-v3 upgrade did not create the new checkpoints record directory"
        )

    print("ledger schema-upgrade migration verification passed")
    ctx.legacy_batch = legacy_batch
    ctx.legacy_batch_id = legacy_batch_id
    ctx.legacy_plan = legacy_plan
