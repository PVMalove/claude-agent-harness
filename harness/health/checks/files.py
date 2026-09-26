"""Group 'files': harness-file validations migrated from `harness/bin/harness`'s old `cmd_health`.

Every check below calls the same detection functions `cmd_health` used to call directly (via the
`_cli` bridge, see its docstring) - their logic is unchanged. What is new here is only the
CheckResult wrapping: a stable id, a group, a status, and - for the branches that used to print
nothing on success - a Russian ok/skipped message.
"""

from __future__ import annotations

import json
import os
import re

from harness.health.checks._cli import cli
from harness.health.context import HealthContext
from harness.health.model import CheckResult, Fix

_TEMPLATE_MARKER = re.compile(r"{{[^{}\n]+}}")

_REPO_MAP_POLICY_FIX = Fix(
    text=(
        "исправьте значение repo_map_policy в .harness/orchestration.json "
        "по схеме .harness/orchestration/orchestration.schema.json и повторите harness health"
    )
)

_NO_LOCK_MESSAGE = "нет .harness/harness.lock"
_NO_ORCHESTRATION_CAPABILITY_MESSAGE = "backend-orchestration capability не выбрана"


def check_lock(context: HealthContext) -> CheckResult:
    if context.lock is None:
        return CheckResult(id="files.lock", group="files", status="fail", message="missing .harness/harness.lock")
    return CheckResult(id="files.lock", group="files", status="ok", message="harness.lock присутствует")


def check_agents_md(context: HealthContext) -> CheckResult:
    agents_path = context.repo / "AGENTS.md"
    if not agents_path.is_file():
        return CheckResult(id="files.agents_md", group="files", status="fail", message="missing AGENTS.md")
    agents_text = agents_path.read_text(encoding="utf-8")
    if _TEMPLATE_MARKER.search(agents_text):
        return CheckResult(
            id="files.agents_md", group="files", status="fail",
            message="AGENTS.md contains unresolved template markers",
        )
    return CheckResult(
        id="files.agents_md", group="files", status="ok",
        message="AGENTS.md заполнен, нерешённых плейсхолдеров нет",
    )


def check_discovery_links(context: HealthContext) -> CheckResult:
    broken: list[str] = []
    for relative, target in cli().DISCOVERY_LINKS.items():
        path = context.repo / relative
        native_target = cli().native_link_target(target)
        if not path.is_symlink() or os.readlink(path) != native_target:
            if not path.exists() and path.parent.exists() and not os.access(path.parent, os.W_OK):
                continue
            broken.append(f"broken discovery link: {relative} -> {target}")
        elif not path.is_dir():
            broken.append(f"discovery link does not resolve: {relative} -> {target}")
    if broken:
        return CheckResult(id="files.discovery_links", group="files", status="fail", message="; ".join(broken))
    return CheckResult(
        id="files.discovery_links", group="files", status="ok", message="discovery-ссылки скиллов исправны"
    )


def check_project_json(context: HealthContext) -> CheckResult:
    problems: list[str] = []
    cli().validate_project_json(context.repo, problems)
    if problems:
        return CheckResult(id="files.project_json", group="files", status="fail", message="; ".join(problems))
    if (context.repo / ".harness" / "project.json").is_file():
        return CheckResult(id="files.project_json", group="files", status="ok", message="project.json корректен")
    return CheckResult(
        id="files.project_json", group="files", status="ok", message="project.json отсутствует (необязателен)"
    )


def check_orchestration_config(context: HealthContext) -> CheckResult:
    lock = context.lock
    if lock is None or cli().BACKEND_ORCHESTRATION_CAPABILITY not in (lock.get("capabilities") or []):
        return CheckResult(
            id="files.orchestration_config", group="files", status="skipped",
            message=_NO_ORCHESTRATION_CAPABILITY_MESSAGE,
        )
    problems: list[str] = []
    cli().validate_orchestration_config(context.repo, problems)
    if not problems:
        return CheckResult(
            id="files.orchestration_config", group="files", status="ok",
            message="конфигурация backend-orchestration корректна",
        )
    fix = _REPO_MAP_POLICY_FIX if any(p.startswith("orchestration repo_map_policy") for p in problems) else None
    return CheckResult(
        id="files.orchestration_config", group="files", status="fail", message="; ".join(problems), fix=fix
    )


