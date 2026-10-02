"""Safe historical ingestion through public memory boundaries."""

import json
import sqlite3
from pathlib import Path

from harness.memory import build, search
from harness.orchestration.ledger import LifecycleLedger
from harness.storage import storage_path
from .test_build import configure, source


def ledger_fixture(repo: Path) -> Path:
    ledger = LifecycleLedger(repo / ".harness/orchestration/state")
    ledger.ensure()
    return ledger.records_root()


def test_completion_lessons_are_unconfirmed_sanitized_history(tmp_path: Path) -> None:
    """Only opted-in lessons are searchable; usage IDs and report prose are not."""
    configure(
        tmp_path,
        source_types=["completion_report"],
        allow_paths=[".harness/orchestration/state/generations/*/reports/*.json"],
    )
    generation = ledger_fixture(tmp_path)
    relative = (generation / "reports/developer.json").relative_to(tmp_path).as_posix()
    source(
        tmp_path,
        relative,
        json.dumps(
            {
                "role": "developer",
                "ticket": "#429",
                "outcome": "completed",
                "status": "accepted",
                "output": "unrelatedoutput",
                "lessons": [
                    "lessonword token=baselineprivate secret=projectprivate /tmp/private.log"
                ],
                "used_memory": ["usageword"],
                "unknown": "unknownword",
            }
        ),
    )
    assert build(tmp_path)["indexed"] == 1
    pointers = search(tmp_path, "lessonword")["pointers"]
    assert isinstance(pointers, list) and len(pointers) == 1
    assert pointers[0]["status"] == "не подтверждено человеком"
    assert pointers[0]["history_to_verify"] is True
    assert pointers[0]["path"] == relative
    for term in (
        "usageword",
        "unrelatedoutput",
        "unknownword",
        "baselineprivate",
        "projectprivate",
    ):
        assert search(tmp_path, term)["pointers"] == []
    with sqlite3.connect(
        storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    ) as db:
        dump = "\n".join(db.iterdump())
    assert "baselineprivate" not in dump and "projectprivate" not in dump
    assert "/tmp/private.log" not in dump and "usageword" not in dump


def test_lessons_opt_in_preserves_qa_and_does_not_promote_used_hits(
    tmp_path: Path,
) -> None:
    """Usage evidence never changes ranking; legacy QA authorization stays narrow."""
    paths = [".harness/orchestration/state/generations/*/reports/*.json"]
    configure(tmp_path, source_types=["qa_finding"], allow_paths=paths)
    generation = ledger_fixture(tmp_path)
    qa = (generation / "reports/qa.json").relative_to(tmp_path).as_posix()
    dev = (generation / "reports/dev.json").relative_to(tmp_path).as_posix()
    qa_value = {
        "role": "qa",
        "output": "qualityword sharedterm",
        "lessons": ["qalesword"],
        "used_memory": [],
    }
    dev_value: dict[str, object] = {
        "role": "developer",
        "lessons": ["lessonword sharedterm"],
        "used_memory": [],
    }
    source(tmp_path, qa, json.dumps(qa_value))
    source(tmp_path, dev, json.dumps(dev_value))
    build(tmp_path)
    assert search(tmp_path, "qualityword")["pointers"]
    assert search(tmp_path, "lessonword qalesword")["pointers"] == []
    configure(
        tmp_path, source_types=["qa_finding", "completion_report"], allow_paths=paths
    )
    build(tmp_path)
    assert search(tmp_path, "qualityword")["pointers"]
    assert search(tmp_path, "qalesword")["pointers"]
    initial_pointers = search(tmp_path, "sharedterm")["pointers"]
    assert isinstance(initial_pointers, list)
    initial = [p["path"] for p in initial_pointers]
    dev_value["used_memory"] = [qa] * 100
    source(tmp_path, dev, json.dumps(dev_value))
    build(tmp_path)
    updated_pointers = search(tmp_path, "sharedterm")["pointers"]
    assert isinstance(updated_pointers, list)
    assert [p["path"] for p in updated_pointers] == initial
    # A replaced or malformed lesson never becomes confirmed historical evidence.
    dev_value["status"] = "superseded"
    source(tmp_path, dev, json.dumps(dev_value))
    build(tmp_path)
    assert search(tmp_path, "lessonword")["pointers"] == []
    dev_value.pop("status")
    dev_value["lessons"] = ["lessonword", {"prompt": "forbiddenword"}]
    source(tmp_path, dev, json.dumps(dev_value))
    build(tmp_path)
    assert search(tmp_path, "lessonword forbiddenword")["pointers"] == []


