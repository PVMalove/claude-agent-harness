"""Установка, выбор capability и обновление harness: сценарий clean-room из `scripts/test_clean_room.py`."""

import filecmp
import hashlib
import json
import re
import subprocess
import sys
from types import SimpleNamespace

from harness.bin import harness as harness_cli

from scripts.clean_room.support import (
    HARNESS,
    apply_patch,
    assert_contract_link,
    ROOT,
    capture,
    capture_json,
    check_technical_english,
    count_skill_files,
    fail_json,
    fill_agents,
    find_check,
    run_fails,
    run_health,
    run_ok,
    starts_with_text,
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
    check_technical_english(foundation)
    # Model an older standard installation with project-owned entry points.
    original_entries = {
        name: (foundation / name).read_bytes() for name in ("AGENTS.md", "CLAUDE.md")
    }
    old_agents = "# Local instructions\n\nKeep the project glossary in Russian.\n"
    old_claude = "# Local Claude instructions\n\nRun the focused checks first.\n"
    (foundation / "AGENTS.md").write_text(old_agents, encoding="utf-8")
    (foundation / "CLAUDE.md").write_text(old_claude, encoding="utf-8")
    contract = foundation / ".harness/docs/technical-english.md"
    contract.unlink()
    old_lock_path = foundation / ".harness/harness.lock"
    old_lock = json.loads(old_lock_path.read_text(encoding="utf-8"))
    old_lock["files"].pop(".harness/docs/technical-english.md")
    old_lock_path.write_text(json.dumps(old_lock), encoding="utf-8")
    update_output = capture(HARNESS + ["update", str(foundation)])
    if (
        contract.read_bytes()
        != (ROOT / "harness/docs/technical-english.md").read_bytes()
    ):
        sys.exit("standard update did not deliver the managed contract")
    for name, original in (("AGENTS.md", old_agents), ("CLAUDE.md", old_claude)):
        if (foundation / name).read_text(encoding="utf-8") != original:
            sys.exit("update changed project-owned instructions before approval")
        if f"--- a/{name}" not in update_output or f"+++ b/{name}" not in update_output:
            sys.exit("update did not show a reviewable entry-point addition")
    diff_output = capture(HARNESS + ["diff", str(foundation)])
    patch = diff_output[diff_output.index("diff --git ") :]
    if patch != update_output[update_output.index("diff --git ") :]:
        sys.exit("diff and update proposed different seed adaptations")
    # Approval is an ordinary edit/patch after review; the CLI never applies seeds.
    apply_patch(patch, foundation)
    check_technical_english(foundation)
    approved = {
        name: (foundation / name).read_bytes() for name in ("AGENTS.md", "CLAUDE.md")
    }
    for name, original in (("AGENTS.md", old_agents), ("CLAUDE.md", old_claude)):
        if not starts_with_text(approved[name], original):
            sys.exit("approved additions lost local instructions")
    repeated = capture(HARNESS + ["update", str(foundation)])
    repeated += capture(HARNESS + ["diff", str(foundation)])
    if "diff --git " in repeated:
        sys.exit("repeat update proposed an already connected contract link")
    if any(
        (foundation / name).read_bytes() != content
        for name, content in approved.items()
    ):
        sys.exit("repeat update changed approved project instructions")
    check_technical_english(foundation)

    # An existing import in either direction already reaches a mandatory source.
    (foundation / "AGENTS.md").write_text(
        old_agents + "\n@CLAUDE.md\n", encoding="utf-8"
    )
    if capture_json(HARNESS + ["diff", str(foundation), "--json"])[
        "seed_link_proposals"
    ]:
        sys.exit("diff proposed a duplicate for an existing entry-point transition")
    # A passive reference needs an explicit obligation, without a second link.
    passive = (
        old_claude + "\nSee [Technical English](.harness/docs/technical-english.md).\n"
    )
    (foundation / "CLAUDE.md").write_text(passive, encoding="utf-8")
    proposal_output = capture(HARNESS + ["update", str(foundation)])
    if "no recognized mandatory reading instruction" not in proposal_output:
        sys.exit("ambiguous reference did not explain the proposed obligation")
    patch = proposal_output[proposal_output.index("diff --git ") :]
    if "technical-english.md" in "\n".join(
        line
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ):
        sys.exit("passive reference proposal duplicated an existing contract link")
    apply_patch(patch, foundation)
    if capture_json(HARNESS + ["diff", str(foundation), "--json"])[
        "seed_link_proposals"
    ]:
        sys.exit("approved passive-reference obligation was proposed again")

    # A valid Markdown title does not disconnect an existing mandatory link.
    for title in ('"Shared contract"', "'Shared contract'", "(Shared contract)"):
        titled = (
            old_agents + "\nBefore the first English handoff, agents must read "
            f"[Technical English](.harness/docs/technical-english.md {title}).\n"
        )
        (foundation / "AGENTS.md").write_text(titled, encoding="utf-8")
        (foundation / "CLAUDE.md").write_text(
            old_claude + "\n@AGENTS.md\n", encoding="utf-8"
        )
        titled_entries = {
            name: (foundation / name).read_bytes()
            for name in ("AGENTS.md", "CLAUDE.md")
        }
        for _ in range(2):
            if capture_json(HARNESS + ["diff", str(foundation), "--json"])[
                "seed_link_proposals"
            ]:
                sys.exit(
                    f"diff proposed a duplicate for a mandatory link with title {title}"
                )
            if "diff --git " in capture(HARNESS + ["update", str(foundation)]):
                sys.exit(
                    f"update proposed a duplicate for a mandatory link with title {title}"
                )
            if any(
                (foundation / name).read_bytes() != content
                for name, content in titled_entries.items()
            ):
                sys.exit(
                    "repeated update changed entry points with a titled contract link"
                )

    # A fenced example cannot supply the obligation for a passive reference.
    for fence in ("```", "~~~"):
        fenced = (
            old_agents
            + "\nSee [Technical English](.harness/docs/technical-english.md).\n\n"
            + fence
            + "\nBefore the first English handoff, agents must read the contract linked above.\n"
            + fence
            + "\n"
        )
        (foundation / "AGENTS.md").write_text(fenced, encoding="utf-8")
        before = {
            name: (foundation / name).read_bytes()
            for name in ("AGENTS.md", "CLAUDE.md")
        }
        proposals = capture_json(HARNESS + ["diff", str(foundation), "--json"])[
            "seed_link_proposals"
        ]
        if [item["path"] for item in proposals] != ["AGENTS.md"]:
            sys.exit("fenced example suppressed the required reading obligation")
        output = capture(HARNESS + ["update", str(foundation)])
        if "no recognized mandatory reading instruction" not in output:
            sys.exit("update treated a fenced example as an active reading obligation")
        patch = output[output.index("diff --git ") :]
        if patch != proposals[0]["diff"]:
            sys.exit("diff and update disagree on the missing non-fenced obligation")
        if any(
            (foundation / name).read_bytes() != content
            for name, content in before.items()
        ):
            sys.exit("update changed the fenced example before approval")
        apply_patch(patch, foundation)
        if not (foundation / "AGENTS.md").read_bytes().startswith(before["AGENTS.md"]):
            sys.exit("approved obligation changed the existing fenced example")
        if capture_json(HARNESS + ["diff", str(foundation), "--json"])[
            "seed_link_proposals"
        ] or "diff --git " in capture(HARNESS + ["update", str(foundation)]):
            sys.exit("approved non-fenced obligation was proposed again")

    for name, content in original_entries.items():
        (foundation / name).write_bytes(content)

    run_ok(HARNESS + ["diff", str(foundation)])
    if not run_fails(HARNESS + ["health", str(foundation)], quiet_all=True):
        sys.exit("unresolved AGENTS.md unexpectedly passed health")
    fill_agents(foundation)
    run_health(foundation)
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
    check_technical_english(project)

    run_ok(HARNESS + ["diff", str(project)])
    run_health(project)

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
    run_health(project)

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
    run_health(project)

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

    # pvmalove-suite inherits 25 upstream skills, overrides 11 of them, and adds 7 names;
    # the clean-room check below expects 32 distinct installed skills (ADR 0001).
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
    # Provenance matters: target docs/agents are intentionally identical to their
    # templates, so comparing bytes against root docs/ would reject a valid install.
    root_docs = (ROOT / "docs").resolve()
    selected = ["pvmalove-suite"]
    sources = harness_cli.selected_skills(selected) + harness_cli.selected_resources(
        selected
    )
    if any(source.resolve().is_relative_to(root_docs) for source in sources):
        sys.exit("package_files selected a source from repository docs/")
    payload = harness_cli.package_files(selected)
    if any(not path.startswith(".harness/") for path in payload):
        sys.exit("package_files emitted a path outside .harness/")
    for destination, expected in payload.items():
        if (pv_project / destination).read_bytes() != expected:
            sys.exit(f"installed package differs from selected source: {destination}")
    templates = harness_cli.PROJECT_TEMPLATE_DIR / "docs-agents"
    installed_docs = pv_project / "docs" / "agents"
    if {path.name for path in installed_docs.glob("*.md")} != {
        path.name for path in templates.glob("*.md")
    }:
        sys.exit("target docs/agents did not come only from project templates")
    for source in templates.glob("*.md"):
        if (installed_docs / source.name).read_bytes() != source.read_bytes():
            sys.exit(f"installed guide differs from project template: {source.name}")
    fill_agents(pv_project)
    check_technical_english(pv_project)
    # Existing root entry points are already connected; only the older agent seed
    # needs adaptation. A missing final newline must survive the reviewed patch.
    for name in ("code-review-spec.md", "code-review-standards.md"):
        entry = pv_project / ".claude/agents" / name
        entry.write_text(
            entry.read_text(encoding="utf-8") + "\n@../../AGENTS.md\n", encoding="utf-8"
        )
    agent = pv_project / ".claude/agents/pr-composer.md"
    original_agent = (
        agent.read_bytes() + b"\nLocal review instructions without final newline"
    )
    agent.write_bytes(original_agent)
    untouched = pv_project / "docs/local-agent-notes.md"
    untouched.write_text(
        "Local notes are not a harness entry point.\n", encoding="utf-8"
    )
    seed_snapshot = {
        path: path.read_bytes()
        for path in (
            pv_project / "AGENTS.md",
            pv_project / "CLAUDE.md",
            agent,
            untouched,
        )
    }
    output = capture(HARNESS + ["update", str(pv_project)])
    if any(path.read_bytes() != content for path, content in seed_snapshot.items()):
        sys.exit("pvmalove update modified an unapproved seed")
    proposal = capture_json(HARNESS + ["diff", str(pv_project), "--json"])[
        "seed_link_proposals"
    ]
    if [item["path"] for item in proposal] != [".claude/agents/pr-composer.md"]:
        sys.exit(
            "update proposed links for already connected or unrelated entry points"
        )
    patch = output[output.index("diff --git ") :]
    if patch != proposal[0]["diff"]:
        sys.exit("JSON diff differs from the visible update proposal")
    apply_patch(patch, pv_project)
    assert_contract_link(
        agent, pv_project / ".harness/docs/technical-english.md", "installed agent seed"
    )
    if not agent.read_bytes().startswith(original_agent):
        sys.exit("approved agent addition lost local instructions")
    approved_agent = agent.read_bytes()
    if "diff --git " in capture(HARNESS + ["update", str(pv_project)]):
        sys.exit("repeat pvmalove update proposed duplicate links")
    if capture_json(HARNESS + ["diff", str(pv_project), "--json"])[
        "seed_link_proposals"
    ]:
        sys.exit("repeat JSON diff proposed duplicate links")
    if (
        agent.read_bytes() != approved_agent
        or untouched.read_bytes() != seed_snapshot[untouched]
    ):
        sys.exit("repeat pvmalove update changed local instructions")
    # Keep subsequent installer baseline checks independent of approved seed edits.
    agent.write_bytes(original_agent)

    run_health(pv_project)
    repo_map_health = find_check(
        capture_json(HARNESS + ["health", str(pv_project), "--json"]), "repo_map.tier"
    )
    if repo_map_health["status"] != "warn":
        sys.exit("health did not mark the degraded Repo Map tier as a warning")
    if not repo_map_health["message"].startswith("Repo Map: tier=minimal"):
        sys.exit("health did not report the degraded Repo Map tier")
    if (
        "provenance: offline parser bundle unavailable"
        not in repo_map_health["message"]
    ):
        sys.exit("health did not report missing Repo Map bundle provenance")
    repo_map_fix = repo_map_health["fix"]["text"] if repo_map_health["fix"] else ""
    if "установите offline parser bundle" not in repo_map_fix:
        sys.exit("health did not provide the offline Repo Map remedy")
    if "uv run" in repo_map_fix:
        sys.exit("health incorrectly offered uv run as a Repo Map dispatch remedy")
    corrupt_registry = (
        pv_project
        / ".harness"
        / ".sandboxes"
        / "cache"
        / "repo_map"
        / "parser_bundle"
        / "registry"
    )
    corrupt_registry.mkdir(parents=True)
    (corrupt_registry / "parser_bundle.lock.json").write_text(
        "{invalid", encoding="utf-8"
    )
    corrupt_bundle_health = find_check(
        capture_json(HARNESS + ["health", str(pv_project), "--json"]), "repo_map.tier"
    )
    if not corrupt_bundle_health["message"].startswith("Repo Map: tier=minimal"):
        sys.exit("health accepted a corrupt Repo Map bundle as full tier")
    if corrupt_bundle_health["status"] == "ok":
        sys.exit("health reported a corrupt Repo Map bundle as full tier")
    repo_map_cli = pv_project / ".harness" / "repo_map" / "repo_map.py"
    if (
        not repo_map_cli.is_file()
        or not (pv_project / ".harness" / "token_estimator.py").is_file()
        or not (pv_project / ".harness" / "repo_map" / "repo_map.schema.json").is_file()
    ):
        sys.exit("pvmalove-suite Repo Map resource missing")
    if not (pv_project / ".harness" / "health" / "registry.py").is_file():
        sys.exit("pvmalove-suite health resource missing")
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
    if pv_skill_count != 32:
        sys.exit(
            f"expected 32 skills in a pvmalove-suite project (25 inherited + 7 additions), found {pv_skill_count}"
        )

    # A failed older update could remove a retired managed skill's SKILL.md but leave
    # its empty top-level directory behind. A forced update must recover instead of
    # making registry generation fail on that stale directory.
    retired_skill_dir = pv_project / ".harness" / "skills" / "run-workflow"
    retired_skill_dir.mkdir()
    run_ok(HARNESS + ["update", str(pv_project), "--force"])
    if retired_skill_dir.exists():
        sys.exit("update did not remove an empty retired skill directory")
    run_health(pv_project)
    check_tracker_field(pv_project)
    check_tracker_from_origin(test_root)
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
    for name in ("grill-me", "grill-with-docs", "diagnosing-bugs", "architect"):
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
        / "research"
        / "SKILL.md",
        pv_project / ".harness" / "skills" / "research" / "SKILL.md",
        shallow=False,
    ):
        sys.exit(
            "pvmalove-suite unexpectedly changed a skill it neither overrides nor adds"
        )
    if not filecmp.cmp(
        ROOT / "harness" / "docs" / "project-memory.md",
        pv_project / ".harness" / "docs" / "project-memory.md",
        shallow=False,
    ):
        sys.exit("pvmalove-suite did not install the interactive memory contract")
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
        sys.exit("pytest summary wrapper leaked a secret into its bounded summary")
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


