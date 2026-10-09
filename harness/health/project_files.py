"""Файлы проекта, управляемые харнессом: пути, хеширование, реестр и инвентарь навыков, а также валидаторы harness.lock, overlay-локов, интеграций, project.json и конфигурации оркестрации.

Единое определение, используемое как сборщиком (`harness/bin/harness.py` импортирует отсюда всё необходимое),
так и проверками файлов (checks/files.py), что позволяет пакету работать автономно после копирования
в `.harness/health/` проекта (см. docs/adr/0001). Ни одна функция здесь не формирует `CheckResult`:
валидаторы добавляют понятные человеку описания проблем, а checks/files.py преобразует их в результаты.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit

from .model import JsonObject

# --- Paths and constants ------------------------------------

REGISTRY_REL = Path(".harness/skills/REGISTRY.md")
LOCK_REL = Path(".harness/harness.lock")
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
    "memory",
    "memory_policy",
    "tracker",
    "ci_required_checks",
}
STORY_POINTS_REQUIRED_FIELDS = (
    "scale",
    "fast_threshold",
    "full_threshold",
    "gray_zone",
)
STORY_POINTS_ALLOWED_FIELDS = frozenset(STORY_POINTS_REQUIRED_FIELDS)

# The optional `tracker` field (docs/adr/0011): the single definition of its rules. The project
# tracker resolver (project_tracker.py) imports them from here, and `project.schema.json` repeats
# the same patterns verbatim - tests/health/test_health_checks_files.py keeps the two in step.
TRACKER_FIELDS = ("type", "host", "project")
TRACKER_TYPES = ("github", "gitlab", "local")
TRACKER_HOSTED_TYPES = ("github", "gitlab")
# A hostname with an optional :port - no scheme, path or userinfo.
TRACKER_HOST_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?$"
# The full project path including subgroups, at least two segments, no leading/trailing slash.
TRACKER_PROJECT_PATTERN = r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$"
# What a host or project value breaking its pattern must look like instead - never the value.
TRACKER_HOST_RULE = (
    "must be a hostname with an optional :port, without scheme, path or userinfo"
)
TRACKER_PROJECT_RULE = (
    "must be the full project path with subgroups (group/sub/project), "
    "without a leading or trailing slash"
)
# Bound of the `git ls-files` skill inventory.
GIT_TIMEOUT_SECONDS = 30
# A JSON file cannot be opened, decoded or parsed (too deep nesting included).
JSON_READ_ERRORS = (OSError, ValueError, RecursionError)


# --- Detection helpers moved unchanged from harness/bin/harness.py ----------------------------


def native_link_target(target: str) -> str:
    """Преобразовать целевой путь ссылки в разделители текущей ОС.

    Цели `DISCOVERY_LINKS` используют '/' для переносимости. Точки соединения (symlink reparse points)
    в Windows разрешают относительные цели через '\\'; использование слэша '/' создаёт точку соединения,
    которая отображается корректно (в листинге виден `SYMLINKD`), но не разрешается, скрывая все навыки.
    """
    return target.replace("/", os.sep)


def digest(data: bytes) -> str:
    """Вычислить SHA-256 хеш байтовых данных в виде шестнадцатеричной строки."""
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str | None:
    """Вычислить SHA-256 хеш файла или вернуть None, если файл не найден."""
    try:
        return digest(path.read_bytes())
    except FileNotFoundError:
        return None


def relative_path(value: object, *, label: str) -> Path:
    """Проверить и преобразовать значение в относительный путь внутри проекта."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must stay inside the project: {value}")
    return path


def frontmatter_metadata(path: Path) -> tuple[str, str]:
    """Извлечь имя и описание навыка из YAML-заголовка файла SKILL.md."""
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
    """Собрать инвентарь навыков проекта: список кортежей (имя, путь к каталогу, описание)."""
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
    """Сформировать markdown-содержимое реестра навыков проекта REGISTRY.md."""
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
    """Проверить соответствие SHA-256 хеша файла ожидаемому значению и зафиксировать проблемы при расхождении."""
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        problems.append(f"{label} has invalid sha256: {relative.as_posix()}")
        return
    actual = file_digest(repo / relative)
    if actual is None:
        problems.append(f"{label} file is missing: {relative.as_posix()}")
    elif actual != expected:
        problems.append(f"{label} hash mismatch: {relative.as_posix()}")