def test_authorized_archive_qa_and_ledger_store_only_safe_projections(
    tmp_path: Path,
) -> None:
    """Explicit types and paths authorize safe history, never raw lifecycle/report JSON."""
    configure(
        tmp_path,
        source_types=["task_archive", "qa_finding", "ledger"],
        allow_paths=[
            "docs/tasks/**/*.md",
            ".harness/orchestration/state/generations/**/*.json",
        ],
    )
    source(
        tmp_path,
        "docs/tasks/issue-423-memory/issue-423-spec-memory.md",
        "# Archive\nStatus: done\narchiveword secret=private",
    )
    source(
        tmp_path,
        "docs/tasks/issue-423-memory/attachments/transcript.md",
        "# Forbidden\nattachmentword",
    )
    generation = ledger_fixture(tmp_path)
    source(
        tmp_path,
        (generation / "reports/qa.json").relative_to(tmp_path).as_posix(),
        json.dumps(
            {
                "ticket": "#423",
                "role": "qa",
                "outcome": "pass",
                "output": "qualityword token=unsafe secret=project /tmp/full.log",
                "risks": ["riskword"],
                "blockers": [],
                "checks_run": [
                    {
                        "command": "forbiddencommand",
                        "result": "pass",
                        "evidence": "evidenceword password=unsafe",
                    }
                ],
                "unknown": "unknownword",
                "prompt": "promptword",
            }
        ),
    )
    source(
        tmp_path,
        (generation / "dispatch-status/d.json").relative_to(tmp_path).as_posix(),
        json.dumps(
            {
                "dispatch_id": "dispatchword",
                "state": "working",
                "ticket": "#423",
                "created_at": "2026-09-30",
                "coordinator_approval": {"approved_by": "approvalword"},
                "prompt": "promptword",
            }
        ),
    )
    source(
        tmp_path,
        (generation / "reports/review.json").relative_to(tmp_path).as_posix(),
        json.dumps({"role": "code-review", "output": "reviewword"}),
    )
    assert build(tmp_path)["indexed"] == 3
    for term in ("archiveword", "qualityword", "evidenceword", "dispatchword"):
        pointers = search(tmp_path, term)["pointers"]
        assert isinstance(pointers, list) and len(pointers) == 1
        assert pointers[0]["history_to_verify"] is True
    for term in (
        "attachmentword",
        "forbiddencommand",
        "unknownword",
        "promptword",
        "approvalword",
        "reviewword",
        "unsafe",
        "project",
    ):
        assert search(tmp_path, term)["pointers"] == []
    with sqlite3.connect(
        storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    ) as db:
        dump = "\n".join(db.iterdump())
    assert (
        "unsafe" not in dump and "/tmp/full.log" not in dump and "secret=" not in dump
    )
    configure(
        tmp_path,
        source_types=["glossary"],
        allow_paths=[
            "docs/tasks/**/*.md",
            ".harness/orchestration/state/generations/**/*.json",
        ],
    )
    assert build(tmp_path)["indexed"] == 0


def test_baseline_redaction_and_generation_selection_do_not_depend_on_project_rules(
    tmp_path: Path,
) -> None:
    """Only the selected generation is searchable, even with a broad JSON allowlist."""
    configure(
        tmp_path,
        source_types=["qa_finding", "ledger"],
        redact_rules=[],
        allow_paths=[".harness/orchestration/state/generations/**/*.json"],
    )
    generation = ledger_fixture(tmp_path)
    relative = (generation / "reports/qa.json").relative_to(tmp_path).as_posix()
    source(
        tmp_path,
        relative,
        json.dumps(
            {
                "role": "qa",
                "outcome": "pass",
                "output": "qualityword token=privatepassword",
                "checks_run": [],
            }
        ),
    )
    source(
        tmp_path,
        ".harness/orchestration/state/generations/generation-old/reports/qa.json",
        json.dumps({"role": "qa", "output": "oldgenerationword"}),
    )
    build(tmp_path)
    assert search(tmp_path, "qualityword")["pointers"]
    assert search(tmp_path, "oldgenerationword")["pointers"] == []
    assert search(tmp_path, "privatepassword")["pointers"] == []
    with sqlite3.connect(
        storage_path(tmp_path, "cache", "memory", "index.sqlite3")
    ) as db:
        assert "privatepassword" not in "\n".join(db.iterdump())
    pointer = tmp_path / ".harness/orchestration/state/ledger.json"
    value = json.loads(pointer.read_text())
    value["generation"] = "generation-../../outside"
    pointer.write_text(json.dumps(value))
    assert search(tmp_path, "qualityword")["status"] == "index_unavailable"
