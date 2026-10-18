"""Скрипты релиза и проверок: ограниченные по времени процессы и явные входные файлы.

Зависший git или инструмент релиза завершает скрипт его обычным путём отказа вместо бесконечного
ожидания CI; явно переданный, но отсутствующий файл тела PR не пропускает проверку.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import (
    build_parser_bundle,
    build_release,
    check_public_metadata,
    check_upstream_drift,
    release_parser_bundle,
)


def _hang(*args: object, **kwargs: object) -> object:
    raise subprocess.TimeoutExpired(["tool"], 1)


def test_public_metadata_bounds_git_log(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert check_public_metadata.git_messages("HEAD~1..HEAD") == []
    assert seen["timeout"] == check_public_metadata.GIT_TIMEOUT_SECONDS


def test_parser_bundle_bounds_the_interpreter_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_check_output(command: list[str], **kwargs: object) -> str:
        seen.update(kwargs)
        return "cp312-linux_x86_64\n"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    assert build_parser_bundle._running_pair() == "cp312-linux_x86_64"
    assert seen["timeout"] == build_parser_bundle.PROBE_TIMEOUT_SECONDS


def test_release_reports_a_hung_git_as_a_cli_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(build_release, "check_release", _hang)
    monkeypatch.setattr(sys, "argv", ["build_release.py", "--check"])

    with pytest.raises(SystemExit) as raised:
        build_release.main()

    assert raised.value.code == 2
    assert "timed out" in capsys.readouterr().err


def test_release_bounds_git_ls_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"README.md\0")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert build_release.tracked_installation_files(tmp_path) == ["README.md"]
    assert seen["timeout"] == build_release.GIT_TIMEOUT_SECONDS


def test_parser_release_reports_a_hung_tool_as_a_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _hang)

    assert (
        release_parser_bundle._run(["pip-audit"])
        == release_parser_bundle.TIMED_OUT_EXIT_CODE
    )


def test_upstream_drift_reports_a_hung_ls_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _hang)

    with pytest.raises(SystemExit) as raised:
        check_upstream_drift.latest_upstream_tag("https://example.invalid/repo.git")

    assert "cannot query https://example.invalid/repo.git" in str(raised.value.code)


def test_public_metadata_rejects_a_missing_pr_body_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "pr-body.md"
    monkeypatch.setattr(
        sys, "argv", ["check_public_metadata.py", "--pr-body-file", str(missing)]
    )

    with pytest.raises(SystemExit) as raised:
        check_public_metadata.main()

    assert raised.value.code == 2
    assert "PR body file not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("body", "expected"), [("Fix parser.\n", 0), ("Co-Authored-By: bot\n", 1)]
)
def test_public_metadata_checks_an_existing_pr_body_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, expected: int
) -> None:
    body_file = tmp_path / "pr-body.md"
    body_file.write_text(body, encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv", ["check_public_metadata.py", "--pr-body-file", str(body_file)]
    )

    assert check_public_metadata.main() == expected


def _commit(repo: Path, message: str) -> str:
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", message],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_public_metadata_skips_commits_the_default_branch_already_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An integration PR carries default-branch commits its base lacks; their published messages
    # cannot be rewritten, so only the commits new to both branches are checked.
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    for key, value in (("user.name", "dev"), ("user.email", "dev@example.invalid")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    base = _commit(repo, "feat: integration base")
    _commit(repo, "feat: squash merge\n\nCo-authored-by: someone")
    subprocess.run(["git", "-C", str(repo), "branch", "published"], check=True)
    head = _commit(repo, "fix: sync")
    monkeypatch.chdir(repo)

    for exclude, expected in ((None, 1), ("published", 0)):
        argv = ["check_public_metadata.py", "--commit-range", f"{base}..{head}"]
        if exclude:
            argv += ["--exclude-reachable-from", exclude]
        monkeypatch.setattr(sys, "argv", argv)
        assert check_public_metadata.main() == expected

    _commit(repo, "fix: new\n\nCo-authored-by: someone")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_public_metadata.py",
            "--commit-range",
            f"{base}..HEAD",
            "--exclude-reachable-from",
            "published",
        ],
    )
    assert check_public_metadata.main() == 1
