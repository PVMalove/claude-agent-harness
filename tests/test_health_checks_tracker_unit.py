"""In-process unit tests for the tracker group's timeout and missing-tool contract (ticket #346):
every external call gets a 10 second timeout, and a missing gh/glab warns instead of failing the
whole check. The end-to-end scenarios (fake gh/glab/git on PATH, run as a subprocess) live in
test_health_checks_tracker.py; these tests instead monkeypatch subprocess.run and shutil.which
directly so the timeout and missing-tool paths are exercised without spawning a process."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from harness.health.checks import tracker
from harness.health.context import HealthContext


def _online_context(tmp_path: Path) -> HealthContext:
    return HealthContext(repo=tmp_path, lock=None, online=True)


def test_online_timeout_constant_is_ten_seconds() -> None:
    assert tracker._ONLINE_TIMEOUT_SECONDS == 10


def test_run_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_timeout(
        *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd="gh", timeout=tracker._ONLINE_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", _raise_timeout)

    assert tracker._run(["gh", "auth", "status"]) is None


def test_run_passes_the_online_timeout_to_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake_run(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
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
    ],
)
def test_missing_tracker_tool_warns_instead_of_failing(
    check_fn: object,
    check_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracker, "detect_tracker", lambda _context: ("github", "acme/widgets"))
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    context = _online_context(tmp_path)

    result = check_fn(context)  # type: ignore[operator]

    assert result.id == check_id
    assert result.status == "warn"


def test_missing_git_warns_reachability_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tracker, "detect_tracker", lambda _context: ("github", "acme/widgets")
    )
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    context = _online_context(tmp_path)

    result = tracker.check_reachability(context)

    assert result.status == "warn"


def test_a_stalled_git_call_fails_reachability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        tracker, "detect_tracker", lambda _context: ("github", "acme/widgets")
    )
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
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _many_labels(count: int) -> list[dict[str, str]]:
    """`count` distinct labels, each with its own deterministic color, as the tracker API would
    return them - well past any single-page cap."""
    return [{"name": f"label-{i:03d}", "color": f"{i:06x}"} for i in range(count)]


@pytest.mark.parametrize("tracker_name,tool,api_path", [
    ("github", "gh", "repos/acme/widgets/labels"),
    ("gitlab", "glab", "projects/acme%2Fwidgets/labels"),
])
def test_list_repo_labels_reads_every_page_past_the_default_cap(
    tracker_name: str,
    tool: str,
    api_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total = 35  # comfortably past gh label list's own default page size of 30
    all_labels = _many_labels(total)
    # The label a naive, unpaginated call would miss entirely: past index 30.
    late_label = all_labels[32]

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_log = tmp_path / "calls.log"
    fake_script = f"""
echo "$@" >> {calls_log}
if [ "$1" = "api" ]; then
    if [ "$3" != "{api_path}" ]; then
        echo "unexpected api path: $3" >&2
        exit 1
    fi
    cat <<'JSON'
{json.dumps(all_labels)}
JSON
    exit 0
fi
if [ "$1" = "label" ] && [ "$2" = "create" ]; then
    exit 0
fi
echo "unexpected invocation: $@" >&2
exit 1
"""
    _write_fake_tool(bin_dir, tool, fake_script)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    executable = shutil.which(tool)
    assert executable is not None

    labels = tracker._list_repo_labels(tracker_name, executable, "acme/widgets", tmp_path)

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
    monkeypatch.setattr(tracker, "detect_tracker", lambda _context: (tracker_name, "acme/widgets"))

    result = tracker.check_labels(context)

    assert result.status == "warn"
    assert late_label["name"] not in result.message.split("отсутствуют:", 1)[-1].split(";")[0]
    assert "brand-new" in result.message

    outcome = tracker.fix_labels(context, result)

    assert outcome == "созданы метки: brand-new (#abcdef)"
    create_calls = [
        line for line in calls_log.read_text(encoding="utf-8").splitlines() if "create" in line
    ]
    assert len(create_calls) == 1
    assert late_label["name"] not in create_calls[0]
    assert "brand-new" in create_calls[0]
