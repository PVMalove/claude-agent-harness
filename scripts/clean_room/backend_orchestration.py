"""Установка backend-orchestration, Repo Map и health: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts.clean_room.support import (
    HARNESS,
    ROOT,
    capture,
    fill_agents,
    run_ok,
)


def run(ctx: SimpleNamespace) -> None:
    """Установка backend-orchestration, Repo Map и health.

    Читает из контекста: `pv_project`, `test_root`.
    Передаёт дальше: `coordinator_path`, `orchestration_config`, `orchestration_project`,
    `orchestration_remote`, `orchestration_root`, `staged_payload`, `valid_orchestration`.
    """
    pv_project = ctx.pv_project
    test_root = ctx.test_root
    # backend-orchestration is opt-in: it extends pvmalove-suite with the portable
    # role contracts, while a regular pvmalove-suite project receives none of them.
    if (pv_project / ".harness" / "orchestration").exists():
        sys.exit("pvmalove-suite unexpectedly installed orchestration resources")
    if (pv_project / ".harness" / "orchestration.json").exists():
        sys.exit("pvmalove-suite unexpectedly installed orchestration config")
    regular_implement = (
        pv_project / ".harness" / "skills" / "implement" / "SKILL.md"
    ).read_text(encoding="utf-8")
    if "run `/fast-implement` instead" not in regular_implement:
        sys.exit(
            "non-opted-in project is not routed to the single-session fast-implement path"
        )
    fast_implement = pv_project / ".harness" / "skills" / "fast-implement" / "SKILL.md"
    if not fast_implement.is_file():
        sys.exit("pvmalove-suite addition fast-implement missing")
    if "no coordinator approval gates" not in fast_implement.read_text(
        encoding="utf-8"
    ):
        sys.exit("fast-implement does not carry the ungated single-session flow")

    orchestration_project = test_root / "o"

    def staged_payload(name: str) -> Path:
        """Role-authored payloads live inside the project, at the brief's staging path.

        The coordinator refuses a report or checkpoint written outside the repository and its
        worktrees, which is what stops evidence from landing in a guessed home-directory folder.
        """
        path = orchestration_project / ".harness" / "scratch" / "inbox" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    orchestration_project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=orchestration_project, check=True)
    orchestration_remote = test_root / "orchestration-remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(orchestration_remote)], check=True
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(orchestration_remote)],
        cwd=orchestration_project,
        check=True,
    )
    run_ok(
        HARNESS
        + [
            "init",
            str(orchestration_project),
            "--capability",
            "backend-orchestration",
            "--base-branch",
            "main",
            "--language",
            "ru",
            "--pr-base-branch",
            "main",
            "--branch-pattern",
            "^feature/issue-[0-9]+-.+",
            "--qa-gate-command",
            "echo test",
        ]
    )
    fill_agents(orchestration_project)
    run_ok(HARNESS + ["health", str(orchestration_project)])
    orchestration_root = orchestration_project / ".harness" / "orchestration"
    if not (orchestration_root / "orchestration.schema.json").is_file():
        sys.exit("backend-orchestration schema missing")
    installed_implement = (
        orchestration_project / ".harness" / "skills" / "implement" / "SKILL.md"
    ).read_text(encoding="utf-8")
    if "This session **is** the coordinator" not in installed_implement:
        sys.exit(
            "opted-in project implement skill does not drive the coordinator pipeline"
        )
    for required_contract in (
        "architect → developer → code-review → qa → publish",
        "explicit approval",
        "model self-report",
        "watchdog",
        "module-owned guidance",
        ".harness/orchestration/playbook.md",
        ".harness/orchestration/roles/",
    ):
        if required_contract.casefold() not in installed_implement.casefold():
            sys.exit(
                f"implement skill is missing coordinator contract: {required_contract}"
            )
    if "## Phase 1:" in installed_implement or "## Phase 5:" in installed_implement:
        sys.exit("implement skill duplicates module-owned orchestration procedure")
    if "coordinator.py --repo . dispatch status" not in installed_implement:
        sys.exit("implement skill does not state the self-contained route check")
    installed_architect = (
        orchestration_project / ".harness" / "orchestration" / "roles" / "architect.md"
    ).read_text(encoding="utf-8")
    normalized_architect = " ".join(installed_architect.split())
    for required_architect_rule in (
        "single concise architecture decision brief",
        "must not run the batch's full verification suite",
    ):
        if required_architect_rule not in normalized_architect:
            sys.exit(
                f"architect role is missing token-budget rule: {required_architect_rule}"
            )
    installed_pr_step = (
        orchestration_project / ".harness" / "skills" / "to-pull-requests" / "SKILL.md"
    )
    if not installed_pr_step.is_file():
        sys.exit("installed project is missing the to-pull-requests PR step")
    if (orchestration_project / ".harness" / "skills" / "to-pr").exists():
        sys.exit("installed project retains the removed to-pr PR step")
    if "qa evidence" not in installed_pr_step.read_text(encoding="utf-8"):
        sys.exit(
            "installed to-pull-requests step does not validate accepted QA evidence"
        )
    orchestration_config = orchestration_project / ".harness" / "orchestration.json"
    if not orchestration_config.is_file():
        sys.exit("backend-orchestration config seed missing")
    # Project-owned orchestration settings must survive a forced refresh of the
    # managed snapshot. `--force` intentionally replaces both groups, but a
    # snapshot-only repair must not turn a configured project back into the empty seed.
    original_orchestration_config = orchestration_config.read_text(encoding="utf-8")
    project_owned_config = original_orchestration_config.replace(
        '"concurrency_budget": 1', '"concurrency_budget": 3'
    )
    orchestration_config.write_text(project_owned_config, encoding="utf-8")
    coordinator_path = (
        orchestration_project / ".harness" / "orchestration" / "coordinator.py"
    )
    with coordinator_path.open("a", encoding="utf-8") as handle:
        handle.write("\n# local drift for force-managed-files regression\n")
    run_ok(
        HARNESS + ["update", str(orchestration_project), "--force-managed-files"],
        quiet=True,
    )
    if orchestration_config.read_text(encoding="utf-8") != project_owned_config:
        sys.exit("force-managed-files overwrote project-owned orchestration config")
    if "local drift for force-managed-files regression" in coordinator_path.read_text(
        encoding="utf-8"
    ):
        sys.exit("force-managed-files did not restore the managed coordinator snapshot")
    orchestration_config.write_text(original_orchestration_config, encoding="utf-8")
    public_documentation = {
        ROOT / "README.md": (
            "`backend-orchestration`",
            "человек явно утверждает каждый dispatch",
            "/to-pull-requests",
        ),
        ROOT / "CONTEXT.md": (
            "`reported`",
            "FIFO",
            "санитизирован",
        ),
        ROOT / "docs" / "agents" / "current-state.md": (
            "`planned → awaiting-approval ↔ active → completed | blocked | failed`",
            "immutable brief",
            "Standards и Spec",
            "FIFO quality-gate lane",
            ".harness/orchestration/state/",
        ),
        ROOT / "docs" / "agents" / "backend-orchestration.md": (
            "`planned → awaiting-approval ↔ active → completed | blocked | failed`",
            "`reported`",
            "детерминирован",
            "Standards и Spec",
            "обязательный полный QA gate",
            "transport-only",
            "санитизирован",
            "stale",
            "/to-pull-requests",
        ),
        ROOT / "docs" / "agents" / "harness-guide.md": (
            "строго opt-in маршрут",
            "candidate commit",
            "`reported`",
            "/to-pull-requests",
        ),
        ROOT / "docs" / "agents" / "git-workflow.md": ("/to-pull-requests",),
    }
    for path, required_phrases in public_documentation.items():
        text = path.read_text(encoding="utf-8")
        if re.search(r"(?<![\w-])/to-pr(?![\w-])", text):
            sys.exit(f"public documentation retains removed /to-pr route: {path}")
        for phrase in required_phrases:
            if phrase.casefold() not in text.casefold():
                sys.exit(
                    f"public documentation missing hybrid coordinator contract {phrase!r}: {path}"
                )
    for source_path, required_phrases in public_documentation.items():
        if source_path == ROOT / "README.md" or source_path == ROOT / "CONTEXT.md":
            continue
        installed_path = orchestration_project / "docs" / "agents" / source_path.name
        if not installed_path.is_file():
            sys.exit(
                f"installed project is missing documentation seed: {source_path.name}"
            )
        installed_text = installed_path.read_text(encoding="utf-8")
        for phrase in required_phrases:
            if phrase.casefold() not in installed_text.casefold():
                sys.exit(
                    f"installed documentation missing hybrid coordinator contract {phrase!r}: {source_path.name}"
                )
        if re.search(r"(?<![\w-])/to-pr(?![\w-])", installed_text):
            sys.exit(
                f"installed documentation retains removed /to-pr route: {source_path.name}"
            )
    source_orchestration_guide = (
        ROOT / "harness" / "project" / "docs-agents" / "backend-orchestration.md"
    ).read_text(encoding="utf-8")
    installed_orchestration_guide = (
        orchestration_project / "docs" / "agents" / "backend-orchestration.md"
    ).read_text(encoding="utf-8")
    if installed_orchestration_guide != source_orchestration_guide:
        sys.exit(
            "installed backend-orchestration guidance differs from its source template"
        )
    expected_role_files = {
        "architect.md",
        "code-review.md",
        "database-migrations.md",
        "developer.md",
        "messaging-integration.md",
        "qa.md",
        "verification.md",
    }
    actual_role_files = {
        path.name
        for path in (
            orchestration_project / ".harness" / "orchestration" / "roles"
        ).glob("*.md")
        if path.name != "_common.md"
    }
    if actual_role_files != expected_role_files:
        sys.exit(
            "backend-orchestration role set changed: "
            f"expected {sorted(expected_role_files)}, found {sorted(actual_role_files)}"
        )
    for name in ("_common.md", *sorted(expected_role_files)):
        if not (
            orchestration_project / ".harness" / "orchestration" / "roles" / name
        ).is_file():
            sys.exit(f"backend-orchestration role manifest missing: {name}")

    playbook_path = orchestration_root / "playbook.md"
    if not playbook_path.is_file():
        sys.exit("backend-orchestration playbook missing")
    playbook = playbook_path.read_text(encoding="utf-8")
    pilot_path = orchestration_root / "pilot.md"
    if not pilot_path.is_file():
        sys.exit("backend-orchestration pilot guide missing")
    pilot = pilot_path.read_text(encoding="utf-8")
    normalized_pilot = " ".join(pilot.split())
    for token_evidence_rule in (
        "provider- or runtime-observed input and output tokens",
        "a role's self-report is never token telemetry",
        "missing-data note",
    ):
        if token_evidence_rule not in normalized_pilot:
            sys.exit(
                f"installed pilot guide is missing token-evidence rule: {token_evidence_rule}"
            )
    for required_pilot_contract in (
        "## Purpose",
        "## Before the first run",
        "## Observation period",
        "## Record each batch",
        "## Measure the quality gate",
        "## Record post-integration defects",
        "## Baseline worksheet",
        "measurement period",
        "batch/ticket IDs",
        "missing-data notes",
        "same counting rules",
        "does not prescribe a named provider, model, runtime, or platform, or a hard numerical target",
    ):
        if required_pilot_contract not in normalized_pilot:
            sys.exit(
                f"backend-orchestration pilot guide missing contract: {required_pilot_contract}"
            )
    if "pilot.md" not in playbook:
        sys.exit("backend-orchestration playbook does not link the pilot guide")
    if re.search(r"(?im)^\s*(?:provider|model|runtime|platform)\s*[:=]", pilot):
        sys.exit(
            "backend-orchestration pilot guide declares a provider, model, runtime, or platform"
        )
    if re.search(
        r"(?i)\b(?:use|choose|select|require|default|must|should)\b.{0,40}"
        r"\b(?:provider|model|runtime|platform)\b",
        normalized_pilot,
    ):
        sys.exit(
            "backend-orchestration pilot guide prescribes a provider, model, runtime, or platform"
        )
    if re.search(
        r"(?i)\b(?:at most|at least|no more than|maximum|minimum|limit|target)\s+"
        r"(?:\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)\b",
        normalized_pilot,
    ):
        sys.exit("backend-orchestration pilot guide contains a hard numerical target")
    for required_contract in (
        "## Lifecycle",
        "planned",
        "awaiting-approval",
        "active",
        "reported",
        "completed",
        "blocked",
        "failed",
        "## Immutable handoff brief",
        "ticket",
        "zone IDs",
        "branch/worktree",
        "Definition of Done",
        "prohibited changes",
        "verification commands",
        "## Completion report",
        "commit SHA",
        "changed files",
        "checks run",
        "risks",
        "blockers",
        "new coordinator decision",
        "new dispatch",
        "one active writer",
        "serialized quality-gate lane",
        "agent starts per closed ticket",
        "tokens per batch",
        "quality-gate wall time",
        "post-integration defects",
    ):
        if required_contract not in playbook:
            sys.exit(
                f"backend-orchestration playbook missing contract: {required_contract}"
            )
    lifecycle_states = [
        f"`{state}`"
        for state in (
            "planned",
            "awaiting-approval",
            "active",
            "completed",
            "blocked",
            "failed",
        )
    ]
    lifecycle_positions = [playbook.index(f"| {state} |") for state in lifecycle_states]
    if lifecycle_positions != sorted(lifecycle_positions):
        sys.exit("backend-orchestration playbook lifecycle states are out of order")
    normalized_playbook = " ".join(playbook.split())
    for required_rule in (
        "A retry is a new dispatch with a new brief and a new dispatch ID",
        "The original brief remains immutable",
        "one active writer at a time",
        "multiple roles must never write to the same batch simultaneously",
        "one serialized quality-gate lane",
        "without importing a provider, runtime, or fixed numerical target",
    ):
        if required_rule not in normalized_playbook:
            sys.exit(f"backend-orchestration playbook missing rule: {required_rule}")

    code_review_role = (
        orchestration_project
        / ".harness"
        / "orchestration"
        / "roles"
        / "code-review.md"
    ).read_text(encoding="utf-8")
    role_frontmatter = code_review_role.split("---", 2)[1]
    if (
        "name: code-review" not in role_frontmatter
        or "mode: read-only" not in role_frontmatter
    ):
        sys.exit(
            "code-review role metadata must remain read-only and named code-review"
        )

    def frontmatter_list(field):
        lines = role_frontmatter.splitlines()
        try:
            start = lines.index(f"{field}:") + 1
        except ValueError:
            return set()
        values = set()
        for line in lines[start:]:
            item = line.strip()
            if not item.startswith("-"):
                break
            values.add(item[2:].strip())
        return values

    actual_capabilities = frontmatter_list("required_capabilities")
    if actual_capabilities != {"code-review"}:
        sys.exit(
            f"code-review role capabilities changed: {sorted(actual_capabilities)}"
        )
    actual_triggers = frontmatter_list("risk_triggers")
    expected_triggers = {
        "api-public-contract",
        "schema-change",
        "data-migration",
        "outbox",
        "queues",
        "message-schema-routing",
        "transactions",
        "authorization-security",
        "concurrency-retry",
        "retry-dlq",
    }
    if actual_triggers != expected_triggers:
        sys.exit(f"code-review role risk triggers changed: {sorted(actual_triggers)}")
    for required_contract in (
        "mandatory two-axis review gate",
        "API/public contracts",
        "schema/data migrations",
        "outbox/queues",
        "transactions",
        "authorization/security",
        "concurrency/retry",
        "evidence",
        "residual risks",
        "blockers",
        "must not change production code or integrate",
        "reviewed branch",
        "must not mark a high-risk batch complete until both the Standards and Spec reports",
        "test_summary.py",
        "original approved command",
    ):
        if required_contract not in code_review_role:
            sys.exit(f"code-review role contract missing: {required_contract}")

    installed_review_skill = (
        orchestration_project / ".harness" / "skills" / "code-review" / "SKILL.md"
    ).read_text(encoding="utf-8")
    for required_boundary in (
        "`language` from `.harness/project.json`",
        "runtime-neutral",
        "must not launch a runtime-specific adapter",
    ):
        if required_boundary not in installed_review_skill:
            sys.exit(f"code-review workflow boundary missing: {required_boundary}")

    role_names = (
        "developer",
        "architect",
        "qa",
        "database-migrations",
        "messaging-integration",
        "code-review",
    )
    valid_orchestration = {
        "$schema": "./orchestration/orchestration.schema.json",
        "provider_profiles": {
            "backend-default": {
                "capabilities": [
                    "backend-development",
                    "architecture-analysis",
                    "independent-verification",
                    "database-migrations",
                    "messaging-integration",
                    "code-review",
                ],
                "agent": "codex",
                "fallback": [],
                "known_limitations": ["project-defined limitations"],
            }
        },
        "assignment_plans": {
            role: {
                "zone": "backend",
                "transport": "orca",
                "runtimes": {
                    "codex": {
                        "profiles": ["backend-default"],
                        "model": f"project-{role}-model",
                        "effort": "high",
                    }
                },
            }
            for role in role_names
        },
        "backend_zones": {"backend": {"paths": ["services/**"]}},
        "concurrency_budget": 2,
        "developer_verification_commands": ["python developer_check.py"],
        "verification_commands": [f"{sys.executable} qa_baseline.py"],
    }
    orchestration_config.write_text(
        json.dumps(valid_orchestration, indent=2) + "\n", encoding="utf-8"
    )
    run_ok(HARNESS + ["health", str(orchestration_project)])
    minimal_repo_map_policy = json.loads(json.dumps(valid_orchestration))
    minimal_repo_map_policy["repo_map_policy"] = {"tier": "minimal"}
    orchestration_config.write_text(
        json.dumps(minimal_repo_map_policy, indent=2) + "\n", encoding="utf-8"
    )
    policy_health = capture(HARNESS + ["health", str(orchestration_project)])
    if "Repo Map: tier=minimal (requested by policy)" not in policy_health:
        sys.exit("health did not report the policy-required Repo Map tier")
    if "dispatch is limited to minimal path inventory" not in policy_health:
        sys.exit("health did not report Repo Map policy dispatch effect")
    orchestration_config.write_text(
        json.dumps(valid_orchestration, indent=2) + "\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=orchestration_project,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Clean Room"],
        cwd=orchestration_project,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "chore: initialize adapter fixture"],
        cwd=orchestration_project,
        check=True,
    )
    subprocess.run(
        ["git", "branch", "feature/issue-900-approved-dispatch"],
        cwd=orchestration_project,
        check=True,
    )
    ctx.coordinator_path = coordinator_path
    ctx.orchestration_config = orchestration_config
    ctx.orchestration_project = orchestration_project
    ctx.orchestration_remote = orchestration_remote
    ctx.orchestration_root = orchestration_root
    ctx.staged_payload = staged_payload
    ctx.valid_orchestration = valid_orchestration
