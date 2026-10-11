"""Установка backend-orchestration, Repo Map и health: сценарий clean-room из `scripts/test_clean_room.py`."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from harness.orchestration.core.constants import RECOVERY_ROUTES
from harness.storage import storage_path
from scripts.clean_room.support import (
    assert_contract_link,
    check_technical_english,
    HARNESS,
    ROOT,
    capture_json,
    fill_agents,
    find_check,
    run_health,
    run_ok,
    run_step,
)


RETRY_ROUTING_HEADING = "## Retry routing and abandon"
RECOVERY_ROUTE_TABLE_HEADING = "## Recovery route table"
RECOVERY_ROUTE_TABLE_HEADER = ("Situation", "Route", "Who approves", "Evidence")


def _require_worker_protocol(skill: str) -> None:
    """Проверить lifecycle внутри поставленного промпта, а не в соседнем тексте skill."""
    section = skill.partition("## Worker prompt\n")[2]
    match = re.search(r"```text\n(.*?)```", section, re.DOTALL)
    if match is None:
        sys.exit("installed implement skill is missing the worker prompt template")
    prompt = match.group(1)
    positions = []
    for command in ("dispatch self-report", "dispatch heartbeat", "report submit"):
        line = re.search(
            rf"^<coordinator CLI> {re.escape(command)}\b", prompt, re.MULTILINE
        )
        if line is None:
            sys.exit(
                f"installed worker prompt is missing the protocol command: {command}"
            )
        positions.append(line.start())
    if positions != sorted(positions):
        sys.exit(
            "installed worker prompt must attest, heartbeat, then submit its report"
        )


def _require_recovery_route_table(playbook: str) -> None:
    """Обязательное правило playbook: таблица маршрутов восстановления (#497).

    Раздел `## Recovery route table` идёт сразу после `## Retry routing and abandon`, содержит
    таблицу `Situation | Route | Who approves | Evidence` и хотя бы одну строку на каждое значение
    `RECOVERY_ROUTES` во второй колонке (у маршрута может быть несколько ситуаций); маршрут вне
    enum в таблице тоже ошибка.
    """
    missing = "backend-orchestration playbook missing rule: recovery route table"
    headings = [
        line.strip() for line in playbook.splitlines() if line.startswith("## ")
    ]
    if RECOVERY_ROUTE_TABLE_HEADING not in headings:
        sys.exit(f"{missing} ({RECOVERY_ROUTE_TABLE_HEADING})")
    position = headings.index(RECOVERY_ROUTE_TABLE_HEADING)
    if position == 0 or headings[position - 1] != RETRY_ROUTING_HEADING:
        sys.exit(
            f"{missing}: {RECOVERY_ROUTE_TABLE_HEADING} must directly follow "
            f"{RETRY_ROUTING_HEADING}"
        )
    section = playbook.split(RECOVERY_ROUTE_TABLE_HEADING, 1)[1].split("\n## ", 1)[0]
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section.splitlines()
        if line.strip().startswith("|")
    ]
    if not rows or tuple(rows[0]) != RECOVERY_ROUTE_TABLE_HEADER:
        sys.exit(
            f"{missing}: header must be | {' | '.join(RECOVERY_ROUTE_TABLE_HEADER)} |"
        )
    routes = [
        row[1].strip("`")
        for row in rows[2:]
        if len(row) == len(RECOVERY_ROUTE_TABLE_HEADER)
    ]
    unknown = sorted(set(routes) - set(RECOVERY_ROUTES))
    absent = [route for route in RECOVERY_ROUTES if route not in routes]
    if len(routes) != len(rows) - 2 or unknown or absent:
        sys.exit(
            f"{missing}: every row needs four cells and one route of RECOVERY_ROUTES "
            f"(missing: {absent}, unknown: {unknown})"
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
        """Сформировать путь к промежуточному файлу роли внутри scratch/inbox проекта."""
        path = storage_path(orchestration_project, "scratch", "inbox", name)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    orchestration_project.mkdir(parents=True)
    run_step(["git", "init", "-q"], cwd=orchestration_project, check=True)
    orchestration_remote = test_root / "orchestration-remote.git"
    run_step(["git", "init", "--bare", "-q", str(orchestration_remote)], check=True)
    run_step(
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
    check_technical_english(orchestration_project)
    run_health(orchestration_project)
    orchestration_root = orchestration_project / ".harness" / "orchestration"
    if not (orchestration_root / "orchestration.schema.json").is_file():
        sys.exit("backend-orchestration schema missing")
    installed_implement = (
        orchestration_project / ".harness" / "skills" / "implement" / "SKILL.md"
    ).read_text(encoding="utf-8")
    _require_worker_protocol(installed_implement)
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
        "Recovery route table",
        "route_preview",
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
    installed_pr_text = installed_pr_step.read_text(encoding="utf-8")
    if "qa evidence" not in installed_pr_text:
        sys.exit(
            "installed to-pull-requests step does not validate accepted QA evidence"
        )
    if installed_pr_text.find("record-qa-gate-pass.sh") < installed_pr_text.find(
        "qa evidence"
    ):
        sys.exit("installed to-pull-requests step does not record accepted QA evidence")
    # Issue #537: the installed PR step carries the whole continuation (next step, separate
    # confirmation bound to the SHA pair, CI wait, local-QA fallback, merge handoff) and still keeps
    # the plain /qa-gate branch for a project without the optional orchestration.
    for required_pr_phrase in (
        "integration next",
        "integration collect-ci",
        "integration local-qa",
        "separate confirmation",
        "qa_source",
        "Never merge it",
        "/qa-gate",
    ):
        if required_pr_phrase not in installed_pr_text:
            sys.exit(
                f"installed to-pull-requests step lacks the PR-continuation rule: {required_pr_phrase}"
            )
    orchestration_config = orchestration_project / ".harness" / "orchestration.json"
    if not orchestration_config.is_file():
        sys.exit("backend-orchestration config seed missing")
    # Project-owned orchestration settings must survive a forced refresh of the
    # managed snapshot. `--force` intentionally replaces both groups, but a
    # snapshot-only repair must not turn a configured project back into the empty seed.
    original_orchestration_config = orchestration_config.read_text(encoding="utf-8")
    project_owned_config = original_orchestration_config.replace(
        '"concurrency_budget": 5', '"concurrency_budget": 3'
    )
    if project_owned_config == original_orchestration_config:
        # Otherwise the overwrite check below compares the seed with itself and cannot fail.
        sys.exit("orchestration config seed no longer has concurrency_budget 5 to edit")
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
            "PR требует отдельного подтверждения",
            "/to-pull-requests",
        ),
        ROOT / "CONTEXT.md": (
            "`reported`",
            "FIFO",
            "санитизирован",
        ),
        ROOT / "docs" / "backend-orchestration.md": (
            "`planned → awaiting-approval ↔ active → completed`",
            "`reported`",
            "детерминирован",
            "Standards и Spec",
            "обязательный полный QA gate",
            "transport-only",
            "санитизирован",
            "stale",
            "/to-pull-requests",
            "integration next",
            "verification-failure",
        ),
        ROOT / "docs" / "harness-guide.md": (
            "строго opt-in маршрут",
            "candidate commit",
            "`reported`",
            "/to-pull-requests",
            "integration next",
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
        if source_path.parent != ROOT / "docs" / "agents":
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
    # Developer guides stay in the source repository's docs/; only agent contracts are delivered.
    for human_guide in ("harness-guide.md", "backend-orchestration.md"):
        if (orchestration_project / ".harness" / "docs" / human_guide).exists():
            sys.exit(f"developer guide leaked into the target project: {human_guide}")
    expected_role_files = {
        "architect.md",
        "code-review.md",
        "conflict-resolver.md",
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
    installed_resolver = (
        orchestration_project
        / ".harness"
        / "orchestration"
        / "roles"
        / "conflict-resolver.md"
    ).read_text(encoding="utf-8")
    if "resolving-merge-conflicts" not in installed_resolver:
        sys.exit(
            "conflict-resolver role lacks its resolving-merge-conflicts skill pointer"
        )
    if not (
        orchestration_project
        / ".harness"
        / "skills"
        / "resolving-merge-conflicts"
        / "SKILL.md"
    ).is_file():
        sys.exit("the skill the conflict-resolver role points to is not installed")
    for name in ("_common.md", *sorted(expected_role_files)):
        if not (
            orchestration_project / ".harness" / "orchestration" / "roles" / name
        ).is_file():
            sys.exit(f"backend-orchestration role manifest missing: {name}")

    playbook_path = orchestration_root / "playbook.md"
    if not playbook_path.is_file():
        sys.exit("backend-orchestration playbook missing")
    playbook = playbook_path.read_text(encoding="utf-8")
    contract = orchestration_project / ".harness/docs/technical-english.md"
    for continuation_rule in ("integration next", "verification-failure"):
        if continuation_rule not in playbook:
            sys.exit(
                f"installed playbook lacks the PR-continuation rule: {continuation_rule}"
            )
    for entry in (playbook_path, orchestration_root / "roles/_common.md"):
        assert_contract_link(entry, contract, entry.name)
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
        "allowed paths",
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
            "paused",
            "blocked",
            "failed",
            "completed",
            "abandoned",
            "not-required",
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
    _require_recovery_route_table(playbook)

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
        """Извлечь список строковых значений поля из YAML frontmatter роли."""
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
        "conflict-resolver",
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
                    "conflict-resolution",
                    "code-review",
                ],
                "fallback": [],
                "known_limitations": ["project-defined limitations"],
            }
        },
        "assignment_plans": {
            role: {
                "zone": "backend",
                "transport": "external",
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
    run_health(orchestration_project)
    minimal_repo_map_policy = json.loads(json.dumps(valid_orchestration))
    minimal_repo_map_policy["repo_map_policy"] = {"tier": "minimal"}
    orchestration_config.write_text(
        json.dumps(minimal_repo_map_policy, indent=2) + "\n", encoding="utf-8"
    )
    policy_health = find_check(
        capture_json(HARNESS + ["health", str(orchestration_project), "--json"]),
        "repo_map.tier",
    )
    if not policy_health["message"].startswith(
        "Repo Map: tier=minimal (requested by policy)"
    ):
        sys.exit("health did not report the policy-required Repo Map tier")
    if "dispatch is limited to minimal path inventory" not in policy_health["message"]:
        sys.exit("health did not report Repo Map policy dispatch effect")
    orchestration_config.write_text(
        json.dumps(valid_orchestration, indent=2) + "\n", encoding="utf-8"
    )
    run_step(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=orchestration_project,
        check=True,
    )
    run_step(
        ["git", "config", "user.name", "Clean Room"],
        cwd=orchestration_project,
        check=True,
    )
    run_step(
        ["git", "commit", "--allow-empty", "-qm", "chore: initialize adapter fixture"],
        cwd=orchestration_project,
        check=True,
    )
    run_step(
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
