"""Group 'files': the harness files of a project - lock, AGENTS.md, discovery links, project.json,
storage, orchestration config, the skill snapshot and registry, overlay locks, integrations and
verification routing. The detection logic itself lives in harness/health/project_files.py (shared
with the packager); this module only wraps it into CheckResults with a stable id, a status, a
Russian message and a remedy.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from harness.storage import sandboxes_health, validate_sandboxes

from ..context import HealthContext
from ..model import CheckResult, Fix
from ..project_files import (
    BACKEND_ORCHESTRATION_CAPABILITY,
    DISCOVERY_LINKS,
    ORCHESTRATION_CONFIG_REL,
    REGISTRY_REL,
    native_link_target,
    project_registry,
    validate_integrations,
    validate_orchestration_config,
    validate_overlay_locks,
    validate_project_json,
    verification_routing_health,
)

_TEMPLATE_MARKER = re.compile(r"{{[^{}\n]+}}")

_REPO_MAP_POLICY_FIX = Fix(
    text=(
        "исправьте значение repo_map_policy в .harness/orchestration.json "
        "по схеме .harness/orchestration/orchestration.schema.json и повторите harness health"
    )
)



def _registry_fix(context: HealthContext) -> Fix:
    return Fix(
        text="пересоберите реестр скиллов (или запустите harness health --fix)",
        command=context.harness_command("registry", str(context.repo)),
    )


# --- Registry-facing checks ------------------------------------------------------------------


def check_lock(context: HealthContext) -> CheckResult:
    if context.lock_error is not None:
        return CheckResult(
            id="files.lock",
            group="files",
            status="fail",
            message=f"повреждён .harness/harness.lock ({context.lock_error})",
            fix=Fix(
                text="восстановите lock из git; если его там нет — удалите .harness/harness.lock "
                "и заново выполните harness init с прежними --capability",
                command="git checkout -- .harness/harness.lock",
            ),
        )
    if context.lock is None:
        return CheckResult(
            id="files.lock",
            group="files",
            status="fail",
            message="отсутствует .harness/harness.lock",
            fix=Fix(
                text="установите харнесс в проект",
                command=context.harness_command("init", str(context.repo)),
            ),
        )
    return CheckResult(
        id="files.lock", group="files", status="ok", message="harness.lock присутствует"
    )


def check_agents_md(context: HealthContext) -> CheckResult:
    agents_path = context.repo / "AGENTS.md"
    if not agents_path.is_file():
        return CheckResult(
            id="files.agents_md",
            group="files",
            status="fail",
            message="отсутствует AGENTS.md",
            # Only `harness init` writes AGENTS.md, and init refuses once a lock exists.
            fix=Fix(
                text="восстановите AGENTS.md из git; если его там нет — создайте его из шаблона "
                "harness/project/AGENTS.md.tmpl и заполните плейсхолдеры",
                command="git checkout -- AGENTS.md",
            ),
        )
    agents_text = agents_path.read_text(encoding="utf-8")
    if _TEMPLATE_MARKER.search(agents_text):
        return CheckResult(
            id="files.agents_md",
            group="files",
            status="fail",
            message="в AGENTS.md остались нерешённые плейсхолдеры шаблона",
            fix=Fix(text="замените плейсхолдеры вида {{...}} в AGENTS.md значениями проекта"),
        )
    return CheckResult(
        id="files.agents_md",
        group="files",
        status="ok",
        message="AGENTS.md заполнен, нерешённых плейсхолдеров нет",
    )


def broken_discovery_links(repo: Path) -> list[str]:
    """One human-readable problem per discovery link that is missing, foreign, or unresolvable.

    A missing link under a read-only parent is not reported (the installer skips it too)."""
    broken: list[str] = []
    for relative, target in DISCOVERY_LINKS.items():
        path = repo / relative
        native_target = native_link_target(target)
        if not path.is_symlink() or os.readlink(path) != native_target:
            if (
                not path.exists()
                and path.parent.exists()
                and not os.access(path.parent, os.W_OK)
            ):
                continue
            broken.append(f"неисправна discovery-ссылка: {relative} -> {target}")
        elif not path.is_dir():
            broken.append(f"discovery-ссылка не резолвится: {relative} -> {target}")
    return broken


def check_discovery_links(context: HealthContext) -> CheckResult:
    broken = broken_discovery_links(context.repo)
    if broken:
        return CheckResult(
            id="files.discovery_links",
            group="files",
            status="fail",
            message="; ".join(broken),
            # `harness update` recreates a missing link but refuses to replace an existing
            # wrong one without --force, so the wrong link is removed by hand first.
            fix=Fix(
                text="удалите перечисленные неверные ссылки (не каталоги со скиллами проекта) "
                "и пересоздайте их; на Windows сначала проверьте environment.symlinks "
                "(Developer Mode)",
                command=context.harness_command("update", str(context.repo)),
            ),
        )
    return CheckResult(
        id="files.discovery_links",
        group="files",
        status="ok",
        message="discovery-ссылки скиллов исправны",
    )


def check_project_json(context: HealthContext) -> CheckResult:
    problems: list[str] = []
    validate_project_json(context.repo, problems)
    if problems:
        return CheckResult(
            id="files.project_json",
            group="files",
            status="fail",
            message="; ".join(problems),
            fix=Fix(
                text="исправьте .harness/project.json по схеме .harness/project.schema.json "
                "и повторите harness health"
            ),
        )
    if (context.repo / ".harness" / "project.json").is_file():
        return CheckResult(
            id="files.project_json",
            group="files",
            status="ok",
            message="project.json корректен",
        )
    return CheckResult(
        id="files.project_json",
        group="files",
        status="ok",
        message="project.json отсутствует (необязателен)",
    )


def check_sandboxes(context: HealthContext) -> CheckResult:
    problems: list[str] = []
    validate_sandboxes(context.repo, problems)
    if problems:
        fix = None
        if any("exists but is not a directory" in problem for problem in problems):
            fix = Fix(text="удалите файл и создайте директорию .harness/.sandboxes")
        elif any(
            "no read/write access" in problem or "permission denied" in problem
            for problem in problems
        ):
            fix = Fix(text="проверьте права доступа к хранилищу .harness")
        return CheckResult(
            id="files.sandboxes",
            group="files",
            status="fail",
            message="; ".join(problems),
            fix=fix,
        )
    lines = sandboxes_health(context.repo)
    if lines:
        warnings = [
            line.removeprefix("ПРЕДУПРЕЖДЕНИЕ: ")
            for line in lines
            if not line.startswith("КАК ИСПРАВИТЬ: ")
        ]
        remedies = [
            line.removeprefix("КАК ИСПРАВИТЬ: ")
            for line in lines
            if line.startswith("КАК ИСПРАВИТЬ: ")
        ]
        return CheckResult(
            id="files.sandboxes",
            group="files",
            status="warn",
            message="; ".join(warnings),
            fix=Fix(text="; ".join(remedies)) if remedies else None,
        )
    return CheckResult(
        id="files.sandboxes",
        group="files",
        status="ok",
        message="хранилище .harness/.sandboxes исправно",
    )


def check_orchestration_config(context: HealthContext) -> CheckResult:
    lock = context.lock
    if lock is None or BACKEND_ORCHESTRATION_CAPABILITY not in (
        lock.get("capabilities") or []
    ):
        return CheckResult(
            id="files.orchestration_config",
            group="files",
            status="skipped",
            message=context.no_orchestration_message(),
        )
    problems: list[str] = []
    validate_orchestration_config(context.repo, problems)
    if not problems:
        return CheckResult(
            id="files.orchestration_config",
            group="files",
            status="ok",
            message="конфигурация backend-orchestration корректна",
        )
    fix = (
        _REPO_MAP_POLICY_FIX
        if any(p.startswith("orchestration repo_map_policy") for p in problems)
        else None
    )
    return CheckResult(
        id="files.orchestration_config",
        group="files",
        status="fail",
        message="; ".join(problems),
        fix=fix,
    )


def check_skill_snapshot(context: HealthContext) -> CheckResult:
    if context.lock is None:
        return CheckResult(
            id="files.skill_snapshot",
            group="files",
            status="skipped",
            message=context.no_lock_message(),
        )
    if context.snapshot_diff is None:
        # snapshot_diff itself stays in harness/bin/harness.py (it re-derives the expected package
        # content from CAPABILITIES.json and the harness/ source tree, which never ships to an
        # installed project); only the canonical `harness health` CLI can supply it (see cmd_health).
        return CheckResult(
            id="files.skill_snapshot",
            group="files",
            status="skipped",
            message="снэпшот скиллов доступен только из харнесс-пакетировщика (harness/bin/harness.py)",
        )
    result = context.snapshot_diff(context.repo)
    if result["state"] != "clean":
        return CheckResult(
            id="files.skill_snapshot",
            group="files",
            status="fail",
            message="в снэпшоте скиллов есть расхождения",
            fix=Fix(
                text="посмотрите расхождения через harness diff, затем восстановите снэпшот",
                command=context.harness_command("update", str(context.repo)),
            ),
        )
    return CheckResult(
        id="files.skill_snapshot",
        group="files",
        status="ok",
        message="снэпшот скиллов без расхождений",
    )


def check_skill_registry(context: HealthContext) -> CheckResult:
    if context.lock is None:
        return CheckResult(
            id="files.skill_registry",
            group="files",
            status="skipped",
            message=context.no_lock_message(),
        )
    registry_rel = REGISTRY_REL
    try:
        expected_registry = project_registry(context.repo)
    except ValueError as exc:
        return CheckResult(
            id="files.skill_registry", group="files", status="fail", message=str(exc)
        )
    registry_path = context.repo / registry_rel
    if not registry_path.is_file():
        return CheckResult(
            id="files.skill_registry",
            group="files",
            status="fail",
            message=f"отсутствует {registry_rel}",
            fix=_registry_fix(context),
        )
    if registry_path.read_text(encoding="utf-8") != expected_registry:
        return CheckResult(
            id="files.skill_registry",
            group="files",
            status="fail",
            message=f"устарел {registry_rel}",
            fix=_registry_fix(context),
        )
    return CheckResult(
        id="files.skill_registry",
        group="files",
        status="ok",
        message="реестр скиллов актуален",
    )


def fix_skill_registry(context: HealthContext, result: CheckResult) -> str | None:
    """`harness health --fix`: regenerate a missing or stale REGISTRY.md, like `harness registry`.

    Acts only on a `fail` whose expected content could be derived (an invalid skill tree stays a
    `fail` for the developer to resolve); returns what was done, or None when nothing was.
    """
    if result.status != "fail":
        return None
    try:
        expected_registry = project_registry(context.repo)
    except ValueError:
        return None
    registry_path = context.repo / REGISTRY_REL
    if registry_path.is_file() and registry_path.read_text(encoding="utf-8") == expected_registry:
        return None
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(expected_registry, encoding="utf-8", newline="\n")
    return f"пересобран {REGISTRY_REL.as_posix()}"


def check_overlay_locks(context: HealthContext) -> CheckResult:
    if context.lock is None:
        return CheckResult(
            id="files.overlay_locks",
            group="files",
            status="skipped",
            message=context.no_lock_message(),
        )
    problems: list[str] = []
    try:
        validate_overlay_locks(context.repo, context.lock, problems)
    except ValueError as exc:
        return CheckResult(
            id="files.overlay_locks", group="files", status="fail", message=str(exc)
        )
    if problems:
        return CheckResult(
            id="files.overlay_locks",
            group="files",
            status="fail",
            message="; ".join(problems),
            fix=Fix(
                text="исправьте overlay-локи в .harness/overlays (schema 1, overlay_id, source с "
                "remote и revision без учётных данных); локи скиллов проекта пересоздаёт "
                "harness lock-project-skills",
                command=context.harness_command("lock-project-skills", str(context.repo)),
            ),
        )
    return CheckResult(
        id="files.overlay_locks",
        group="files",
        status="ok",
        message="overlay-локи согласованы",
    )


def check_integrations(context: HealthContext) -> CheckResult:
    if context.lock is None:
        return CheckResult(
            id="files.integrations",
            group="files",
            status="skipped",
            message=context.no_lock_message(),
        )
    problems: list[str] = []
    count = validate_integrations(context.repo, problems)
    if problems:
        return CheckResult(
            id="files.integrations",
            group="files",
            status="fail",
            message="; ".join(problems),
            fix=Fix(
                text="дополните .harness/integrations.json: у каждой интеграции id, kind, "
                "runtimes, verify и secret_refs только с именами переменных окружения"
            ),
        )
    return CheckResult(
        id="files.integrations",
        group="files",
        status="ok",
        message=f"интеграции инвентаризованы ({count})",
    )


def check_verification_routing(context: HealthContext) -> CheckResult:
    lock = context.lock
    if lock is None or BACKEND_ORCHESTRATION_CAPABILITY not in (
        lock.get("capabilities") or []
    ):
        return CheckResult(
            id="files.verification_routing",
            group="files",
            status="skipped",
            message=context.no_orchestration_message(),
        )
    config_path = context.repo / ORCHESTRATION_CONFIG_REL
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CheckResult(
            id="files.verification_routing",
            group="files",
            status="skipped",
            message="нет .harness/orchestration.json",
        )
    lines = verification_routing_health(context.repo)
    if lines:
        remedy = (
            lines[1].removeprefix("КАК ИСПРАВИТЬ: ") if len(lines) > 1 else lines[0]
        )
        return CheckResult(
            id="files.verification_routing",
            group="files",
            status="warn",
            message=lines[0].removeprefix("ПРЕДУПРЕЖДЕНИЕ: "),
            fix=Fix(text=remedy),
        )
    if not isinstance(config, dict) or not config.get("verification_commands"):
        return CheckResult(
            id="files.verification_routing",
            group="files",
            status="ok",
            message="verification_commands не заданы для этого проекта",
        )
    return CheckResult(
        id="files.verification_routing",
        group="files",
        status="ok",
        message="developer_verification_commands заданы; developer получает фокусные проверки",
    )
