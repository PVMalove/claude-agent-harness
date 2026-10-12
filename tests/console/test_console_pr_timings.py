"""PR creation time comes from the tracker CLI, independently of publish/merge."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness.console.pr_timings import load_reports_view
from harness.console.reports import batch_timing_text
from harness.orchestration.core.constants import STATE_REL
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from tests.console._console_ledger_fixture import _Batch


@pytest.mark.parametrize("tracker", ["github", "gitlab"])
def test_batch_time_to_pr_uses_the_matching_branch_and_creation_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tracker: str,
) -> None:
    ledger = LifecycleLedger(tmp_path / STATE_REL)
    ledger.ensure()
    batch = _Batch(ledger, "batch-pr", "#101")
    batch.record.update(
        branch="feature/issue-101-console", created_at="2026-10-12T10:00:00Z"
    )
    batch.save("awaiting-approval")
    (tmp_path / ".harness/project.json").write_text(
        json.dumps(
            {
                "tracker": {
                    "type": tracker,
                    "host": "github.com"
                    if tracker == "github"
                    else "gitlab.example.test:4443",
                    "project": "team/project",
                },
            }
        )
    )
    calls: list[list[str]] = []
    payload = [
        {
            "headRefName": "foreign",
            "source_branch": "foreign",
            "createdAt": "2026-10-12T10:01:00Z",
            "created_at": "2026-10-12T10:01:00Z",
            "url": "https://example.test/foreign",
            "web_url": "https://example.test/foreign",
        },
        {
            "headRefName": "feature/issue-101-console",
            "isCrossRepository": False,
            "source_branch": "feature/issue-101-console",
            "source_project_id": 100,
            "target_project_id": 100,
            "createdAt": "2026-10-12T12:14:00Z",
            "created_at": "2026-10-12T12:14:00Z",
            "mergedAt": "2026-10-13T13:00:00Z",
            "merged_at": "2026-10-13T13:00:00Z",
            "url": "https://example.test/pr/10",
            "web_url": "https://example.test/pr/10",
        },
    ]

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(argv[0]).stem.lower() == "git":
            return subprocess.CompletedProcess(argv, 1, "", "")
        calls.append(argv)
        assert kwargs.get("cwd") == tmp_path
        assert kwargs.get("timeout")
        if tracker == "gitlab":
            env = kwargs["env"]
            assert isinstance(env, dict)
            assert env["GITLAB_HOST"] == "gitlab.example.test:4443"
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(subprocess, "run", run)
    view = load_reports_view(tmp_path)
    [summary] = view.batches
    assert summary.pr_created_at == "2026-10-12T12:14:00Z"
    assert summary.pr_url == "https://example.test/pr/10"
    assert batch_timing_text(summary) == "до PR: 2 ч 14 мин"
    timeline = view.timeline("batch-pr")
    assert timeline is not None
    assert timeline.batch == summary
    assert len(calls) == 1
    assert calls[0][0] == ("gh" if tracker == "github" else "glab")
    assert (
        batch_timing_text(load_reports_view(tmp_path).batches[0]) == "до PR: 2 ч 14 мин"
    )


@pytest.mark.parametrize(
    "response,exit_code,expected",
    [
        ("[]", 0, "PR ещё не создан"),
        ("[]", 1, "до PR: неизвестно"),
        ("not json", 0, "до PR: неизвестно"),
        (
            '[{"headRefName":"feature/issue-101-console","isCrossRepository":false,"createdAt":"bad date","url":"https://example.test/pr"}]',
            0,
            "до PR: неизвестно",
        ),
        (
            '[{"headRefName":"feature/issue-101-console","isCrossRepository":true,"createdAt":"2026-10-12T12:00:00Z","url":"https://example.test/pr"}]',
            0,
            "до PR: неизвестно",
        ),
        (
            '[{"headRefName":"feature/issue-101-console","isCrossRepository":false,"createdAt":"2026-10-11T12:00:00Z","url":"https://example.test/pr"}]',
            0,
            "до PR: неизвестно",
        ),
    ],
)
def test_missing_and_unconfirmed_pr_times_are_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    exit_code: int,
    expected: str,
) -> None:
    ledger = LifecycleLedger(tmp_path / STATE_REL)
    ledger.ensure()
    batch = _Batch(ledger, "batch-pr", "#101")
    batch.record.update(
        branch="feature/issue-101-console", created_at="2026-10-12T10:00:00Z"
    )
    batch.save("awaiting-approval")
    (tmp_path / ".harness/project.json").write_text(
        json.dumps(
            {
                "tracker": {
                    "type": "github",
                    "host": "github.com",
                    "project": "team/project",
                }
            }
        )
    )
    before = {
        path: path.read_bytes() for path in batch.root.rglob("*") if path.is_file()
    }

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            exit_code if argv[0] == "gh" else 1,
            response if argv[0] == "gh" else "",
            "",
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert batch_timing_text(load_reports_view(tmp_path).batches[0]) == expected
    assert {
        path: path.read_bytes() for path in batch.root.rglob("*") if path.is_file()
    } == before


def test_first_pr_is_selected_by_instant_across_timezones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = LifecycleLedger(tmp_path / STATE_REL)
    ledger.ensure()
    batch = _Batch(ledger, "batch-pr", "#101")
    batch.record.update(
        branch="feature/issue-101-console", created_at="2026-10-12T10:00:00Z"
    )
    batch.save("awaiting-approval")
    (tmp_path / ".harness/project.json").write_text(
        json.dumps(
            {
                "tracker": {
                    "type": "github",
                    "host": "github.com",
                    "project": "team/project",
                }
            }
        )
    )
    payload = [
        {
            "headRefName": "feature/issue-101-console",
            "isCrossRepository": False,
            "createdAt": at,
            "url": "https://example.test/pr",
        }
        for at in ("2026-10-12T11:00:00Z", "2026-10-12T12:30:00+02:00")
    ]

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            0 if argv[0] == "gh" else 1,
            json.dumps(payload) if argv[0] == "gh" else "",
            "",
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert batch_timing_text(load_reports_view(tmp_path).batches[0]) == "до PR: 30 мин"


def test_tracker_timeout_does_not_repeat_for_every_historical_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.console import pr_timings

    ledger = LifecycleLedger(tmp_path / STATE_REL)
    ledger.ensure()
    for index in range(3):
        batch = _Batch(ledger, f"batch-{index}", f"#{index}")
        batch.record["branch"] = f"feature/issue-{index}-console"
        batch.save("awaiting-approval")
    (tmp_path / ".harness/project.json").write_text(
        json.dumps(
            {
                "tracker": {
                    "type": "github",
                    "host": "github.com",
                    "project": "team/project",
                }
            }
        )
    )
    elapsed = 0.0
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal elapsed
        if argv[0] == "gh":
            calls.append(argv)
            elapsed += 10
            raise subprocess.TimeoutExpired(argv, 10)
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(pr_timings, "monotonic", lambda: elapsed, raising=False)
    view = load_reports_view(tmp_path)
    assert len(calls) == 1
    assert all(
        batch_timing_text(batch) == "до PR: неизвестно" for batch in view.batches
    )
