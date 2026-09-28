"""Group 'tracker' (#346): online checks of the GitHub/GitLab tracker this repository is hosted
on. End-to-end cases run `harness health --json [--online] [--fix]` as a process with fake
`git`/`gh`/`glab` executables on PATH, following the same fake-tool idiom as
test_health_checks_environment.py."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1] / "harness" / "bin" / "harness.py"
REAL_GIT = shutil.which("git")

_FAKE_TOOL = """
import json, subprocess, sys
from pathlib import Path

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
args = sys.argv[2:]
with open(spec["log"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
key = list(args)
while len(key) >= 2 and key[0] in ("-c", "-C"):
    key = key[2:]
response = spec["responses"].get(" ".join(key))
if response is not None:
    code, out, err = response
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)
if spec["passthrough"]:
    sys.exit(subprocess.run([spec["passthrough"], *args]).returncode)
sys.stderr.write("fake tool: unexpected arguments " + " ".join(args) + "\\n")
sys.exit(2)
"""

Responses = dict[str, tuple[int, str, str]]


def _fake_tool(
    bin_dir: Path, name: str, responses: Responses, *, passthrough: str | None = None
) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    log = bin_dir / f"{name}.log"
    spec = bin_dir / f"{name}.json"
    spec.write_text(
        json.dumps({"responses": responses, "passthrough": passthrough, "log": str(log)}),
        encoding="utf-8",
    )
    script = bin_dir / f"{name}_fake.py"
    script.write_text(_FAKE_TOOL, encoding="utf-8")
    if os.name == "nt":
        (bin_dir / f"{name}.cmd").write_text(
            f'@"{sys.executable}" "{script}" "{spec}" %*\r\n', encoding="utf-8"
        )
    else:
        launcher = bin_dir / name
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "{spec}" "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return log


def _invocations(log: Path) -> list[list[str]]:
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _fake_git(bin_dir: Path, responses: Responses | None = None) -> Path:
    canned: Responses = {"--version": (0, "git version 9.9.9\n", "")}
    canned.update(responses or {})
    assert REAL_GIT is not None
    return _fake_tool(bin_dir, "git", canned, passthrough=REAL_GIT)


def _fake_gh(bin_dir: Path, responses: Responses | None = None) -> Path:
    canned: Responses = {"auth status": (0, "", "")}
    canned.update(responses or {})
    return _fake_tool(bin_dir, "gh", canned)


def _fake_glab(bin_dir: Path, responses: Responses | None = None) -> Path:
    canned: Responses = {"auth status": (0, "", "")}
    canned.update(responses or {})
    return _fake_tool(bin_dir, "glab", canned)


def _repo(path: Path, *, remote: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    assert REAL_GIT is not None
    subprocess.run([REAL_GIT, "init", "-q"], cwd=path, check=True)
    if remote:
        subprocess.run(
            [REAL_GIT, "remote", "add", "origin", remote], cwd=path, check=True
        )
    return path


def _triage_labels(repo: Path) -> None:
    (repo / "docs" / "agents").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "agents" / "triage-labels.md").write_text(
        "\n".join(
            [
                "| Label | Color | Meaning |",
                "| --- | --- | --- |",
                "| `hitl` | yellow `#fbca04` | Human-in-the-loop |",
                "| `afk` | light blue `#54c1e8` | Away-from-keyboard |",
                "| `status::ready` | green `#0e8a16` | Ready |",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _health(
    repo: Path, *bin_dirs: Path, online: bool = False, fix: bool = False
) -> tuple[int, dict[str, dict[str, object]]]:
    _exit_code, _checks, _data = _health_full(repo, *bin_dirs, online=online, fix=fix)
    return _exit_code, _checks


def _health_full(
    repo: Path, *bin_dirs: Path, online: bool = False, fix: bool = False
) -> tuple[int, dict[str, dict[str, object]], dict[str, object]]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONIOENCODING", "PYTHONUTF8", "GIT_DIR", "GIT_WORK_TREE")
    }
    env["PATH"] = os.pathsep.join(str(path) for path in bin_dirs)
    env["PYTHONUTF8"] = "1"
    argv = [sys.executable, str(HARNESS), "health", str(repo), "--json"]
    if online:
        argv.append("--online")
    if fix:
        argv.append("--fix")
    result = subprocess.run(argv, env=env, capture_output=True, check=False)
    data = json.loads(result.stdout.decode("utf-8"))
    return (
        result.returncode,
        {check["id"]: check for check in data["checks"]},
        data,
    )


TRACKER_IDS = (
    "tracker.auth",
    "tracker.reachability",
    "tracker.permissions",
    "tracker.labels",
)


# --- offline (default) ---------------------------------------------------------------------------


def test_without_online_every_tracker_check_is_skipped_offline(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir, online=False)

    for check_id in TRACKER_IDS:
        assert checks[check_id]["status"] == "skipped"
        assert "офлайн" in str(checks[check_id]["message"])


# --- local tracker ---------------------------------------------------------------------------------


def test_online_with_a_non_hosted_remote_is_skipped_as_local(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="https://example.internal/team/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir, online=True)

    for check_id in TRACKER_IDS:
        assert checks[check_id]["status"] == "skipped"
        assert "локальный" in str(checks[check_id]["message"])


def test_online_with_no_remote_at_all_is_skipped_as_local(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.auth"]["status"] == "skipped"


# --- missing gh ------------------------------------------------------------------------------------


def test_online_github_without_gh_warns_and_skips_dependents(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)

    exit_code, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.auth"]["status"] == "warn"
    assert "gh" in str(checks["tracker.auth"]["message"])
    assert checks["tracker.permissions"]["status"] == "warn"
    assert checks["tracker.labels"]["status"] == "warn"
    # reachability does not depend on gh, only on git, which is present.
    assert checks["tracker.reachability"]["status"] in ("ok", "fail")
    assert exit_code in (0, 1)


# --- gh auth status ---------------------------------------------------------------------------------


def test_online_github_gh_not_authenticated_fails_auth_and_skips_dependents(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    gh_log = _fake_gh(bin_dir, {"auth status": (1, "", "not logged in")})

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.auth"]["status"] == "fail"
    # the raw (possibly sensitive) stderr of `gh auth status` never reaches the report.
    assert "not logged in" not in str(checks["tracker.auth"]["message"])
    invocations = _invocations(gh_log)
    assert ["auth", "status"] in invocations


def test_online_github_authenticated_reports_ok(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_gh(
        bin_dir,
        {
            "api repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"admin": True, "push": True, "triage": True}),
                "",
            ),
            "api --paginate repos/acme/widgets/labels": (0, "[]", ""),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.auth"]["status"] == "ok"
    assert checks["tracker.reachability"]["status"] == "ok"


# --- permissions -----------------------------------------------------------------------------------


def test_online_github_triage_only_permissions_warns_about_pull_requests(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_gh(
        bin_dir,
        {
            "api repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": False, "triage": True}),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    permissions = checks["tracker.permissions"]
    assert permissions["status"] == "warn"
    assert "недостаточно прав для PR" in str(permissions["message"])
    assert "метки доступны" in str(permissions["message"])


def test_online_gitlab_developer_access_level_has_full_permissions(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="git@gitlab.example.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_glab(
        bin_dir,
        {
            "api projects/acme%2Fwidgets": (
                0,
                json.dumps(
                    {"permissions": {"project_access": {"access_level": 30}}}
                ),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.permissions"]["status"] == "ok"


def test_online_gitlab_reporter_access_level_can_manage_labels_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="git@gitlab.example.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_glab(
        bin_dir,
        {
            "api projects/acme%2Fwidgets": (
                0,
                json.dumps(
                    {"permissions": {"project_access": {"access_level": 20}}}
                ),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    permissions = checks["tracker.permissions"]
    assert permissions["status"] == "warn"
    assert "недостаточно прав для PR" in str(permissions["message"])
    assert "метки доступны" in str(permissions["message"])


# --- labels ------------------------------------------------------------------------------------------


def test_missing_triage_labels_file_warns(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_gh(
        bin_dir,
        {
            "api repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    labels = checks["tracker.labels"]
    assert labels["status"] == "warn"
    assert "triage-labels.md" in str(labels["message"])


def test_missing_labels_warn_and_list_missing_names(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _triage_labels(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    gh_log = _fake_gh(
        bin_dir,
        {
            "api repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
            "api --paginate repos/acme/widgets/labels": (
                0,
                json.dumps([{"name": "hitl", "color": "fbca04"}]),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    labels = checks["tracker.labels"]
    assert labels["status"] == "warn"
    assert "afk" in str(labels["message"])
    assert "status::ready" in str(labels["message"])
    missing_part = str(labels["message"]).split("отсутствуют:")[1].split(";")[0]
    assert "hitl" not in missing_part
    assert not any(
        call[:2] == ["label", "create"] for call in _invocations(gh_log)
    )


def test_missing_labels_with_fix_creates_only_missing_ones(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _triage_labels(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    gh_log = _fake_gh(
        bin_dir,
        {
            "api repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
            "api --paginate repos/acme/widgets/labels": (
                0,
                json.dumps([{"name": "hitl", "color": "fbca04"}]),
                "",
            ),
            "label create afk --color #54c1e8 -R acme/widgets": (0, "", ""),
            "label create status::ready --color #0e8a16 -R acme/widgets": (0, "", ""),
        },
    )

    _exit_code, _checks, data = _health_full(repo, bin_dir, online=True, fix=True)

    invocations = _invocations(gh_log)
    created = [call for call in invocations if call[:2] == ["label", "create"]]
    created_names = {call[2] for call in created}
    assert created_names == {"afk", "status::ready"}
    assert all("--force" not in call for call in created)
    fixes_applied = data["fixes_applied"]
    assert isinstance(fixes_applied, list) and fixes_applied
    assert any("afk" in entry and "status::ready" in entry for entry in fixes_applied)


def test_color_mismatch_warns_and_never_recolors_even_with_fix(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _triage_labels(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    gh_log = _fake_gh(
        bin_dir,
        {
            "api repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
            "api --paginate repos/acme/widgets/labels": (
                0,
                json.dumps(
                    [
                        {"name": "hitl", "color": "000000"},
                        {"name": "afk", "color": "54c1e8"},
                        {"name": "status::ready", "color": "0e8a16"},
                    ]
                ),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True, fix=True)

    labels = checks["tracker.labels"]
    assert labels["status"] == "warn"
    assert "hitl" in str(labels["message"])
    invocations = _invocations(gh_log)
    assert not any(call[:2] in (["label", "create"], ["label", "edit"]) for call in invocations)


def test_labels_beyond_gh_default_page_size_are_not_reported_missing(tmp_path: Path) -> None:
    """`gh label list`/`glab label list` themselves default to a single 30-item page; the tracker
    reads labels through `api --paginate` instead, so a repository with more than 30 labels does
    not have its later labels misreported as missing (which would otherwise make `--fix` try to
    recreate labels that already exist)."""
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _triage_labels(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    # 32 filler labels ahead of the 3 canonical ones, well past gh's own default page of 30.
    filler = [{"name": f"filler-{i:03d}", "color": "cccccc"} for i in range(32)]
    canonical = [
        {"name": "hitl", "color": "fbca04"},
        {"name": "afk", "color": "54c1e8"},
        {"name": "status::ready", "color": "0e8a16"},
    ]
    gh_log = _fake_gh(
        bin_dir,
        {
            "api repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
            "api --paginate repos/acme/widgets/labels": (
                0,
                json.dumps(filler + canonical),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True, fix=True)

    labels = checks["tracker.labels"]
    assert labels["status"] == "ok"
    invocations = _invocations(gh_log)
    assert not any(call[:2] == ["label", "create"] for call in invocations)


def test_all_labels_present_and_matching_is_ok(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    _triage_labels(repo)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (0, "abc\tHEAD\n", "")})
    _fake_gh(
        bin_dir,
        {
            "api repos/acme/widgets --jq .permissions": (
                0,
                json.dumps({"push": True, "triage": True}),
                "",
            ),
            "api --paginate repos/acme/widgets/labels": (
                0,
                json.dumps(
                    [
                        {"name": "hitl", "color": "fbca04"},
                        {"name": "afk", "color": "54c1e8"},
                        {"name": "status::ready", "color": "0e8a16"},
                    ]
                ),
                "",
            ),
        },
    )

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.labels"]["status"] == "ok"


# --- reachability ------------------------------------------------------------------------------------


def test_unreachable_origin_fails_reachability(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", remote="git@github.com:acme/widgets.git")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"ls-remote origin": (128, "", "could not resolve host")})
    _fake_gh(bin_dir)

    _, checks = _health(repo, bin_dir, online=True)

    assert checks["tracker.reachability"]["status"] == "fail"