def public_skill_names(lock: JsonObject) -> set[str]:
    """Извлечь имена публичных навыков из секции files объекта harness.lock."""
    names: set[str] = set()
    for value in lock.get("files") or {}:
        parts = Path(value).parts
        if len(parts) >= 4 and parts[:2] == (".harness", "skills"):
            names.add(parts[2])
    return names


def fail(message: str) -> NoReturn:
    """Вывести сообщение об ошибке в stderr и завершить процесс с кодом 1.

    Ошибка сборщика, разделяемая с `harness/bin/harness.py`. Запуск проверок здоровья изолирует
    SystemExit для каждой проверки в `registry.run`, предотвращая аварийную остановку всего отчёта.
    """
    print(f"harness: {message}", file=sys.stderr)
    raise SystemExit(1)


def git_command(repo: Path, *arguments: str) -> list[str]:
    """Сформировать аргументы команды git с безопасной привязкой к переданному каталогу репозитория."""
    checkout = repo.resolve()
    return ["git", "-c", f"safe.directory={checkout}", "-C", str(checkout), *arguments]


def project_skill_files(repo: Path, directory: Path) -> list[Path]:
    """Перечислить файлы навыка через Git, исключая артефакты из .gitignore.

    Использует Git вместо прямого обхода файловой системы, чтобы игнорируемые артефакты
    (node_modules, кэши сборки) не попадали в lock-файл локальных хешей и не вызывали ложный дрейф.
    """
    try:
        relative = directory.relative_to(repo)
    except ValueError:
        fail(f"project skill path escapes repository: {directory}")
    try:
        result = subprocess.run(
            git_command(
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
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"cannot inventory project skill files: {directory}: {exc}")
    if result.returncode != 0:
        fail(f"cannot inventory project skill files: {directory}")
    return sorted(
        repo / Path(value.decode("utf-8", errors="surrogateescape"))
        for value in result.stdout.split(b"\0")
        if value
        and (repo / Path(value.decode("utf-8", errors="surrogateescape"))).is_file()
    )


def validate_overlay_locks(repo: Path, lock: JsonObject, problems: list[str]) -> None:
    """Проверить корректность всех overlay-локов в каталоге .harness/overlays и их соответствие файлам."""
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
        except JSON_READ_ERRORS as exc:
            problems.append(
                f"cannot read overlay lock {lock_path.relative_to(repo).as_posix()}: {exc}"
            )
            continue
        if not isinstance(data, dict):
            problems.append(
                f"invalid overlay lock header: {lock_path.relative_to(repo).as_posix()}"
            )
            continue
        if data.get("schema") != 1 or not isinstance(data.get("overlay_id"), str):
            problems.append(
                f"invalid overlay lock header: {lock_path.relative_to(repo).as_posix()}"
            )
        source = data.get("source")
        if not isinstance(source, dict):
            problems.append(f"missing overlay source: {lock_path.relative_to(repo).as_posix()}")
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
                    f"overlay source needs remote and revision: {lock_path.relative_to(repo).as_posix()}"
                )
            elif "://" in remote and (
                urlsplit(remote).username or urlsplit(remote).password
            ):
                problems.append(
                    f"overlay remote contains credentials: {lock_path.relative_to(repo).as_posix()}"
                )

        skills = data.get("skills")
        if not isinstance(skills, list):
            problems.append(
                f"overlay skills must be a list: {lock_path.relative_to(repo).as_posix()}"
            )
            continue
        for entry in skills:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                problems.append(
                    f"invalid overlay skill entry: {lock_path.relative_to(repo).as_posix()}"
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
                    f"invalid overlay notice: {lock_path.relative_to(repo).as_posix()}"
                )
                continue
            try:
                target = relative_path(
                    entry.get("target"), label="overlay notice target"
                )
            except ValueError:
                problems.append(
                    f"overlay notice target escapes project: {lock_path.relative_to(repo).as_posix()}"
                )
                continue
            validate_hash(repo, target, entry.get("sha256"), problems, "overlay notice")

        for entry in data.get("routing", []):
            if not isinstance(entry, dict):
                problems.append(
                    f"invalid overlay routing entry: {lock_path.relative_to(repo).as_posix()}"
                )
                continue
            bundle = entry.get("bundle")
            try:
                target = relative_path(
                    entry.get("target"), label="overlay routing target"
                )
            except ValueError:
                problems.append(
                    f"overlay routing target escapes project: {lock_path.relative_to(repo).as_posix()}"
                )
                continue
            if not isinstance(bundle, str) or not bundle:
                problems.append(
                    f"overlay routing is missing bundle: {lock_path.relative_to(repo).as_posix()}"
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
    """Проверить файл каталога интеграций .harness/integrations.json и наличие неучтённых файлов конфигураций."""
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
    except JSON_READ_ERRORS as exc:
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


def tracker_field_problems(value: object) -> list[str]:
    """Problems of the `tracker` field value of .harness/project.json; empty when it is valid.

    The `host` and `project` values never appear in a message: a pasted URL can carry userinfo
    credentials, which must not reach a health report or the console.
    """
    prefix = ".harness/project.json tracker"
    if not isinstance(value, dict):
        return [f"{prefix} must be an object"]
    problems: list[str] = []
    extra = sorted(str(key) for key in set(value) - set(TRACKER_FIELDS))
    if extra:
        problems.append(f"{prefix} has unknown field(s): {', '.join(extra)}")
    tracker_type = value.get("type")
    if "type" not in value:
        problems.append(f"{prefix} missing required field(s): type")
    elif tracker_type not in TRACKER_TYPES:
        problems.append(f"{prefix} type must be one of: {', '.join(TRACKER_TYPES)}")
    elif tracker_type in TRACKER_HOSTED_TYPES:
        missing = [field for field in ("host", "project") if field not in value]
        if missing:
            problems.append(
                f"{prefix} of type {tracker_type} missing field(s): {', '.join(missing)}"
            )
    host = value.get("host")
    if "host" in value and not (
        isinstance(host, str) and re.fullmatch(TRACKER_HOST_PATTERN, host)
    ):
        problems.append(f"{prefix} host {TRACKER_HOST_RULE}")
    project = value.get("project")
    if "project" in value and not (
        isinstance(project, str) and re.fullmatch(TRACKER_PROJECT_PATTERN, project)
    ):
        problems.append(f"{prefix} project {TRACKER_PROJECT_RULE}")
    return problems


def ci_required_checks_problems(value: object) -> list[str]:
    """Problems of the `ci_required_checks` value: a list of unique non-empty CI check names
    (empty list = not configured).  Names are shown by position, never echoed."""
    prefix = ".harness/project.json ci_required_checks"
    if not isinstance(value, list):
        return [f"{prefix} must be a list of check names"]
    if not all(isinstance(item, str) and item.strip() for item in value):
        return [f"{prefix} must contain only non-empty strings"]
    if len(set(value)) != len(value):
        return [f"{prefix} must not contain duplicate names"]
    return []


def validate_project_json(repo: Path, problems: list[str]) -> None:
    """.harness/project.json не является обязательным (создаётся только pvmalove-suite), поэтому его
    отсутствие не считается ошибкой; проверяется только при его наличии.
    Структура вручную синхронизируется с `harness/project/project.schema.json`: эта схема предназначена
    только для редактора, в рантайме нет зависимости `jsonschema`, поэтому соответствие поддерживается синхронно.
    """
    path = repo / ".harness/project.json"
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except JSON_READ_ERRORS as exc:
        problems.append(f".harness/project.json is not valid JSON: {exc}")
        return
    if not isinstance(data, dict):
        problems.append(".harness/project.json must contain a JSON object")
        return

    if "memory" in data or "memory_policy" in data:
        from harness.memory.policy import parse_policy

        try:
            parse_policy(data)
        except ValueError as exc:
            problems.append(f".harness/project.json {exc}")

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
    if "tracker" in data:
        problems.extend(tracker_field_problems(data["tracker"]))
    if "ci_required_checks" in data:
        problems.extend(ci_required_checks_problems(data["ci_required_checks"]))
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
    """Проверить конфигурацию оркестрации .harness/orchestration.json через контракт backend-orchestration.

    Функция `health_problems` находится в `harness.orchestration.contract`, который поставляется
    только в проекты с выбранной возможностью backend-orchestration; вызовы достигают этой функции
    только при наличии данной возможности (см. check_orchestration_config), поэтому импорт
    выполняется локально внутри функции, а не на уровне модуля — импорт на уровне модуля потребовал
    бы наличия `harness.orchestration.contract` при каждом запуске `harness health`.
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
    except JSON_READ_ERRORS:
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
