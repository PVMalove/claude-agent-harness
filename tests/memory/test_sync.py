"""Tracker snapshots through the explicit public writer and offline search."""

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from harness.memory import search
from .test_build import configure

SNAPSHOT = ".harness/.sandboxes/memory/snapshot"


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

    monkeypatch.setattr(importlib.import_module("harness.memory.sync"), "fetch_page", page)
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
