"""block-pr-merge at its process seam: merge text in quoted heredoc data that `cat` or `tee`
writes to a file passes, also in a chain with other commands, and every real PR-merge form stays
blocked in every hook profile."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.orchestration.hook_policy import parse_hook_response

HOOKS = Path(__file__).resolve().parents[2] / "harness" / "project" / "hooks"
HOOK_FILES = ("block-pr-merge.sh", "pr_commands.py", "hook_response.py")
BASH = shutil.which("bash")
PROFILES = ("minimal", "standard", "strict")
# Split, so that a shell search of this file does not trip the hook it tests.
MERGE = "gh pr " + "merge 1"
GLAB_MERGE = "glab mr " + "merge 1"
GLAB_ACCEPT = "glab mr " + "accept 1"
SCRIPT = "cd /repo && cat <<'EOF' > m.sh\n" + MERGE + "\nEOF\n"

pytestmark = pytest.mark.skipif(BASH is None, reason="the hook is a bash script")

ALLOWED = [
    pytest.param(
        "cd /repo && cat > docs/tasks/body.md <<'EOF'\nnever run "
        + MERGE
        + "\nEOF\nruff format harness && pytest -q tests/core",
        id="cat-to-file-in-a-chain",
    ),
    pytest.param(
        "cd /repo && cat <<'EOF' >> body.md\n" + GLAB_MERGE + "\nEOF\nuv run pytest -q",
        id="cat-appends-glab",
    ),
    pytest.param(
        'cd /repo && cat >| body.md <<"EOF"\n' + MERGE + "\nEOF\npytest -q",
        id="double-quoted-delimiter",
    ),
    pytest.param(
        "cd /repo && cat 1> body.md <<\\EOF\n" + MERGE + "\nEOF\npytest -q",
        id="escaped-delimiter-stdout-number",
    ),
    pytest.param(
        "mkdir -p d && cat > d/body.md <<-'EOF'\n\t"
        + GLAB_ACCEPT
        + "\n\tEOF\nmake lint",
        id="tab-stripped-glab-accept",
    ),
    pytest.param(
        "cd /repo && tee body.md <<'EOF' > /dev/null\n" + MERGE + "\nEOF\npytest",
        id="tee-to-file-stdout-discarded",
    ),
    pytest.param(
        "cd /repo && cat > body.md <<'EOF'\n"
        + MERGE
        + "\nEOF\ngh pr comment 7 --body-file body.md",
        id="inert-reader-names-the-file",
    ),
    pytest.param(
        "cd /repo && cat > body.md <<'EOF' && pytest\n" + MERGE + "\nEOF",
        id="chain-continues-on-the-operator-line",
    ),
    pytest.param(
        "cd /repo && cat > a.md <<'EOF'\n"
        + MERGE
        + "\nEOF\ncat > b.md <<'EOF'\n"
        + GLAB_ACCEPT
        + "\nEOF\npytest -q",
        id="two-writers",
    ),
]

BLOCKED = [
    pytest.param(MERGE, id="alone"),
    pytest.param("cd /repo && " + MERGE, id="in-a-chain"),
    pytest.param("cd /repo && " + GLAB_ACCEPT, id="glab-in-a-chain"),
    pytest.param(
        "cd /repo && cat > body.md <<'EOF'\n" + MERGE + "\nEOF\n" + GLAB_MERGE,
        id="merge-after-heredoc-data",
    ),
    pytest.param('cd /repo && echo "$(' + MERGE + ')"', id="command-substitution"),
    pytest.param("cd /repo && echo `" + MERGE + "`", id="backquotes"),
    pytest.param("cd /repo && eval '" + MERGE + "'", id="eval"),
    pytest.param("cd /repo && bash -c '" + GLAB_MERGE + "'", id="shell-command-string"),
    pytest.param(
        "cd /repo && bash <<'EOF'\n" + MERGE + "\nEOF", id="heredoc-to-a-shell"
    ),
    pytest.param(
        "cd /repo && bash <<'EOF'\n" + GLAB_ACCEPT + "\nEOF",
        id="glab-heredoc-to-a-shell",
    ),
    pytest.param(
        "cd /repo && python3 - <<'EOF'\nimport os; os.system('" + MERGE + "')\nEOF",
        id="heredoc-to-interpreter-stdin",
    ),
    pytest.param(SCRIPT + "bash m.sh", id="written-script-run"),
    pytest.param(SCRIPT + "cat m.sh | bash", id="written-script-piped-to-a-shell"),
    pytest.param(SCRIPT + "chmod +x m.sh && ./m.sh", id="written-script-executed"),
    pytest.param(SCRIPT + "bash *.sh", id="written-script-by-glob"),
    pytest.param(SCRIPT + "sh m.s?", id="written-script-by-pattern"),
    pytest.param(SCRIPT + "bash {m,n}.sh", id="written-script-by-brace"),
    pytest.param(
        "cd /repo && cat <<'EOF' > f | bash\n" + MERGE + "\nEOF",
        id="writer-in-a-pipeline",
    ),
    pytest.param(
        "cd /repo && tee f <<'EOF'\n" + MERGE + "\nEOF", id="tee-without-own-stdout"
    ),
    pytest.param(
        "cd /repo && tee -- -m <<'EOF' > /dev/null\n" + MERGE + "\nEOF\nbash ./-m",
        id="tee-file-after-double-dash",
    ),
    pytest.param(
        "cd /repo && cat <<EOF > f\n" + MERGE + "\nEOF", id="unquoted-delimiter"
    ),
    pytest.param(
        "cd /repo && /usr/bin/cat > f <<'EOF'\n" + MERGE + "\nEOF",
        id="writer-with-a-path",
    ),
    pytest.param(
        "cd /repo && cat > f 2>&1 <<'EOF'\n" + MERGE + "\nEOF",
        id="writer-redirects-stderr",
    ),
    pytest.param(
        "cd /repo && cat > f 1>&2 <<'EOF'\n" + MERGE + "\nEOF",
        id="writer-stdout-to-stderr",
    ),
    pytest.param(
        "cd /repo && cat 3> f <<'EOF'\n" + MERGE + "\nEOF",
        id="writer-output-on-another-descriptor",
    ),
    pytest.param(
        "cd /repo && cat {fd}> f <<'EOF'\n" + MERGE + "\nEOF",
        id="writer-output-on-a-named-descriptor",
    ),
    pytest.param(
        "cd /repo && cat > /dev/stdout <<'EOF'\n" + MERGE + "\nEOF",
        id="writer-to-a-device",
    ),
    pytest.param(
        "cd /dev/fd && cat > 1 <<'EOF'\n" + MERGE + "\nEOF",
        id="writer-to-a-relative-descriptor",
    ),
    pytest.param(
        "cd /dev && cat > stdin <<'EOF'\n" + MERGE + "\nEOF",
        id="writer-to-a-relative-standard-stream",
    ),
    pytest.param(
        "cd /repo && cat > f <<'EOF'\n" + MERGE + "\nEOF\ngh pr mer''ge 1",
        id="quote-split-merge-after-heredoc-data",
    ),
    pytest.param(
        "cd $HOME && cat > f <<'EOF'\n" + MERGE + "\nEOF", id="dollar-outside-the-body"
    ),
    pytest.param(
        "hash -p /bin/bash cat && cat > f <<'EOF'\n" + MERGE + "\nEOF",
        id="writer-rebound-by-hash",
    ),
    pytest.param(
        "shopt -s expand_aliases\nalias cat=bash\ncat > f <<'EOF'\n" + MERGE + "\nEOF",
        id="writer-rebound-by-alias",
    ),
    pytest.param(
        "mkfifo p && bash < p &\nexec > p\ncat > body.md <<'EOF'\n"
        + MERGE
        + "\nEOF\ncat body.md",
        id="shell-output-rerouted-by-exec",
    ),
]


@pytest.fixture(scope="module")
def hooks(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """block-pr-merge.sh installed in one project per hook profile."""
    installed: dict[str, Path] = {}
    for profile in PROFILES:
        project = tmp_path_factory.mktemp(profile) / "project"
        target = project / ".claude" / "hooks"
        target.mkdir(parents=True)
        for name in HOOK_FILES:
            shutil.copy2(HOOKS / name, target / name)
        (project / ".harness").mkdir()
        (project / ".harness" / "project.json").write_text(
            json.dumps({"hook_policy": {"profile": profile}}), encoding="utf-8"
        )
        installed[profile] = target / "block-pr-merge.sh"
    return installed


def _verdict(hook: Path, command: str) -> tuple[int, str, str, str]:
    """(exit code, decision, reason_code, profile) of the hook for a Bash command."""
    project = hook.parents[2]
    done = subprocess.run(
        [str(BASH), str(hook)],
        input=json.dumps({"tool_input": {"command": command}}),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
        cwd=project,
        check=False,
        timeout=60,
    )
    verdict = parse_hook_response(done.stderr)
    return (
        done.returncode,
        str(verdict["decision"]),
        str(verdict["reason_code"]),
        str(verdict["profile"]),
    )


@pytest.mark.parametrize("command", ALLOWED)
@pytest.mark.parametrize("profile", PROFILES)
def test_quoted_heredoc_data_written_to_a_file_is_allowed(
    hooks: dict[str, Path], profile: str, command: str
) -> None:
    assert _verdict(hooks[profile], command) == (0, "allow", "ok", profile)


@pytest.mark.parametrize("command", BLOCKED)
@pytest.mark.parametrize("profile", PROFILES)
def test_a_real_merge_stays_blocked(
    hooks: dict[str, Path], profile: str, command: str
) -> None:
    assert _verdict(hooks[profile], command) == (2, "block", "pr-merge", profile)
