"""Tracker snapshots through the explicit public writer and offline search."""

import importlib
import json
import subprocess
import sqlite3
import os
import sys
from pathlib import Path

import pytest

from harness.memory import search
from .test_build import configure

SNAPSHOT = ".harness/.sandboxes/memory/snapshot"


@pytest.fixture
def remote_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://github.com/team/project.git",
        ],
        check=True,
    )
    configure(
        tmp_path,
        source_types=["task_archive", "completion_report"],
        allow_paths=[SNAPSHOT + "/records/*.json"],
    )
    return tmp_path


def fake_inventory(
    monkeypatch: pytest.MonkeyPatch,
    items: list[dict[str, object]],
    comments: list[dict[str, object]] | None = None,
) -> None:
    def page(argv: list[str], repo: Path) -> list[dict[str, object]]:
        if "page=2" in argv[-1] or "pulls?" in argv[-1]:
            return []
        return (comments or []) if "comments?" in argv[-1] else items

    monkeypatch.setattr(
        importlib.import_module("harness.memory.sync"), "fetch_page", page
    )


def test_ungranted_snapshot_is_not_read_by_offline_build(remote_repo: Path) -> None:
    from harness.memory import build

    configure(remote_repo, source_types=["adr"], allow_paths=["docs/adr/*.md"])
    snapshot = remote_repo / SNAPSHOT
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.json").write_text("invalid raw secret=shouldnotread")
    assert build(remote_repo)["indexed"] == 0


