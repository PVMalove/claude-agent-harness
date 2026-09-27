"""Group 'environment' (#343): git and its identity, line endings, Python, uv, this repository's dev
environment and output encoding. End-to-end cases run `harness health --json` as a process with
fake `git`/`uv` executables on PATH; pure helpers are tested in-process."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harness.health.checks import environment
from harness.health.context import HealthContext

HARNESS = Path(__file__).resolve().parents[1] / "harness" / "bin" / "harness"
REAL_GIT = shutil.which("git")

# A fake tool answers canned responses keyed by its arguments (for git: after the leading
# `-c safe.directory=... -C <repo>` pairs); anything else goes to the real tool when one is given,
# otherwise it fails. Every invocation is appended to a log so tests can assert what ran.
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
        json.dumps(
            {"responses": responses, "passthrough": passthrough, "log": str(log)}
        ),
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


def _fake_git(
    bin_dir: Path, responses: Responses | None = None, *, identity: bool = True
) -> Path:
    canned: Responses = {"--version": (0, "git version 9.9.9\n", "")}
    if identity:
        canned["config --get user.name"] = (0, "Test User\n", "")
        canned["config --get user.email"] = (0, "test@example.invalid\n", "")
    else:
        canned["config --get user.name"] = (1, "", "")
        canned["config --get user.email"] = (1, "", "")
    canned.update(responses or {})
    assert REAL_GIT is not None
    return _fake_tool(bin_dir, "git", canned, passthrough=REAL_GIT)


def _fake_uv(bin_dir: Path, *, sync_code: int = 0) -> Path:
    sync_err = (
        ""
        if sync_code == 0
        else "The environment is outdated; run `uv sync` to update the environment\n"
    )
    return _fake_tool(
        bin_dir,
        "uv",
        {
            "--version": (0, "uv 9.9.9\n", ""),
            "sync --locked --check --offline": (sync_code, "", sync_err),
        },
    )


def _repo(path: Path, *, gitattributes: bool = True) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    assert REAL_GIT is not None
    subprocess.run([REAL_GIT, "init", "-q"], cwd=path, check=True)
    if gitattributes:
        (path / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    return path


def _harness_like_repo(path: Path) -> Path:
    repo = _repo(path)
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "{environment.HARNESS_PROJECT_NAME}"\nversion = "0"\n',
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return repo


def _health(
    repo: Path, *bin_dirs: Path, extra_env: dict[str, str] | None = None
) -> tuple[int, dict[str, dict[str, object]]]:
    """Run `harness health --json` with PATH made of `bin_dirs` only."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONIOENCODING", "PYTHONUTF8", "GIT_DIR", "GIT_WORK_TREE")
    }
    env["PATH"] = os.pathsep.join(str(path) for path in bin_dirs)
    env["PYTHONUTF8"] = "1"
    env.update(extra_env or {})
    result = subprocess.run(
        [sys.executable, str(HARNESS), "health", str(repo), "--json"],
        env=env,
        capture_output=True,
        check=False,
    )
    data = json.loads(result.stdout.decode("utf-8"))
    return result.returncode, {check["id"]: check for check in data["checks"]}


def _fix_command(check: dict[str, object]) -> str:
    fix = check["fix"]
    assert isinstance(fix, dict)
    command = fix["command"]
    assert isinstance(command, str)
    return command


# --- git ---------------------------------------------------------------------------------------


def test_missing_git_fails_and_skips_git_dependent_checks(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    uv_bin = tmp_path / "uv-bin"
    _fake_uv(uv_bin)

    exit_code, checks = _health(repo, uv_bin)

    assert exit_code == 1
    assert checks["environment.git"]["status"] == "fail"
    assert checks["environment.git"]["fix"] is not None
    assert checks["environment.git_identity"]["status"] == "skipped"
    assert checks["environment.line_endings"]["status"] == "skipped"
    assert checks["environment.uv"]["status"] == "ok"


def test_missing_git_with_a_project_local_skill_still_prints_the_full_report(
    tmp_path: Path,
) -> None:
    """files.overlay_locks inventories project-local skills through git (#394); without git that
    check crashes, and the registry must report it instead of aborting the whole run."""
    repo = _repo(tmp_path / "repo")
    harness_dir = repo / ".harness"
    skill = harness_dir / "skills" / "local-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: local-skill\ndescription: A project-local skill.\n---\n",
        encoding="utf-8",
    )
    (harness_dir / "harness.lock").write_text("{}\n", encoding="utf-8")
    overlays = harness_dir / "overlays"
    overlays.mkdir()
    (overlays / "local.lock.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "overlay_id": "local",
                "source": {"type": "project-local"},
                "skills": [{"name": "local-skill", "files": {"SKILL.md": "0" * 64}}],
            }
        ),
        encoding="utf-8",
    )
    uv_bin = tmp_path / "uv-bin"
    _fake_uv(uv_bin)

    exit_code, checks = _health(repo, uv_bin)

    assert exit_code == 1
    assert checks["environment.git"]["status"] == "fail"
    overlay_locks = checks["files.overlay_locks"]
    assert overlay_locks["status"] == "fail"
    assert "check_overlay_locks" in str(overlay_locks["message"])
    assert checks["environment.uv"]["status"] == "ok"
    assert "environment.output_encoding" in checks