def check_skill_snapshot(context: HealthContext) -> CheckResult:
    if context.lock is None:
        return CheckResult(id="files.skill_snapshot", group="files", status="skipped", message=_NO_LOCK_MESSAGE)
    result = cli().snapshot_diff(context.repo)
    if result["state"] != "clean":
        return CheckResult(
            id="files.skill_snapshot", group="files", status="fail", message="managed skill snapshot has drift"
        )
    return CheckResult(
        id="files.skill_snapshot", group="files", status="ok", message="снэпшот скиллов без расхождений"
    )


def check_skill_registry(context: HealthContext) -> CheckResult:
    if context.lock is None:
        return CheckResult(id="files.skill_registry", group="files", status="skipped", message=_NO_LOCK_MESSAGE)
    registry_rel = cli().REGISTRY_REL
    try:
        expected_registry = cli().project_registry(context.repo)
    except ValueError as exc:
        return CheckResult(id="files.skill_registry", group="files", status="fail", message=str(exc))
    registry_path = context.repo / registry_rel
    if not registry_path.is_file():
        return CheckResult(
            id="files.skill_registry", group="files", status="fail", message=f"missing {registry_rel}"
        )
    if registry_path.read_text(encoding="utf-8") != expected_registry:
        return CheckResult(
            id="files.skill_registry", group="files", status="fail",
            message=f"stale {registry_rel}; run harness registry",
        )
    return CheckResult(id="files.skill_registry", group="files", status="ok", message="реестр скиллов актуален")


def check_overlay_locks(context: HealthContext) -> CheckResult:
    if context.lock is None:
        return CheckResult(id="files.overlay_locks", group="files", status="skipped", message=_NO_LOCK_MESSAGE)
    problems: list[str] = []
    try:
        cli().validate_overlay_locks(context.repo, context.lock, problems)
    except ValueError as exc:
        return CheckResult(id="files.overlay_locks", group="files", status="fail", message=str(exc))
    if problems:
        return CheckResult(id="files.overlay_locks", group="files", status="fail", message="; ".join(problems))
    return CheckResult(id="files.overlay_locks", group="files", status="ok", message="overlay-локи согласованы")


def check_integrations(context: HealthContext) -> CheckResult:
    if context.lock is None:
        return CheckResult(id="files.integrations", group="files", status="skipped", message=_NO_LOCK_MESSAGE)
    problems: list[str] = []
    count = cli().validate_integrations(context.repo, problems)
    if problems:
        return CheckResult(id="files.integrations", group="files", status="fail", message="; ".join(problems))
    return CheckResult(
        id="files.integrations", group="files", status="ok", message=f"интеграции инвентаризованы ({count})"
    )


def check_verification_routing(context: HealthContext) -> CheckResult:
    lock = context.lock
    if lock is None or cli().BACKEND_ORCHESTRATION_CAPABILITY not in (lock.get("capabilities") or []):
        return CheckResult(
            id="files.verification_routing", group="files", status="skipped",
            message=_NO_ORCHESTRATION_CAPABILITY_MESSAGE,
        )
    config_path = context.repo / cli().ORCHESTRATION_CONFIG_REL
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CheckResult(
            id="files.verification_routing", group="files", status="skipped",
            message="нет .harness/orchestration.json",
        )
    lines = cli().verification_routing_health(context.repo)
    if lines:
        remedy = lines[1].removeprefix("КАК ИСПРАВИТЬ: ") if len(lines) > 1 else lines[0]
        return CheckResult(
            id="files.verification_routing", group="files", status="warn", message=lines[0],
            fix=Fix(text=remedy),
        )
    if not isinstance(config, dict) or not config.get("verification_commands"):
        return CheckResult(
            id="files.verification_routing", group="files", status="ok",
            message="verification_commands не заданы для этого проекта",
        )
    return CheckResult(
        id="files.verification_routing", group="files", status="ok",
        message="developer_verification_commands заданы; developer получает фокусные проверки",
    )