def test_sync_sanitizes_all_retained_fields_and_ignores_unmarked_comments(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    configure(
        remote_repo,
        source_types=["task_archive", "completion_report"],
        allow_paths=[SNAPSHOT + "/records/*.json"],
        redact_rules=["customprivate"],
    )
    report = {
        "title": "reportword token=titleprivate",
        "output": "customprivate password=reportprivate",
        "risks": ["riskword https://private.invalid/token"],
        "blockers": "/tmp/private.log",
        "unknown": "unknownprivate",
        "checks_run": [{"command": "commandprivate"}],
    }
    fake_inventory(
        monkeypatch,
        [
            {
                "number": 1,
                "state": "closed",
                "title": "ticketword customprivate",
                "body": "token=bodyprivate",
                "updated_at": "credential=dateprivate",
            }
        ],
        [
            {
                "id": 4,
                "body": "## Completion report\n```json\n"
                + json.dumps(report)
                + "\n```",
            },
            {"id": 5, "body": "unmarkedprivate"},
            {"id": 6, "body": "## Completion report\n```json\ninvalid\n```"},
        ],
    )
    assert sync(remote_repo)["skipped_reports"] == 2
    serialized = "".join(
        path.read_text() for path in (remote_repo / SNAPSHOT).rglob("*.json")
    )
    with sqlite3.connect(
        remote_repo / ".harness/.sandboxes/cache/memory/index.sqlite3"
    ) as db:
        serialized += "\n".join(db.iterdump())
    for sentinel in (
        "customprivate",
        "titleprivate",
        "reportprivate",
        "bodyprivate",
        "dateprivate",
        "private.invalid",
        "/tmp/private.log",
        "unknownprivate",
        "commandprivate",
        "unmarkedprivate",
    ):
        assert sentinel not in serialized
    assert search(remote_repo, "riskword")["pointers"]


@pytest.mark.parametrize(
    "types,paths",
    [
        ([], [SNAPSHOT + "/**"]),
        (["ledger"], [SNAPSHOT + "/**"]),
        (["task_archive"], ["docs/tasks/**"]),
    ],
)
def test_sync_requires_independent_type_and_path_grants(
    remote_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    types: list[str],
    paths: list[str],
) -> None:
    from harness.memory import sync

    configure(remote_repo, source_types=types, allow_paths=paths)

    def forbidden(repo: Path) -> tuple[str, str]:
        pytest.fail("ungranted tracker access")

    monkeypatch.setattr(
        importlib.import_module("harness.memory.sync"), "tracker", forbidden
    )
    assert sync(remote_repo)["status"] == "disabled"
    assert not (remote_repo / SNAPSHOT).exists()


def test_completion_only_grant_never_publishes_ticket_text(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    configure(
        remote_repo,
        source_types=["completion_report"],
        allow_paths=[SNAPSHOT + "/records/*.json"],
    )
    fake_inventory(
        monkeypatch,
        [{"number": 1, "state": "closed", "title": "ticketprivate"}],
        [
            {
                "id": 8,
                "body": '## Completion report\n```json\n{"output":"reportword"}\n```',
            }
        ],
    )
    assert sync(remote_repo)["synced"] == 1
    assert search(remote_repo, "ticketprivate")["pointers"] == []
    assert search(remote_repo, "reportword")["pointers"]


def test_sync_rejects_symlink_namespace_before_publication(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from harness.memory import sync

    fake_inventory(
        monkeypatch, [{"number": 1, "state": "closed", "title": "ticketword"}]
    )
    directory = remote_repo / SNAPSHOT
    directory.parent.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    directory.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        sync(remote_repo)
    assert list(elsewhere.iterdir()) == []


def test_remote_report_projects_identity_and_skips_invalid_structure(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync

    fake_inventory(
        monkeypatch,
        [{"number": 1, "state": "closed"}],
        [
            {
                "id": 5,
                "body": '## Completion report\n```json\n{"output":{},"unknown":"x"}\n```',
            },
            {
                "id": 6,
                "body": '## Completion report\n```json\n{"output":"reportword","role":"developerword","ticket":"#1","outcome":"completed"}\n```',
            },
        ],
    )
    result = sync(remote_repo)
    assert result["skipped_reports"] == 1
    assert search(remote_repo, "developerword")["pointers"]


def test_cli_error_does_not_expose_tracker_output(
    remote_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.memory import sync
    directory = remote_repo / "bin"
    directory.mkdir()
    executable = directory / "gh"
    executable.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('token=errorprivate')\nsys.exit(7)\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    with pytest.raises(ValueError, match="gh failed.*exit 7") as error:
        sync(remote_repo)
    assert "errorprivate" not in str(error.value)
    assert not (remote_repo / SNAPSHOT).exists()


@pytest.mark.parametrize("host,tool", [("github.com", "gh"), ("gitlab.com", "glab")])
def test_sync_imports_terminal_records_and_marked_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, tool: str
) -> None:
    from harness.memory import sync

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            f"https://{host}/team/project.git",
        ],
        check=True,
    )
    configure(
        tmp_path,
        source_types=["task_archive", "completion_report"],
        allow_paths=[SNAPSHOT + "/records/*.json"],
    )
    calls: list[list[str]] = []

    def page(argv: list[str], repo: Path) -> list[dict[str, object]]:
        calls.append(argv)
        assert argv[0] == tool
        endpoint = argv[-1]
        if "page=2" in endpoint:
            return []
        if "comments?" in endpoint or "notes?" in endpoint:
            return [
                {
                    "id": 31,
                    "body": "## Completion report\n```json\n"
                    + json.dumps({"output": "reportword", "role": "developer"})
                    + "\n```",
                }
            ]
        pull = "pulls?" in endpoint or "merge_requests?" in endpoint
        return [
            {
                "number": 2 if pull else 1,
                "iid": 2 if pull else 1,
                "title": "pullword" if pull else "ticketword",
                "state": "closed",
                "body": "archiveword",
                "description": "archiveword",
            }
        ]

    monkeypatch.setattr(
        importlib.import_module("harness.memory.sync"), "fetch_page", page
    )
    result = sync(tmp_path)
    assert result["status"] == "synced"
    for query in ("ticketword", "pullword", "reportword"):
        pointers = search(tmp_path, query)["pointers"]
        assert isinstance(pointers, list) and pointers
        assert Path(tmp_path / pointers[0]["path"]).is_file()
    report_pointers = search(tmp_path, "reportword")["pointers"]
    assert isinstance(report_pointers, list)
    assert report_pointers[0]["status"] == "не подтверждено человеком"
    assert calls
