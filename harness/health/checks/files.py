"""Group 'files': harness-file validations migrated from `harness/bin/harness`'s old `cmd_health`.

The detection functions below (native_link_target, digest, file_digest, relative_path,
frontmatter_metadata, skill_inventory, project_registry, validate_hash, public_skill_names,
project_skill_files, validate_overlay_locks, validate_integrations, validate_project_json,
validate_orchestration_config, verification_routing_health) are unchanged copies of the functions
`harness/bin/harness` used to define under the same names; `harness/bin/harness` now imports them
from here instead of defining its own copy, so there is a single definition and this package works
standalone once copied into an installed project's `.harness/health/` (no more loading
`harness/bin/harness` by path - see docs/adr/0018 for the same bootstrap-alias approach
`harness/repo_map/repo_map.py` already uses). What is new in the `check_*` functions themselves is
only the `CheckResult` wrapping: a stable id, a group, a status, and - for branches that used to
print nothing on success - a Russian ok/skipped message.

`fail`/`git_command` are small, generic CLI helpers `harness/bin/harness` also defines and uses in
~30 unrelated packager call sites; they are mirrored here privately (as `_fail`/`_git_command`,
byte-identical behavior) rather than moved, so the packager's own error-exit plumbing is untouched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from harness.storage import sandboxes_health, validate_sandboxes

from ..context import HealthContext
from ..model import CheckResult, Fix, JsonObject

_TEMPLATE_MARKER = re.compile(r"{{[^{}\n]+}}")

_REPO_MAP_POLICY_FIX = Fix(
    text=(
        "исправьте значение repo_map_policy в .harness/orchestration.json "
        "по схеме .harness/orchestration/orchestration.schema.json и повторите harness health"
    )
)

_NO_LOCK_MESSAGE = "нет .harness/harness.lock"
_NO_ORCHESTRATION_CAPABILITY_MESSAGE = "backend-orchestration capability не выбрана"

# --- Constants moved unchanged from harness/bin/harness ------------------------------------

REGISTRY_REL = Path(".harness/skills/REGISTRY.md")
INTEGRATIONS_REL = Path(".harness/integrations.json")
DISCOVERY_LINKS = {
    ".agents/skills": "../.harness/skills",
    ".claude/skills": "../.harness/skills",
}
ORCHESTRATION_CONFIG_REL = Path(".harness/orchestration.json")
ORCHESTRATION_SCHEMA_REL = Path(".harness/orchestration/orchestration.schema.json")
BACKEND_ORCHESTRATION_CAPABILITY = "backend-orchestration"

PROJECT_JSON_REQUIRED_FIELDS = (
    "language",
    "base_branch",
    "branch_pattern",
    "qa_gate_commands",
)
PROJECT_JSON_ALLOWED_FIELDS = frozenset(PROJECT_JSON_REQUIRED_FIELDS) | {
    "$schema",
    "story_points",
    "shell",
}
STORY_POINTS_REQUIRED_FIELDS = (
    "scale",
    "fast_threshold",
    "full_threshold",
    "gray_zone",
)
STORY_POINTS_ALLOWED_FIELDS = frozenset(STORY_POINTS_REQUIRED_FIELDS)


# --- Detection helpers moved unchanged from harness/bin/harness ----------------------------


def native_link_target(target: str) -> str:
    """DISCOVERY_LINKS targets use '/' for portability. Windows symlink reparse
    points resolve relative targets with '\\'; a '/'-separated target creates a
    reparse point that looks fine (dir listing shows SYMLINKD) but never
    resolves, silently hiding every skill under it."""
    return target.replace("/", os.sep)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str | None:
    try:
        return digest(path.read_bytes())
    except FileNotFoundError:
        return None


def relative_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must stay inside the project: {value}")
    return path


def frontmatter_metadata(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"missing YAML frontmatter: {path}")
    sections = text.split("---", 2)
    if len(sections) < 3:
        raise ValueError(f"unterminated YAML frontmatter: {path}")
    frontmatter = sections[1]

    fields: dict[str, str] = {}
    lines = frontmatter.splitlines()
    index = 0
    while index < len(lines):
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$", lines[index])
        if not match:
            index += 1
            continue
        key, value = match.groups()
        if value in {"|", ">"}:
            block: list[str] = []
            index += 1
            while index < len(lines) and (
                not lines[index] or lines[index][0].isspace()
            ):
                block.append(lines[index].strip())
                index += 1
            fields[key] = " ".join(part for part in block if part)
            continue
        fields[key] = value.strip(" '\"")
        index += 1

    name = fields.get("name", "")
    description = fields.get("description", "")
    if not name or not description:
        raise ValueError(f"missing frontmatter name or description: {path}")
    return name, description


def skill_inventory(repo: Path) -> list[tuple[str, Path, str]]:
    root = repo / ".harness/skills"
    if not root.is_dir():
        raise ValueError(f"missing skill root: {root}")
    rows: list[tuple[str, Path, str]] = []
    names: set[str] = set()
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        skill_file = directory / "SKILL.md"
        if not skill_file.is_file():
            raise ValueError(f"skill directory has no SKILL.md: {directory}")
        name, description = frontmatter_metadata(skill_file)
        if name != directory.name:
            raise ValueError(
                f"skill name {name!r} does not match directory {directory.name!r}"
            )
        if name in names:
            raise ValueError(f"duplicate skill name: {name}")
        names.add(name)
        rows.append((name, directory, description))
    return rows


def project_registry(repo: Path) -> str:
    lines = [
        "# Project Skill Registry",
        "",
        "Generated by `harness registry`. Search metadata here, then open only the exact matching",
        "`SKILL.md`; do not load the whole catalog into context.",
        "",
        "| Skill | Project path | When to use |",
        "|---|---|---|",
    ]
    for name, directory, description in skill_inventory(repo):
        relative = directory.relative_to(repo).as_posix()
        escaped_description = description.replace("|", "\\|")
        lines.append(f"| `{name}` | `{relative}` | {escaped_description} |")
    return "\n".join(lines) + "\n"


def validate_hash(
    repo: Path, relative: Path, expected: object, problems: list[str], label: str
) -> None:
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        problems.append(f"{label} has invalid sha256: {relative.as_posix()}")
        return
    actual = file_digest(repo / relative)
    if actual is None:
        problems.append(f"{label} file is missing: {relative.as_posix()}")
    elif actual != expected:
        problems.append(f"{label} hash mismatch: {relative.as_posix()}")


def public_skill_names(lock: JsonObject) -> set[str]:
    names: set[str] = set()
    for value in lock.get("files") or {}:
        parts = Path(value).parts
        if len(parts) >= 4 and parts[:2] == (".harness", "skills"):
            names.add(parts[2])
    return names


def _fail(message: str) -> None:
    """Mirrors harness/bin/harness's fail(): print to stderr and exit the process. Kept private
    and local (not imported) because that function also serves ~30 unrelated packager call sites
    that stay in harness/bin/harness."""
    print(f"harness: {message}", file=sys.stderr)
    raise SystemExit(1)


def _git_command(repo: Path, *arguments: str) -> list[str]:
    """Mirrors harness/bin/harness's git_command(): trust only the checkout explicitly supplied to
    this operation."""
    checkout = repo.resolve()
    return ["git", "-c", f"safe.directory={checkout}", "-C", str(checkout), *arguments]


def project_skill_files(repo: Path, directory: Path) -> list[Path]:
    """Enumerate through Git rather than a raw filesystem walk, so gitignored
    runtime artifacts (node_modules, build caches) never enter a project-local
    hash lock and never cause false health drift when they change."""
    try:
        relative = directory.relative_to(repo)
    except ValueError:
        _fail(f"project skill path escapes repository: {directory}")
    result = subprocess.run(
        _git_command(
            repo,
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            relative.as_posix(),
        ),
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        _fail(f"cannot inventory project skill files: {directory}")
    return sorted(
        repo / Path(value.decode("utf-8", errors="surrogateescape"))
        for value in result.stdout.split(b"\0")
        if value
        and (repo / Path(value.decode("utf-8", errors="surrogateescape"))).is_file()
    )


def validate_overlay_locks(repo: Path, lock: JsonObject, problems: list[str]) -> None:
    overlay_root = repo / ".harness/overlays"
    lock_paths = (
        []
        if not overlay_root.is_dir()
        else sorted(
            path
            for path in overlay_root.iterdir()
            if path.is_file() and path.name.endswith((".lock", ".lock.json"))
        )
    )
    locked_names: set[str] = set()
    for lock_path in lock_paths:
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(
                f"cannot read overlay lock {lock_path.relative_to(repo)}: {exc}"
            )
            continue
        if data.get("schema") != 1 or not isinstance(data.get("overlay_id"), str):
            problems.append(
                f"invalid overlay lock header: {lock_path.relative_to(repo)}"
            )
        source = data.get("source")
        if not isinstance(source, dict):
            problems.append(f"missing overlay source: {lock_path.relative_to(repo)}")
        elif source.get("type") != "project-local":
            remote = source.get("remote")
            revision = source.get("revision")
            if (
                not isinstance(remote, str)
                or not remote
                or not isinstance(revision, str)
                or not revision
            ):
                problems.append(
                    f"overlay source needs remote and revision: {lock_path.relative_to(repo)}"
                )
            elif "://" in remote and (
                urlsplit(remote).username or urlsplit(remote).password
            ):
                problems.append(
                    f"overlay remote contains credentials: {lock_path.relative_to(repo)}"
                )

        skills = data.get("skills")
        if not isinstance(skills, list):
            problems.append(
                f"overlay skills must be a list: {lock_path.relative_to(repo)}"
            )
            continue
        for entry in skills:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                problems.append(
                    f"invalid overlay skill entry: {lock_path.relative_to(repo)}"
                )
                continue
            name = entry["name"]
            if name in locked_names:
                problems.append(f"skill is claimed by multiple overlay locks: {name}")
            locked_names.add(name)
            files = entry.get("files")
            if not isinstance(files, dict) or not files:
                problems.append(f"overlay skill has no file hashes: {name}")
                continue
            expected_files: set[str] = set()
            for value, expected in files.items():
                try:
                    suffix = relative_path(value, label=f"overlay file in {name}")
                except ValueError:
                    problems.append(
                        f"overlay skill path escapes project: {name}/{value}"
                    )
                    continue
                relative = Path(".harness/skills") / name / suffix
                expected_files.add(suffix.as_posix())
                validate_hash(repo, relative, expected, problems, f"overlay {name}")
            directory = repo / ".harness/skills" / name
            actual_files = (
                {
                    path.relative_to(directory).as_posix()
                    for path in project_skill_files(repo, directory)
                }
                if directory.is_dir()
                else set()
            )
            if actual_files != expected_files:
                problems.append(f"overlay file inventory mismatch: {name}")

        for entry in data.get("notices", []):
            if not isinstance(entry, dict):
                problems.append(
                    f"invalid overlay notice: {lock_path.relative_to(repo)}"
                )
                continue
            try:
                target = relative_path(
                    entry.get("target"), label="overlay notice target"
                )
            except ValueError:
                problems.append(
                    f"overlay notice target escapes project: {lock_path.relative_to(repo)}"
                )
                continue
            validate_hash(repo, target, entry.get("sha256"), problems, "overlay notice")

        for entry in data.get("routing", []):
            if not isinstance(entry, dict):
                problems.append(
                    f"invalid overlay routing entry: {lock_path.relative_to(repo)}"
                )
                continue
            bundle = entry.get("bundle")
            try:
                target = relative_path(
                    entry.get("target"), label="overlay routing target"
                )
            except ValueError:
                problems.append(
                    f"overlay routing target escapes project: {lock_path.relative_to(repo)}"
                )
                continue
            if not isinstance(bundle, str) or not bundle:
                problems.append(
                    f"overlay routing is missing bundle: {lock_path.relative_to(repo)}"
                )
                continue
            target_path = repo / target
            if not target_path.is_file():
                problems.append(f"overlay routing target is missing: {target}")
                continue
            marker = f"harness-overlay:{data.get('overlay_id')}/{bundle}"
            match = re.search(
                rf"<!-- {re.escape(marker)}:start -->\n(.*?)<!-- {re.escape(marker)}:end -->",
                target_path.read_text(encoding="utf-8"),
                re.DOTALL,
            )
            actual = digest(match.group(1).encode()) if match else None
            if actual != entry.get("sha256"):
                problems.append(f"overlay routing block mismatch: {marker}")

    actual_names = {name for name, _, _ in skill_inventory(repo)}
    unmanaged = actual_names - public_skill_names(lock)
    if unmanaged != locked_names:
        missing = sorted(unmanaged - locked_names)
        stale = sorted(locked_names - unmanaged)
        if missing:
            problems.append(
                f"project skills missing provenance lock: {', '.join(missing)}"
            )
        if stale:
            problems.append(
                f"overlay lock claims absent/public skills: {', '.join(stale)}"
            )


def validate_integrations(repo: Path, problems: list[str]) -> int:
    known_paths = [
        Path(".mcp.json"),
        Path(".codex/hooks.json"),
        Path(".claude/settings.json"),
        Path(".claude/settings.local.json"),
        Path(".kimi/mcp.json"),
        Path("opencode.json"),
        Path("opencode.jsonc"),
    ]
    present = {path.as_posix() for path in known_paths if (repo / path).is_file()}
    inventory = repo / INTEGRATIONS_REL
    if not inventory.is_file():
        if present:
            problems.append(
                f"project integrations are not inventoried: {', '.join(sorted(present))}"
            )
        return 0
    try:
        data = json.loads(inventory.read_text(encoding="utf-8"))
    except Exception as exc:
        problems.append(f"cannot read {INTEGRATIONS_REL}: {exc}")
        return 0
    entries = (
        data.get("integrations")
        if isinstance(data, dict) and data.get("schema") == 1
        else None
    )
    if not isinstance(entries, list):
        problems.append(
            f"{INTEGRATIONS_REL} must use schema 1 with an integrations list"
        )
        return 0
    recorded: set[str] = set()
    ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            problems.append(f"invalid integration entry in {INTEGRATIONS_REL}")
            continue
        identifier = entry.get("id")
        kind = entry.get("kind")
        runtimes = entry.get("runtimes")
        secrets = entry.get("secret_refs", [])
        verify = entry.get("verify")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            problems.append(f"integration id is missing or duplicated: {identifier!r}")
        else:
            ids.add(identifier)
        if kind not in {"mcp", "plugin", "hook", "runtime-setting", "other"}:
            problems.append(f"integration {identifier!r} has invalid kind")
        if (
            not isinstance(runtimes, list)
            or not runtimes
            or not all(isinstance(item, str) for item in runtimes)
        ):
            problems.append(f"integration {identifier!r} needs runtimes")
        if not isinstance(verify, str) or not verify.strip():
            problems.append(f"integration {identifier!r} needs a verification action")
        if not isinstance(secrets, list) or not all(
            isinstance(item, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", item)
            for item in secrets
        ):
            problems.append(
                f"integration {identifier!r} secret_refs must contain environment variable names only"
            )
        try:
            config = relative_path(
                entry.get("config"), label=f"integration {identifier!r} config"
            )
        except ValueError:
            problems.append(f"integration {identifier!r} config escapes project")
            continue
        recorded.add(config.as_posix())
        validate_hash(
            repo, config, entry.get("sha256"), problems, f"integration {identifier!r}"
        )
    missing = present - recorded
    if missing:
        problems.append(
            f"project integrations are not inventoried: {', '.join(sorted(missing))}"
        )
    return len(entries)


def validate_project_json(repo: Path, problems: list[str]) -> None:
    """.harness/project.json is optional - only pvmalove-suite writes it - so a missing file is
    not itself a problem; only validate its shape when it's actually there. Mirrors
    harness/project/project.schema.json by hand: that schema is editor-facing only, this repo
    has no jsonschema dependency to enforce it at runtime, so keep both in sync by eye."""
    path = repo / ".harness/project.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        problems.append(f".harness/project.json is not valid JSON: {exc}")
        return
    if not isinstance(data, dict):
        problems.append(".harness/project.json must contain a JSON object")
        return

    missing = [field for field in PROJECT_JSON_REQUIRED_FIELDS if field not in data]
    if missing:
        problems.append(
            f".harness/project.json missing required field(s): {', '.join(missing)}"
        )
    extra = sorted(set(data) - PROJECT_JSON_ALLOWED_FIELDS)
    if extra:
        problems.append(
            f".harness/project.json has unknown field(s): {', '.join(extra)}"
        )

    if "language" in data and data["language"] not in ("ru", "en"):
        problems.append(
            f".harness/project.json language must be 'ru' or 'en', got {data['language']!r}"
        )
    for field in ("base_branch", "branch_pattern"):
        if field in data and (not isinstance(data[field], str) or not data[field]):
            problems.append(f".harness/project.json {field} must be a non-empty string")
    if "qa_gate_commands" in data:
        commands = data["qa_gate_commands"]
        if not isinstance(commands, list) or not all(
            isinstance(item, str) for item in commands
        ):
            problems.append(
                ".harness/project.json qa_gate_commands must be a list of strings"
            )
    if "shell" in data and data["shell"] not in ("bash", "powershell"):
        problems.append(
            f".harness/project.json shell must be 'bash' or 'powershell', got {data['shell']!r}"
        )
    if "story_points" in data:
        story_points = data["story_points"]
        if not isinstance(story_points, dict):
            problems.append(".harness/project.json story_points must be an object")
        else:
            sp_missing = [
                f for f in STORY_POINTS_REQUIRED_FIELDS if f not in story_points
            ]
            if sp_missing:
                problems.append(
                    f".harness/project.json story_points missing field(s): {', '.join(sp_missing)}"
                )
            sp_extra = sorted(set(story_points) - STORY_POINTS_ALLOWED_FIELDS)
            if sp_extra:
                problems.append(
                    f".harness/project.json story_points has unknown field(s): {', '.join(sp_extra)}"
                )
            scale = story_points.get("scale")
            if "scale" in story_points and (
                not isinstance(scale, list)
                or not scale
                or not all(
                    isinstance(v, int) and not isinstance(v, bool) and v >= 1
                    for v in scale
                )
            ):
                problems.append(
                    ".harness/project.json story_points.scale must be a non-empty list of integers >= 1"
                )
            for field in ("fast_threshold", "full_threshold", "gray_zone"):
                if field in story_points and (
                    not isinstance(story_points[field], int)
                    or isinstance(story_points[field], bool)
                ):
                    problems.append(
                        f".harness/project.json story_points.{field} must be an integer"
                    )


def validate_orchestration_config(repo: Path, problems: list[str]) -> None:
    """Adapt portable orchestration policy into accumulated health diagnostics.

    `health_problems` lives in harness.orchestration.contract, which only ships to a project that
    selected the backend-orchestration capability; callers only reach this function when that
    capability is present (see check_orchestration_config), so the import stays local to this
    function rather than at module level - a module-level import would make every `harness health`
    run require harness.orchestration.contract, even for projects without backend-orchestration.
    """
    from harness.orchestration.contract import health_problems

    if not (repo / ORCHESTRATION_SCHEMA_REL).is_file():
        problems.append(
            f"missing {ORCHESTRATION_SCHEMA_REL.as_posix()} for backend-orchestration capability"
        )
    problems.extend(
        health_problems(
            repo / ORCHESTRATION_CONFIG_REL, repo / ".harness/orchestration/roles"
        )
    )


def verification_routing_health(repo: Path) -> list[str]:
    """Предупредить, если developer получает полный QA-gate вместо фокусных проверок.

    Без `developer_verification_commands` coordinator отдаёт developer весь `verification_commands`,
    и каждая итерация разработки гоняет полный gate. Это совместимое поведение, а не ошибка, поэтому
    health только предупреждает и подсказывает поле.
    """
    config_path = repo / ORCHESTRATION_CONFIG_REL
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if (
        not isinstance(config, dict)
        or not config.get("verification_commands")
        or "developer_verification_commands" in config
    ):
        return []
    return [
        "ПРЕДУПРЕЖДЕНИЕ: developer получает полный verification_commands на каждой итерации",
        "КАК ИСПРАВИТЬ: задайте developer_verification_commands (и review_verification_commands) "
        "с быстрыми task-scoped проверками в .harness/orchestration.json",
    ]


# --- Registry-facing checks ------------------------------------------------------------------


def check_lock(context: HealthContext) -> CheckResult:
    if context.lock is None:
        return CheckResult(
            id="files.lock",
            group="files",
            status="fail",
            message="отсутствует .harness/harness.lock",
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
        )
    agents_text = agents_path.read_text(encoding="utf-8")
    if _TEMPLATE_MARKER.search(agents_text):
        return CheckResult(
            id="files.agents_md",
            group="files",
            status="fail",
            message="в AGENTS.md остались нерешённые плейсхолдеры шаблона",
        )
    return CheckResult(
        id="files.agents_md",
        group="files",
        status="ok",
        message="AGENTS.md заполнен, нерешённых плейсхолдеров нет",
    )


def check_discovery_links(context: HealthContext) -> CheckResult:
    broken: list[str] = []
    for relative, target in DISCOVERY_LINKS.items():
        path = context.repo / relative
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
    if broken:
        return CheckResult(
            id="files.discovery_links",
            group="files",
            status="fail",
            message="; ".join(broken),
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
            message=_NO_ORCHESTRATION_CAPABILITY_MESSAGE,
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
            message=_NO_LOCK_MESSAGE,
        )
    if context.snapshot_diff is None:
        # snapshot_diff itself stays in harness/bin/harness (it re-derives the expected package
        # content from CAPABILITIES.json and the harness/ source tree, which never ships to an
        # installed project); only the canonical `harness health` CLI can supply it (see cmd_health).
        return CheckResult(
            id="files.skill_snapshot",
            group="files",
            status="skipped",
            message="снэпшот скиллов доступен только из харнесс-пакетировщика (harness/bin/harness)",
        )
    result = context.snapshot_diff(context.repo)
    if result["state"] != "clean":
        return CheckResult(
            id="files.skill_snapshot",
            group="files",
            status="fail",
            message="в снэпшоте скиллов есть расхождения",
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
            message=_NO_LOCK_MESSAGE,
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
        )
    if registry_path.read_text(encoding="utf-8") != expected_registry:
        return CheckResult(
            id="files.skill_registry",
            group="files",
            status="fail",
            message=f"устарел {registry_rel}; выполните harness registry",
        )
    return CheckResult(
        id="files.skill_registry",
        group="files",
        status="ok",
        message="реестр скиллов актуален",
    )


def check_overlay_locks(context: HealthContext) -> CheckResult:
    if context.lock is None:
        return CheckResult(
            id="files.overlay_locks",
            group="files",
            status="skipped",
            message=_NO_LOCK_MESSAGE,
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
            message=_NO_LOCK_MESSAGE,
        )
    problems: list[str] = []
    count = validate_integrations(context.repo, problems)
    if problems:
        return CheckResult(
            id="files.integrations",
            group="files",
            status="fail",
            message="; ".join(problems),
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
            message=_NO_ORCHESTRATION_CAPABILITY_MESSAGE,
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
            message=lines[0],
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