def check_tracker_field(pv_project) -> None:
    """Поле tracker в установленном проекте (docs/adr/0011): discovery обоих runtime и health.

    Проект без origin получает при установке project.json без поля tracker. С добавленным полем
    discovery-ссылки (`.agents/skills` для Codex и `.claude/skills`), `AGENTS.md`, реестр навыков и
    `files.project_json` остаются ok, а `tracker.project` берёт трекер из поля; неизвестный ключ
    внутри tracker роняет health. Исходные байты project.json восстанавливаются: по нему дальше
    работают сценарии hooks.
    """
    project_json = pv_project / ".harness" / "project.json"
    original = project_json.read_bytes()
    data = json.loads(original)
    if "tracker" in data:
        sys.exit("install wrote a tracker field into a project without origin")
    if not (pv_project / ".harness" / "health" / "project_tracker.py").is_file():
        sys.exit(
            "pvmalove-suite health resource is missing the project tracker resolver"
        )
    if not (pv_project / ".agents" / "skills" / "qa-gate" / "SKILL.md").is_file():
        sys.exit("Codex discovery path .agents/skills does not expose installed skills")
    data["tracker"] = {
        "type": "gitlab",
        "host": "git.example.test:4443",
        "project": "group/sub/project",
    }
    project_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    report = capture_json(HARNESS + ["health", str(pv_project), "--json"])
    for check_id in (
        "files.project_json",
        "files.discovery_links",
        "files.agents_md",
        "files.skill_registry",
    ):
        if find_check(report, check_id)["status"] != "ok":
            sys.exit(f"health with a tracker field did not keep {check_id} ok")
    tracker = find_check(report, "tracker.project")
    if tracker["status"] != "ok" or tracker["message"] != (
        "трекер проекта: gitlab, хост git.example.test:4443, проект group/sub/project "
        "(источник: поле tracker)"
    ):
        sys.exit("health did not resolve the project tracker from the tracker field")
    data["tracker"]["unexpected"] = True
    project_json.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    invalid = find_check(
        fail_json(HARNESS + ["health", str(pv_project), "--json"]), "files.project_json"
    )
    if (
        invalid["status"] != "fail"
        or "tracker has unknown field(s): unexpected" not in invalid["message"]
    ):
        sys.exit("health accepted an unknown key inside the tracker field")
    project_json.write_bytes(original)
    run_health(pv_project)


