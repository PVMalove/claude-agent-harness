"""Установка, выбор capability и обновление harness: сценарий clean-room из `scripts/test_clean_room.py`."""

import filecmp
import hashlib
import json
import re
import subprocess
import sys
from types import SimpleNamespace

from scripts.clean_room.support import (
    HARNESS,
    ROOT,
    capture,
    count_skill_files,
    fill_agents,
    run_fails,
    run_ok,
)


def run(ctx: SimpleNamespace) -> None:
    """Установка, выбор capability и обновление harness.

    Читает из контекста: `test_root`.
    Передаёт дальше: `pv_project`, `target_home`.
    """
    test_root = ctx.test_root
    project = test_root / "project"
    foundation = test_root / "foundation"
    target_home = test_root / "home"
    for d in (project, foundation, target_home):
        d.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "init", "-q"], cwd=foundation, check=True)

    run_ok(
        HARNESS
        + [
            "init",
            str(foundation),
            "--project-type",
            "content",
            "--base-branch",
            "main",
        ]
    )
    run_ok(HARNESS + ["diff", str(foundation)])
    if not run_fails(HARNESS + ["health", str(foundation)], quiet_all=True):
        sys.exit("unresolved AGENTS.md unexpectedly passed health")
    fill_agents(foundation)
    run_ok(HARNESS + ["health", str(foundation)])
    if count_skill_files(foundation / ".harness" / "skills") != 5:
        sys.exit("expected 5 skills in foundation")
    if "- Type: content" not in (foundation / "AGENTS.md").read_text(encoding="utf-8"):
        sys.exit("AGENTS.md missing '- Type: content'")
    if "| `grilling` | `.harness/skills/grilling` |" not in (
        foundation / ".harness" / "skills" / "REGISTRY.md"
    ).read_text(encoding="utf-8"):
        sys.exit("REGISTRY.md missing grilling entry")

    run_ok(
        HARNESS
        + [
            "init",
            str(project),
            "--stack",
            "node",
            "--capability",
            "mattpocock-suite",
            "--base-branch",
            "main",
        ]
    )
    fill_agents(project)

    run_ok(HARNESS + ["diff", str(project)])
    run_ok(HARNESS + ["health", str(project)])

    if count_skill_files(project / ".harness" / "skills") != 25:
        sys.exit("expected 25 skills in project")
    if (project / ".agents" / "skills").readlink().as_posix() != "../.harness/skills":
        sys.exit(".agents/skills does not link to ../.harness/skills")
    if (project / ".claude" / "skills").readlink().as_posix() != "../.harness/skills":
        sys.exit(".claude/skills does not link to ../.harness/skills")

    list_output = capture(HARNESS + ["list", str(project)])
    lines = list_output.splitlines()
    if lines != sorted(lines):
        sys.exit("harness list output is not sorted")
    if "ask-matt" not in lines:
        sys.exit("ask-matt missing from harness list output")

    registry_path = project / ".harness" / "skills" / "REGISTRY.md"
    registry_path.write_text("corrupted\n", encoding="utf-8")
    run_ok(HARNESS + ["registry", str(project)])
    if "| `ask-matt` |" not in registry_path.read_text(encoding="utf-8"):
        sys.exit(
            "standalone 'harness registry' did not regenerate REGISTRY.md correctly"
        )

    with (project / ".harness" / "skills" / "ask-matt" / "SKILL.md").open(
        "a", encoding="utf-8"
    ) as f:
        f.write("\nlocal edit\n")
    if not run_fails(HARNESS + ["diff", str(project)], quiet_all=True):
        sys.exit("drift check unexpectedly passed")
    if not run_fails(HARNESS + ["update", str(project)], quiet_all=True):
        sys.exit("non-forced update unexpectedly overwrote a local edit")
    run_ok(HARNESS + ["update", str(project), "--force"], quiet=True)
    run_ok(HARNESS + ["health", str(project)])

    (project / ".mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")
    if not run_fails(HARNESS + ["health", str(project)], quiet_all=True):
        sys.exit("uninventoried project integration unexpectedly passed health")

    mcp_config = project / ".mcp.json"
    integrations_payload = {
        "schema": 1,
        "integrations": [
            {
                "id": "test-mcp",
                "kind": "mcp",
                "runtimes": ["codex"],
                "config": ".mcp.json",
                "sha256": hashlib.sha256(mcp_config.read_bytes()).hexdigest(),
                "secret_refs": ["TEST_MCP_TOKEN"],
                "verify": "Open a fresh session and list MCP tools.",
            }
        ],
    }
    (project / ".harness" / "integrations.json").write_text(
        json.dumps(integrations_payload, indent=2) + "\n", encoding="utf-8"
    )
    run_ok(HARNESS + ["health", str(project)])

    # Selecting a capability together with a second one that overrides the same names by a
    # different source path must fail loudly (docs/adr/0001) instead of picking one silently.
    dup_project = test_root / "dup_project"
    dup_project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=dup_project, check=True)
    if not run_fails(
        HARNESS
        + [
            "init",
            str(dup_project),
            "--capability",
            "mattpocock-suite",
            "--capability",
            "pvmalove-suite",
        ],
        quiet_all=True,
    ):
        sys.exit(
            "selecting mattpocock-suite and pvmalove-suite together unexpectedly succeeded"
        )

    # pvmalove-suite exercises extends (inherits mattpocock-suite's 25 skills), overrides (10 of
    # those 25 swapped for a first-party source under the same name) and additions (4 new names
    # with no mattpocock-suite equivalent) - docs/adr/0001. 25 + 4 = 29 distinct skill names.
    pv_project = test_root / "pv_project"
    pv_project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=pv_project, check=True)
    run_ok(
        HARNESS
        + [
            "init",
            str(pv_project),
            "--capability",
            "pvmalove-suite",
            "--base-branch",
            "main",
            "--language",
            "ru",
            "--pr-base-branch",
            "main",
            "--branch-pattern",
            "^release/rel-[0-9]+-.+",
            "--qa-gate-command",
            "echo test",
        ]
    )
    fill_agents(pv_project)
    run_ok(HARNESS + ["health", str(pv_project)])
    repo_map_health = capture(HARNESS + ["health", str(pv_project)])
    if "Repo Map: tier=minimal" not in repo_map_health:
        sys.exit("health did not report the degraded Repo Map tier")
    if "provenance: offline parser bundle unavailable" not in repo_map_health:
        sys.exit("health did not report missing Repo Map bundle provenance")
    if "КАК ИСПРАВИТЬ: установите offline parser bundle" not in repo_map_health:
        sys.exit("health did not provide the offline Repo Map remedy")
    if "ПРЕДУПРЕЖДЕНИЕ: Repo Map работает в ограниченном режиме" not in repo_map_health:
        sys.exit("health did not localize the Repo Map warning")
    if "uv run" in repo_map_health:
        sys.exit("health incorrectly offered uv run as a Repo Map dispatch remedy")
    corrupt_registry = (
        pv_project
        / ".harness"
        / ".cache"
        / "repo_map"
        / "parser_bundle"
        / "registry"
    )
    corrupt_registry.mkdir(parents=True)
    (corrupt_registry / "parser_bundle.lock.json").write_text("{invalid", encoding="utf-8")
    corrupt_bundle_health = capture(HARNESS + ["health", str(pv_project)])
    if "Repo Map: tier=minimal" not in corrupt_bundle_health:
        sys.exit("health accepted a corrupt Repo Map bundle as full tier")
    if "Repo Map: tier=full" in corrupt_bundle_health:
        sys.exit("health reported a corrupt Repo Map bundle as full tier")
    repo_map_cli = pv_project / ".harness" / "repo_map" / "repo_map.py"
    if (
        not repo_map_cli.is_file()
        or not (pv_project / ".harness" / "token_estimator.py").is_file()
        or not (pv_project / ".harness" / "repo_map" / "repo_map.schema.json").is_file()
    ):
        sys.exit("pvmalove-suite Repo Map resource missing")
    map_project = test_root / "map_project"
    map_project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=map_project, check=True)
    (map_project / "mapped.py").write_text(
        "def mapped(value: int = 1) -> int:\n    return value\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "mapped.py"], cwd=map_project, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=map_project,
        check=True,
    )
    mapped_commit = capture(
        ["git", "-C", str(map_project), "rev-parse", "HEAD"]
    ).strip()
    mapped = json.loads(
        capture(
            [
                sys.executable,
                str(repo_map_cli),
                "--repo",
                str(map_project),
                "--commit",
                mapped_commit,
            ]
        )
    )
    if not any(item["path"] == "mapped.py" for item in mapped["files"]):
        sys.exit("installed Repo Map did not read the pinned commit")

    pv_skill_count = count_skill_files(pv_project / ".harness" / "skills")
    if pv_skill_count != 31:
        sys.exit(
            f"expected 31 skills in a pvmalove-suite project (25 inherited + 6 additions), found {pv_skill_count}"
        )

    # A failed older update could remove a retired managed skill's SKILL.md but leave
    # its empty top-level directory behind. A forced update must recover instead of
    # making registry generation fail on that stale directory.
    retired_skill_dir = pv_project / ".harness" / "skills" / "run-workflow"
    retired_skill_dir.mkdir()
    run_ok(HARNESS + ["update", str(pv_project), "--force"])
    if retired_skill_dir.exists():
        sys.exit("update did not remove an empty retired skill directory")
    run_ok(HARNESS + ["health", str(pv_project)])
    if not filecmp.cmp(
        ROOT / "skills" / "first-party" / "pvmalove" / "to-spec" / "SKILL.md",
        pv_project / ".harness" / "skills" / "to-spec" / "SKILL.md",
        shallow=False,
    ):
        sys.exit("pvmalove-suite override did not install the first-party to-spec")
    to_tickets_source = (
        ROOT / "skills" / "first-party" / "pvmalove" / "to-tickets" / "SKILL.md"
    )
    to_tickets_installed = (
        pv_project / ".harness" / "skills" / "to-tickets" / "SKILL.md"
    )
    if not filecmp.cmp(to_tickets_source, to_tickets_installed, shallow=False):
        sys.exit("pvmalove-suite override did not install the first-party to-tickets")
    to_tickets_text = to_tickets_installed.read_text(encoding="utf-8")
    for required_text in (
        "enumerate every path under those directories",
        "one cheap-model advisory call",
        "stop and report blocker",
    ):
        if required_text not in to_tickets_text:
            sys.exit(f"to-tickets discovery contract is missing: {required_text}")
    for name in ("grill-me", "grill-with-docs"):
        if not filecmp.cmp(
            ROOT / "skills" / "first-party" / "pvmalove" / name / "SKILL.md",
            pv_project / ".harness" / "skills" / name / "SKILL.md",
            shallow=False,
        ):
            sys.exit(f"pvmalove-suite override did not install the first-party {name}")
    if not filecmp.cmp(
        ROOT
        / "skills"
        / "vendor"
        / "mattpocock"
        / "engineering"
        / "diagnosing-bugs"
        / "SKILL.md",
        pv_project / ".harness" / "skills" / "diagnosing-bugs" / "SKILL.md",
        shallow=False,
    ):
        sys.exit(
            "pvmalove-suite unexpectedly changed a skill it neither overrides nor adds"
        )
    qa_gate_skill = pv_project / ".harness" / "skills" / "qa-gate" / "SKILL.md"
    if not qa_gate_skill.is_file():
        sys.exit("pvmalove-suite addition qa-gate missing")
    if "test_summary.py" not in qa_gate_skill.read_text(encoding="utf-8"):
        sys.exit("qa-gate does not route quality commands through the summary wrapper")
    pytest_summary = (
        pv_project / ".harness" / "skills" / "qa-gate" / "scripts" / "test_summary.py"
    )
    if pytest_summary.exists():
        pytest_summary.unlink()
    run_ok(HARNESS + ["update", str(pv_project), "--force"])
    if not pytest_summary.is_file():
        sys.exit("update did not install the qa-gate pytest summary wrapper")
    passing_summary = subprocess.run(
        [
            sys.executable,
            str(pytest_summary),
            "--",
            sys.executable,
            "-c",
            "print('3 passed in 0.01s'); print('PASSING_NOISE')",
        ],
        cwd=pv_project,
        capture_output=True,
        text=True,
        check=False,
    )
    if passing_summary.returncode != 0:
        sys.exit("pytest summary wrapper rejected a passing command")
    if "PASS" not in passing_summary.stdout or "3 passed" not in passing_summary.stdout:
        sys.exit("pytest summary wrapper did not report a compact pass result")
    if "PASSING_NOISE" in passing_summary.stdout:
        sys.exit("pytest summary wrapper leaked passing command output")
    failing_summary = subprocess.run(
        [
            sys.executable,
            str(pytest_summary),
            "--",
            sys.executable,
            "-c",
            (
                "import sys; print('=== short test summary info ==='); "
                "print('FAILED tests/test_checkout.py::test_quote - AssertionError: gho_abcdefghijklmnopqrstuvwxyz1234567890'); "
                "print('Traceback (most recent call last):'); "
                "print('  File tests/test_checkout.py, line 12, in test_quote'); "
                "print('RuntimeError: gho_abcdefghijklmnopqrstuvwxyz1234567890'); "
                "print('src/check.py:7:4: error: incompatible types'); "
                "print('1 failed, 2 passed in 0.01s'); sys.exit(1)"
            ),
        ],
        cwd=pv_project,
        capture_output=True,
        text=True,
        check=False,
    )
    if failing_summary.returncode != 1:
        sys.exit(
            "pytest summary wrapper did not preserve the failing command exit code"
        )
    if "tests/test_checkout.py::test_quote" not in failing_summary.stdout:
        sys.exit("pytest summary wrapper did not retain the failed test node ID")
    for expected_diagnostic in (
        "Traceback: RuntimeError: <REDACTED_GITHUB_TOKEN>",
        "Error: src/check.py:7:4: incompatible types",
    ):
        if expected_diagnostic not in failing_summary.stdout:
            sys.exit(
                "pytest summary wrapper did not extract a structured failure diagnostic"
            )
    if "gho_abcdefghijklmnopqrstuvwxyz1234567890" in failing_summary.stdout:
        sys.exit(
            "pytest summary wrapper leaked a secret into its bounded summary"
        )
    log_match = re.search(
        r"^Full log: (.+)$", failing_summary.stdout, flags=re.MULTILINE
    )
    if not log_match:
        sys.exit("pytest summary wrapper did not provide a failure-log path")
    failure_log = pv_project / log_match.group(1).strip()
    if (
        not failure_log.is_file()
        or "gho_abcdefghijklmnopqrstuvwxyz1234567890"
        in failure_log.read_text(encoding="utf-8")
    ):
        sys.exit("pytest summary wrapper did not save a sanitized failure log")
    ctx.pv_project = pv_project
    ctx.target_home = target_home
