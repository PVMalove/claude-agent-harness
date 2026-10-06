"""Модульные тесты таймаутов и обработки отсутствующих утилит группы 'tracker'."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from harness.health.checks import tracker
from harness.health.context import HealthContext
from harness.health.project_tracker import ProjectTracker

_GITHUB = ProjectTracker("github", "github.com", "acme/widgets", "origin")


def _online_context(tmp_path: Path) -> HealthContext:
    """Создать контекст проверки с включенным сетевым режимом."""
    return HealthContext(repo=tmp_path, lock=None, online=True)


def test_online_timeout_constant_is_ten_seconds() -> None:
    """Проверить, что константа сетевого таймаута равна 10 секундам."""
    assert tracker._ONLINE_TIMEOUT_SECONDS == 10


def test_run_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверить, что функция _run возвращает None при истечении таймаута."""

    def _raise_timeout(
        *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Имитировать истечение времени ожидания подпроцесса."""
        raise subprocess.TimeoutExpired(
            cmd="gh", timeout=tracker._ONLINE_TIMEOUT_SECONDS
        )

    monkeypatch.setattr(subprocess, "run", _raise_timeout)

    assert tracker._run(["gh", "auth", "status"]) is None


def test_run_passes_the_online_timeout_to_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверить, что функция _run передает значение таймаута в subprocess."""
    seen: dict[str, object] = {}

    def _fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        """Зафиксировать переданные параметры вызова подпроцесса."""
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    tracker._run(["git", "ls-remote", "origin"])

    assert seen["timeout"] == tracker._ONLINE_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("check_fn", "check_id"),
    [
        (tracker.check_auth, "tracker.auth"),
        (tracker.check_permissions, "tracker.permissions"),
        (tracker.check_labels, "tracker.labels"),
        (tracker.check_git_base, "tracker.git_base"),
    ],
)
def test_missing_tracker_tool_warns_instead_of_failing(
    check_fn: object,
    check_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверить, что отсутствие утилиты трекера приводит к предупреждению вместо ошибки."""
    monkeypatch.setattr(tracker, "detect_tracker", lambda _context: _GITHUB)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    context = _online_context(tmp_path)

    result = check_fn(context)  # type: ignore[operator]

    assert result.id == check_id
    assert result.status == "warn"


def test_missing_git_warns_reachability_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что отсутствие git приводит к предупреждению в проверке доступности."""
    monkeypatch.setattr(tracker, "detect_tracker", lambda _context: _GITHUB)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    context = _online_context(tmp_path)

    result = tracker.check_reachability(context)

    assert result.status == "warn"


def test_a_stalled_git_call_fails_reachability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверить, что зависший вызов git приводит к ошибке проверки доступности."""
    monkeypatch.setattr(tracker, "detect_tracker", lambda _context: _GITHUB)
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(tracker, "_run", lambda *_args, **_kwargs: None)
    context = _online_context(tmp_path)

    result = tracker.check_reachability(context)

    assert result.status == "fail"
    assert str(tracker._ONLINE_TIMEOUT_SECONDS) in result.message


# --- tracker.labels: pagination beyond gh/glab's own default page size -------------------------
#
# `gh label list`/`glab label list` cap at their own default page (30 for gh, with no single flag
# to walk every page), so a repository with more labels than that must be read through the raw
# paginated API endpoint instead. These tests run a real fake `gh`/`glab` executable as a
# subprocess (not a monkeypatched `subprocess.run`) so the exact argv `_list_repo_labels` invokes
# is what is actually exercised, proving both that every page is fetched and that a canonical
# label sitting past the old 30-item cutoff is never treated as missing nor recreated.


def _write_fake_tool(bin_dir: Path, name: str, script: str) -> Path:
    """Создать фиктивную утилиту в bin_dir для запуска тестового скрипта."""
    source = bin_dir / f"{name}_fake.py"
    source.write_text(script, encoding="utf-8")
    if os.name == "nt":
        path = bin_dir / f"{name}.cmd"
        path.write_text(f'@"{sys.executable}" "{source}" %*\r\n', encoding="utf-8")
    else:
        path = bin_dir / name
        path.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{source}" "$@"\n', encoding="utf-8"
        )
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _many_labels(count: int) -> list[dict[str, str]]:
    """Сгенерировать заданное количество уникальных меток с детерминированными цветами."""
    return [{"name": f"label-{i:03d}", "color": f"{i:06x}"} for i in range(count)]


@pytest.mark.parametrize(
    "target,tool,api_args",
    [
        (
            _GITHUB,
            "gh",
            ["--hostname", "github.com", "--paginate", "repos/acme/widgets/labels"],
        ),
        (
            ProjectTracker(
                "gitlab", "gitlab.example.test:4443", "acme/widgets", "origin"
            ),
            "glab",
            [
                "--hostname",
                "gitlab.example.test:4443",
                "--paginate",
                "projects/acme%2Fwidgets/labels",
            ],
        ),
    ],
)
def test_list_repo_labels_reads_every_page_past_the_default_cap(
    target: ProjectTracker,
    tool: str,
    api_args: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Проверить, что получение меток репозитория считывает все страницы за пределами лимита по умолчанию."""
    total = 35  # comfortably past gh label list's own default page size of 30
    all_labels = _many_labels(total)
    # The label a naive, unpaginated call would miss entirely: past index 30.
    late_label = all_labels[32]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_log = tmp_path / "calls.log"
    fake_script = f"""
import json, sys
args = sys.argv[1:]
with open({str(calls_log)!r}, "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\\n")
if args[:1] == ["api"]:
    if args[1:] != {api_args!r}:
        sys.stderr.write("unexpected api arguments: " + " ".join(args[1:]) + "\\n")
        sys.exit(1)
    sys.stdout.write({json.dumps(all_labels)!r})
    sys.exit(0)
if args[:2] == ["label", "create"]:
    sys.exit(0)
sys.stderr.write("unexpected invocation: " + " ".join(args) + "\\n")
sys.exit(1)
"""
    _write_fake_tool(bin_dir, tool, fake_script)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    executable = shutil.which(tool)
    assert executable is not None

    online_target = tracker._hosted(target)
    assert online_target is not None
    labels = tracker._list_repo_labels(
        online_target, executable, "acme/widgets", tmp_path
    )

    assert labels is not None
    assert len(labels) == total
    assert (late_label["name"], f"#{late_label['color']}") in labels

    # Now prove the check-and-fix path never treats that late label as missing, nor calls
    # `label create` for it: the canonical table asks for it (already present, past the old
    # cutoff) plus one genuinely missing label, so fix_labels does run.
    triage_labels = tmp_path / "docs" / "agents" / "triage-labels.md"
    triage_labels.parent.mkdir(parents=True, exist_ok=True)
    triage_labels.write_text(
        "| Label | Color | Meaning |\n"
        "| --- | --- | --- |\n"
        f"| `{late_label['name']}` | `#{late_label['color']}` | Already exists past page 1 |\n"
        "| `brand-new` | `#abcdef` | Genuinely missing |\n",
        encoding="utf-8",
    )
    context = HealthContext(repo=tmp_path, lock=None, online=True)
    monkeypatch.setattr(tracker, "detect_tracker", lambda _context: target)

    result = tracker.check_labels(context)

    assert result.status == "warn"
    assert (
        late_label["name"]
        not in result.message.split("отсутствуют:", 1)[-1].split(";")[0]
    )
    assert "brand-new" in result.message

    outcome = tracker.fix_labels(context, result)

    assert outcome == "созданы метки: brand-new (#abcdef)"
    create_calls = [
        line
        for line in calls_log.read_text(encoding="utf-8").splitlines()
        if "create" in line
    ]
    assert len(create_calls) == 1
    assert late_label["name"] not in create_calls[0]
    assert "brand-new" in create_calls[0]