def check_tracker_from_origin(test_root) -> None:
    """Поле tracker из GitLab- и GitHub-origin при установке (docs/adr/0011).

    Install без терминала и флагов трекера выводит тип, хост с портом и проект с подгруппами из
    origin; userinfo в project.json не попадает, `files.project_json` ok, а `tracker.project` берёт
    трекер из записанного поля.
    """
    remotes = {
        "gitlab": (
            "https://ci-user@gitlab.example.test:4443/group/sub/project.git",
            {
                "type": "gitlab",
                "host": "gitlab.example.test:4443",
                "project": "group/sub/project",
            },
        ),
        "github": (
            "git@github.com:acme/widgets.git",
            {"type": "github", "host": "github.com", "project": "acme/widgets"},
        ),
    }
    for name, (remote, expected) in remotes.items():
        project = test_root / f"tracker_{name}_project"
        project.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", remote], cwd=project, check=True
        )
        run_ok(
            HARNESS
            + [
                "init",
                str(project),
                "--capability",
                "pvmalove-suite",
                "--language",
                "ru",
                "--pr-base-branch",
                "main",
                "--branch-pattern",
                "^feature/issue-[0-9]+-.+",
                "--qa-gate-command",
                "echo test",
            ],
            quiet=True,
        )
        text = (project / ".harness" / "project.json").read_text(encoding="utf-8")
        if json.loads(text).get("tracker") != expected or "ci-user" in text:
            sys.exit(f"install did not write the {name} tracker derived from origin")
        fill_agents(project)
        report = capture_json(HARNESS + ["health", str(project), "--json"])
        if find_check(report, "files.project_json")["status"] != "ok":
            sys.exit(f"the {name} tracker field written by install failed health")
        tracker = find_check(report, "tracker.project")
        if tracker["status"] != "ok" or not tracker["message"].endswith(
            "(источник: поле tracker)"
        ):
            sys.exit(f"health did not resolve the {name} tracker from the field")