def test_git_present_reports_its_version(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)
    _fake_uv(bin_dir)

    _, checks = _health(repo, bin_dir)

    assert checks["environment.git"]["status"] == "ok"
    assert checks["environment.git"]["message"] == "git version 9.9.9"
    assert checks["environment.git_identity"]["status"] == "ok"


def test_unset_git_identity_warns_with_git_config_command(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, identity=False)
    _fake_uv(bin_dir)

    _, checks = _health(repo, bin_dir)

    identity = checks["environment.git_identity"]
    assert identity["status"] == "warn"
    command = _fix_command(identity)
    assert "git config --global user.name" in command
    assert "git config --global user.email" in command


def test_only_the_missing_identity_key_is_in_the_fix(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir, {"config --get user.email": (1, "", "")})
    _fake_uv(bin_dir)

    _, checks = _health(repo, bin_dir)

    command = _fix_command(checks["environment.git_identity"])
    assert "user.email" in command
    assert "user.name" not in command


# --- line endings ------------------------------------------------------------------------------


def test_missing_gitattributes_warns(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo", gitattributes=False)
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)
    _fake_uv(bin_dir)

    exit_code, checks = _health(repo, bin_dir)

    assert checks["environment.gitattributes"]["status"] == "warn"
    assert checks["environment.gitattributes"]["fix"] is not None
    # A warning never changes the exit code on its own.
    assert exit_code == 1  # files.lock still fails in a bare repository
    assert checks["files.lock"]["status"] == "fail"


