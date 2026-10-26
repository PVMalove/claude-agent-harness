"""Tracker titles are optional, bounded, read-only console observations."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from harness.console.task_titles import load_task_titles


def test_title_queries_share_one_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path)
    clock = Mock(side_effect=[0.0, 0.0, 7.0, 10.0])
    timeouts: list[object] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(argv[0]).stem == "git":
            return subprocess.CompletedProcess(argv, 1, "", "")
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, '{"title":"Подтверждено"}', "")

    monkeypatch.setattr(subprocess, "run", run)

    assert load_task_titles(tmp_path, ["#1", "#2", "#3"], clock=clock) == {
        "#1": "Подтверждено",
        "#2": "Подтверждено",
    }
    assert timeouts == [10.0, 3.0]


def _project(repo: Path, tracker: str = "github") -> None:
    (repo / ".harness").mkdir()
    (repo / ".harness/project.json").write_text(
        json.dumps(
            {
                "tracker": {
                    "type": tracker,
                    "host": "github.com",
                    "project": "team/project",
                }
            }
        )
    )


def test_gitlab_titles_keep_the_configured_port_and_subgroups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness/project.json").write_text(
        '{"tracker":{"type":"gitlab","host":"gitlab.example.test:4443",'
        '"project":"group/sub/project"}}'
    )

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(argv[0]).stem == "git":
            return subprocess.CompletedProcess(argv, 1, "", "")
        assert argv == ["glab", "api", "projects/group%2Fsub%2Fproject/issues/42"]
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["GITLAB_HOST"] == "gitlab.example.test:4443"
        return subprocess.CompletedProcess(argv, 0, '{"title":"Название задачи"}', "")

    monkeypatch.setattr(subprocess, "run", run)

    assert load_task_titles(tmp_path, ["#42"]) == {"#42": "Название задачи"}


def test_titles_use_the_explicit_github_project_once_per_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path)
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(argv[0]).stem == "git":
            return subprocess.CompletedProcess(argv, 1, "", "")
        calls.append(argv)
        assert kwargs["cwd"] == tmp_path
        assert kwargs["stdin"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"title": "Проверка [bold]сессии"}), ""
        )

    monkeypatch.setattr(subprocess, "run", run)

    assert load_task_titles(tmp_path, ["#101", "#101", "101"]) == {
        "#101": "Проверка [bold]сессии",
        "101": "Проверка [bold]сессии",
    }
    assert calls == [
        [
            "gh",
            "issue",
            "view",
            "101",
            "--repo",
            "github.com/team/project",
            "--json",
            "title",
        ]
    ]


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        "null",
        "[]",
        "{}",
        '{"title":null}',
        '{"title":42}',
        '{"title":""}',
        '{"title":"  "}',
    ],
)
def test_unconfirmed_titles_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    _project(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, response, ""),
    )

    assert load_task_titles(tmp_path, ["#1"]) == {}


@pytest.mark.parametrize("failure", ["missing", "timeout", "401", "403", "404"])
def test_failed_title_reads_do_not_hide_other_confirmed_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _project(tmp_path)

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(argv[0]).stem == "git":
            return subprocess.CompletedProcess(argv, 1, "", "")
        if argv[3] == "1":
            if failure == "missing":
                raise FileNotFoundError("gh")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 1)
            return subprocess.CompletedProcess(argv, 1, "", failure)
        return subprocess.CompletedProcess(argv, 0, '{"title":"Другая задача"}', "")

    monkeypatch.setattr(subprocess, "run", run)

    assert load_task_titles(tmp_path, ["#1", "#2"]) == {"#2": "Другая задача"}


@pytest.mark.parametrize(
    "tickets", [[], ["unknown", "https://github.com/other/repo/issues/1", "#0"]]
)
def test_no_supported_tickets_needs_no_external_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tickets: list[str]
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("no process should start without supported tickets")

    monkeypatch.setattr(subprocess, "run", forbidden)

    assert load_task_titles(tmp_path, tickets) == {}


def test_local_tracker_does_not_query_a_hosted_tracker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness/project.json").write_text('{"tracker":{"type":"local"}}')

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert Path(argv[0]).stem == "git"
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    assert load_task_titles(tmp_path, ["#1"]) == {}
