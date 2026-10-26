"""``harness-runtime migrate`` on a state the Python 1.x ledger wrote (ADR 0020, section 5).

The state is written by the real ``LifecycleLedger`` and the cutover is run by the real Rust
process, so each rule is observed at the process boundary: the exit code, the JSON line, the files
and what a 1.x installation does with the result. CI builds the binary first
(``HARNESS_REQUIRE_RUNTIME=1`` makes a missing one fail).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from harness.orchestration.core.constants import DispatchState
from harness.orchestration.ledger.lifecycle import JsonObject, LifecycleLedger, _canonical

from .runtime_store_support import (
    BATCH_ID,
    STATE_REL,
    build_state,
    coordinator,
    load,
    migrate,
    tree,
)

# What a cutover may add next to the legacy records: the store and the authority file.
NEW = ("runtime.db", "AUTHORITY.json")


def _store(repo: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{repo / STATE_REL / 'runtime.db'}?mode=ro", uri=True)


def test_migrate_cuts_the_state_over_and_reports_one_json_line(tmp_path: Path) -> None:
    build_state(tmp_path)
    before = tree(tmp_path / STATE_REL)

    result = migrate(tmp_path)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["migrated"] is True
    assert report["already_cut_over"] is False
    pointer = load(tmp_path / STATE_REL / "ledger.json")
    assert pointer["version"] == 4
    authority = load(tmp_path / STATE_REL / "AUTHORITY.json")
    assert authority["authority"] == "runtime-store"
    assert authority["store_id"] == report["store_id"]
    after = tree(tmp_path / STATE_REL)
    for path, content in before.items():
        if path != "ledger.json":
            assert after[path] == content, path
    assert {path for path in after if path not in before} <= set(NEW)


def test_a_1x_installation_refuses_the_state_after_the_cutover(tmp_path: Path) -> None:
    build_state(tmp_path)
    assert migrate(tmp_path).returncode == 0
    legacy_files = tree(tmp_path / STATE_REL)

    result = coordinator(tmp_path, "ledger", "validate")

    assert result.returncode == 2
    assert "unsupported version" in result.stderr
    assert tree(tmp_path / STATE_REL) == legacy_files


def test_the_snapshot_is_a_state_a_1x_installation_still_validates(tmp_path: Path) -> None:
    build_state(tmp_path)
    report = json.loads(migrate(tmp_path).stdout)

    snapshot = tmp_path / report["snapshot"]
    validated = LifecycleLedger(snapshot / "state").validate()

    assert validated["valid"] is True
    assert validated["batches"] == [BATCH_ID]
    manifest = load(snapshot / "snapshot-manifest.json")
    files = manifest["files"]
    assert isinstance(files, list)
    assert {entry["path"] for entry in files if isinstance(entry, dict)} >= {"ledger.json"}


def test_a_repeated_migrate_reports_already_cut_over_and_changes_no_byte(
    tmp_path: Path,
) -> None:
    build_state(tmp_path)
    first = json.loads(migrate(tmp_path).stdout)
    before = tree(tmp_path / STATE_REL, skip=("runtime.db-",))

    result = migrate(tmp_path)

    assert result.returncode == 0, result.stderr
    again = json.loads(result.stdout)
    assert again["already_cut_over"] is True
    assert again["migrated"] is False
    assert again["store_id"] == first["store_id"]
    assert again["manifest_sha256"] == first["manifest_sha256"]
    assert tree(tmp_path / STATE_REL, skip=("runtime.db-",)) == before


def test_a_working_dispatch_refuses_with_exit_code_2_and_a_remedy(tmp_path: Path) -> None:
    build_state(tmp_path, second_status=DispatchState.WORKING)
    before = tree(tmp_path / STATE_REL)

    result = migrate(tmp_path)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("ERROR: dispatches are sent or working")
    assert "REMEDY:" in result.stderr
    assert tree(tmp_path / STATE_REL) == before
    assert not (tmp_path / ".harness/orchestration/state-snapshots").exists()


def test_an_empty_repository_has_nothing_to_migrate(tmp_path: Path) -> None:
    (tmp_path / STATE_REL).mkdir(parents=True)

    result = migrate(tmp_path)

    assert result.returncode == 2
    assert "nothing_to_migrate" in result.stderr


def test_a_pre_ledger_layout_names_the_1x_ledger_migrate(tmp_path: Path) -> None:
    (tmp_path / STATE_REL / "batches").mkdir(parents=True)

    result = migrate(tmp_path)

    assert result.returncode == 2
    assert "ledger migrate" in result.stderr


def test_the_781_repair_is_applied_and_audited_with_hashes_python_recomputes(
    tmp_path: Path,
) -> None:
    ledger = build_state(tmp_path, owes_reassessment_without_candidate=True)
    path = ledger.records_root() / "batches" / f"{BATCH_ID}.json"
    legacy = json.loads(path.read_text(encoding="utf-8"))
    assert "risk_reassessment_candidate" not in legacy

    result = migrate(tmp_path)

    assert result.returncode == 0, result.stderr
    repaired = {**legacy, "risk_reassessment_pending_next": True}
    with closing(_store(tmp_path)) as store:
        pending = store.execute(
            "SELECT risk_reassessment_required, risk_reassessment_candidate, "
            "risk_reassessment_pending_next FROM batch"
        ).fetchone()
        audit = store.execute(
            "SELECT action, details_json FROM audit ORDER BY seq"
        ).fetchall()
    assert pending == (1, None, 1)
    assert [action for action, _ in audit] == ["legacy-import", "migration-repair"]
    details = json.loads(audit[1][1])
    assert details["rule"] == "781-risk-reassessment-pending-next"
    assert details["before_sha256"] == _sha(legacy)
    assert details["after_sha256"] == _sha(repaired)
    # The legacy file is the same bytes it was.
    assert json.loads(path.read_text(encoding="utf-8")) == legacy


def test_the_first_audit_record_carries_the_manifest_hash_of_the_legacy_audit(
    tmp_path: Path,
) -> None:
    build_state(tmp_path)
    report = json.loads(migrate(tmp_path).stdout)

    with closing(_store(tmp_path)) as store:
        (details,) = store.execute(
            "SELECT details_json FROM audit WHERE seq = 1 AND action = 'legacy-import'"
        ).fetchone()
        stored = store.execute("SELECT path, sha256 FROM import_manifest ORDER BY path").fetchall()
        audit_files = store.execute("SELECT count(*) FROM legacy_audit").fetchone()[0]
    assert json.loads(details)["manifest_sha256"] == report["manifest_sha256"]
    assert stored == sorted(stored)
    assert audit_files == len(stored) > 0
    snapshot = tmp_path / report["snapshot"]
    manifest = json.loads((snapshot / "import-manifest.json").read_text(encoding="utf-8"))
    assert [(entry["path"], entry["sha256"]) for entry in manifest["entries"]] == stored


def _sha(value: JsonObject) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