def test_line_ending_divergence_warns_with_the_file_list(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    ls_files = (
        "\0".join(
            [
                "i/lf    w/lf    attr/text=auto eol=lf \tclean.py",
                "i/crlf  w/crlf  attr/text=auto eol=lf \tcommitted_crlf.py",
                "i/lf    w/crlf  attr/text eol=lf      \tworktree_crlf.sh",
                "i/mixed w/mixed attr/text=auto        \tmixed.md",
                "i/crlf  w/crlf  attr/-text            \tvendor.bin",
            ]
        )
        + "\0"
    )
    _fake_git(
        bin_dir,
        {
            "ls-files --eol -z": (0, ls_files, ""),
            "config --get core.autocrlf": (0, "true\n", ""),
        },
    )
    _fake_uv(bin_dir)

    _, checks = _health(repo, bin_dir)

    line_endings = checks["environment.line_endings"]
    assert line_endings["status"] == "warn"
    message = line_endings["message"]
    assert isinstance(message, str)
    for path in ("committed_crlf.py", "worktree_crlf.sh", "mixed.md"):
        assert path in message
    assert "clean.py" not in message
    assert "vendor.bin" not in message
    assert "core.autocrlf=true" in message
    assert _fix_command(line_endings) == "git add --renormalize ."


def test_consistent_line_endings_are_ok_and_show_autocrlf_as_info(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(
        bin_dir,
        {
            "ls-files --eol -z": (0, "i/lf    w/crlf  attr/text=auto \ta.py\0", ""),
            "config --get core.autocrlf": (0, "true\n", ""),
        },
    )
    _fake_uv(bin_dir)

    _, checks = _health(repo, bin_dir)

    line_endings = checks["environment.line_endings"]
    assert line_endings["status"] == "ok"
    assert "core.autocrlf=true" in str(line_endings["message"])


@pytest.mark.parametrize(
    ("index_eol", "worktree_eol", "attributes", "expected"),
    [
        ("lf", "lf", "text=auto eol=lf", False),
        ("lf", "crlf", "text=auto", False),  # autocrlf decides; information only
        ("lf", "crlf", "", False),  # no attributes: not judged
        ("crlf", "crlf", "", False),
        ("crlf", "crlf", "-text", False),
        ("mixed", "mixed", "binary", False),
        ("crlf", "crlf", "text=auto", True),
        ("mixed", "lf", "text", True),
        ("lf", "crlf", "text eol=lf", True),
        ("lf", "crlf", "eol=lf", True),
        ("lf", "", "text eol=lf", False),  # deleted in the worktree
        ("-text", "-text", "text=auto", False),
    ],
)
def test_eol_mismatch(
    index_eol: str, worktree_eol: str, attributes: str, expected: bool
) -> None:
    assert environment.eol_mismatch(index_eol, worktree_eol, attributes) is expected


def test_parse_ls_files_eol_keeps_paths_with_spaces_and_tabs() -> None:
    output = "i/lf    w/lf    attr/text=auto eol=lf \tdir/a b.txt\0i/none  w/none  attr/        \tc\td\0"

    assert environment.parse_ls_files_eol(output) == [
        ("lf", "lf", "text=auto eol=lf", "dir/a b.txt"),
        ("none", "none", "", "c\td"),
    ]


# --- Python and uv -----------------------------------------------------------------------------


def test_missing_uv_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    git_bin = tmp_path / "git-bin"
    _fake_git(git_bin)

    exit_code, checks = _health(repo, git_bin)

    assert exit_code == 1
    assert checks["environment.uv"]["status"] == "fail"
    assert checks["environment.uv"]["fix"] is not None


def test_python_below_minimum_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = HealthContext(repo=tmp_path, lock=None, online=False)
    monkeypatch.setattr(environment, "_python_version", lambda: (3, 11, 9))

    result = environment.check_python(context)

    assert result.status == "fail"
    assert result.fix is not None
    assert "3.11.9" in result.message


def test_python_at_minimum_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = HealthContext(repo=tmp_path, lock=None, online=False)
    monkeypatch.setattr(environment, "_python_version", lambda: (3, 12, 0))

    assert environment.check_python(context).status == "ok"


def test_dev_env_sync_runs_only_in_the_harness_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)
    uv_log = _fake_uv(bin_dir)

    _, checks = _health(repo, bin_dir)

    assert checks["environment.dev_env"]["status"] == "skipped"
    assert not any("sync" in call for call in _invocations(uv_log))


def test_dev_env_in_sync_is_ok(tmp_path: Path) -> None:
    repo = _harness_like_repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)
    uv_log = _fake_uv(bin_dir, sync_code=0)

    _, checks = _health(repo, bin_dir)

    assert checks["environment.dev_env"]["status"] == "ok"
    assert ["sync", "--locked", "--check", "--offline"] in _invocations(uv_log)


def test_dev_env_out_of_sync_warns_with_uv_sync_command(tmp_path: Path) -> None:
    repo = _harness_like_repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)
    _fake_uv(bin_dir, sync_code=1)

    exit_code, checks = _health(repo, bin_dir)

    dev_env = checks["environment.dev_env"]
    assert dev_env["status"] == "warn"
    assert "uv sync --locked" in _fix_command(dev_env)
    assert "outdated" in str(dev_env["message"])
    assert exit_code == 1  # only files.* fails in this bare repository
    assert {
        check_id for check_id, check in checks.items() if check["status"] == "fail"
    } <= {check_id for check_id in checks if check_id.startswith("files.")}


# --- output encoding ---------------------------------------------------------------------------


def test_non_utf8_output_encoding_warns(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)
    _fake_uv(bin_dir)

    _, checks = _health(repo, bin_dir, extra_env={"PYTHONIOENCODING": "cp1251"})

    encoding = checks["environment.output_encoding"]
    assert encoding["status"] == "warn"
    assert "cp1251" in str(encoding["message"])
    assert encoding["fix"] is not None


def test_utf8_output_encoding_is_ok(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    _fake_git(bin_dir)
    _fake_uv(bin_dir)

    _, checks = _health(repo, bin_dir, extra_env={"PYTHONIOENCODING": "utf-8"})

    assert checks["environment.output_encoding"]["status"] == "ok"


def test_output_encoding_falls_back_to_sys_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Stream:
        encoding = "latin-1"

    monkeypatch.setattr(sys, "stdout", _Stream())
    context = HealthContext(repo=tmp_path, lock=None, online=False)

    result = environment.check_output_encoding(context)

    assert result.status == "warn"
    assert "latin-1" in result.message
