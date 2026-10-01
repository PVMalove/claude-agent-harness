#!/usr/bin/env python3
"""CLI пакетирования harness проекта во время сборки. Не участвует в сессиях агента."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Captured before the forced UTF-8 below so `harness health` can still warn about the console's
# own encoding (harness.health.checks.environment.check_output_encoding).
ORIGINAL_STDOUT_ENCODING: str | None = getattr(sys.stdout, "encoding", None)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

MIN_PYTHON = (3, 12)  # same floor as bin/install-global.py and scripts/test_clean_room
if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "[ERROR] harness requires Python %s+ (found %s).\n"
        % (".".join(map(str, MIN_PYTHON)), sys.version.split()[0])
    )
    sys.exit(1)


PACKAGE = Path(__file__).resolve().parent.parent
ROOT = PACKAGE.parent
# Run as a script, this file's own directory is on sys.path, where `harness.py` would shadow the
# `harness` package; ROOT goes first even when PYTHONPATH already lists it behind that directory.
_BIN_DIR = Path(__file__).resolve().parent
sys.path[:] = [
    entry
    for entry in sys.path
    if entry != str(ROOT) and Path(entry or os.curdir).resolve() != _BIN_DIR
]
sys.path.insert(0, str(ROOT))
from harness.errors import HarnessError, print_and_exit
from harness.cleanup import apply_cleanup, plan_cleanup
from harness.uninstall import (
    CLAUDE_MD_SEED,
    CONFIRM_WORD,
    apply_uninstall,
    plan_uninstall,
)
from harness.storage import storage_path
from harness.health import registry as health_registry
from harness.health import render as health_render
from harness.health import report_json as health_report_json
from harness.health.model import JsonObject
from harness.health.project_files import (
    BACKEND_ORCHESTRATION_CAPABILITY,
    DISCOVERY_LINKS,
    INTEGRATIONS_REL,
    LOCK_REL,
    REGISTRY_REL,
    digest,
    fail,
    file_digest,
    git_command,
    native_link_target,
    project_registry,
    project_skill_files,
    public_skill_names,
    skill_inventory,
    tracker_field_problems,
)
from harness.health.project_tracker import resolve_project_tracker

CAPABILITIES_FILE = PACKAGE / "CAPABILITIES.json"
VERSION_FILE = PACKAGE / "VERSION"
# How printed remedies invoke this CLI: the running interpreter and this script, runnable as shown.
HARNESS_CLI = (sys.executable, str(Path(__file__).resolve()))
DEFAULT_CAPABILITY = "project-foundation"


def write_registry(repo: Path) -> None:
    """Записать актуальный реестр скиллов в файл REGISTRY.md проекта."""
    path = repo / REGISTRY_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        content = project_registry(repo)
    except ValueError as exc:
        fail(str(exc))
    path.write_text(content, encoding="utf-8", newline="\n")


def skill_file_hashes(repo: Path, directory: Path) -> dict[str, str]:
    """Вычислить хэши sha256 всех файлов скилла относительно его директории."""
    return {
        path.relative_to(directory).as_posix(): digest(path.read_bytes())
        for path in project_skill_files(repo, directory)
    }


def version() -> str:
    """Получить текущую версию пакета harness из файла VERSION."""
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def source_revision() -> str:
    """Получить ревизию Git исходного репозитория harness."""
    result = subprocess.run(
        git_command(ROOT, "rev-parse", "HEAD"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else "uncommitted"


def capabilities() -> dict[str, JsonObject]:
    """Загрузить и распарсить каталог возможностей из CAPABILITIES.json."""
    try:
        data: object = json.loads(CAPABILITIES_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"cannot read {CAPABILITIES_FILE}: {exc}")
    if not isinstance(data, dict):
        fail("CAPABILITIES.json must contain an object")
    return data


def selected_capabilities(names: list[str] | None) -> list[str]:
    """Нормализовать и проверить переданный список возможностей."""
    selected = names or [DEFAULT_CAPABILITY]
    catalog = capabilities()
    unknown = [name for name in selected if name not in catalog]
    if unknown:
        fail(f"unknown capabilities: {', '.join(unknown)}")
    return list(dict.fromkeys(selected))


def _resolve_skill_source(entry: str) -> Path:
    """Найти исходный каталог скилла в репозитории harness."""
    source = ROOT / "skills" / entry
    if not (source / "SKILL.md").is_file():
        fail(f"missing skill: skills/{entry}/SKILL.md")
    return source


def _resolve_resource_source(entry: str) -> Path:
    """Найти исходный путь ресурса возможности в репозитории harness."""
    source = PACKAGE / entry
    if not source.is_dir() and not source.is_file():
        fail(f"missing capability resource: harness/{entry}")
    return source


def resolve_capability_skills(
    name: str, catalog: dict[str, JsonObject], _stack: tuple[str, ...] = ()
) -> dict[str, Path]:
    """Разрешить возможность в словарь {имя_скилла: путь_к_источнику}.

    Обрабатывает наследование `extends`, переопределения `overrides` и дополнения `additions`.
    """
    if name in _stack:
        fail(f"capability cycle: {' -> '.join(_stack + (name,))}")
    definition = catalog[name]
    resolved: dict[str, Path] = {}

    extends = definition.get("extends")
    if extends:
        if extends not in catalog:
            fail(f"capability {name!r} extends unknown capability {extends!r}")
        resolved.update(resolve_capability_skills(extends, catalog, _stack + (name,)))

    for entry in definition.get("skills") or []:
        source = _resolve_skill_source(entry)
        resolved[source.name] = source

    for skill_name, entry in (definition.get("overrides") or {}).items():
        if skill_name not in resolved:
            fail(
                f"capability {name!r} overrides unknown skill {skill_name!r} (not in {extends!r})"
            )
        resolved[skill_name] = _resolve_skill_source(entry)

    for entry in definition.get("additions") or []:
        source = _resolve_skill_source(entry)
        if source.name in resolved:
            fail(
                f"capability {name!r} addition {source.name!r} collides with an inherited skill"
            )
        resolved[source.name] = source

    return resolved


def resolve_capability_resources(
    name: str, catalog: dict[str, JsonObject], _stack: tuple[str, ...] = ()
) -> dict[str, Path]:
    """Разрешить управляемые не-скилловые ресурсы для возможности и её родителей.

    Ресурсы сохраняют свой относительный путь при развёртывании в проектной директории `.harness/`.
    """
    if name in _stack:
        fail(f"capability cycle: {' -> '.join(_stack + (name,))}")
    definition = catalog[name]
    resolved: dict[str, Path] = {}

    extends = definition.get("extends")
    if extends:
        if extends not in catalog:
            fail(f"capability {name!r} extends unknown capability {extends!r}")
        resolved.update(
            resolve_capability_resources(extends, catalog, _stack + (name,))
        )

    for entry in definition.get("resources") or []:
        resolved[entry] = _resolve_resource_source(entry)

    return resolved


def selected_skills(names: list[str]) -> list[Path]:
    """Получить список путей ко всем скиллам, входящим в выбранные возможности."""
    catalog = capabilities()
    paths: list[Path] = []
    seen_names: dict[str, Path] = {}
    for capability in names:
        for skill_name, source in resolve_capability_skills(
            capability, catalog
        ).items():
            if skill_name in seen_names and seen_names[skill_name] != source:
                fail(
                    f"duplicate skill name {skill_name!r}: {seen_names[skill_name]} and {source}"
                )
            if skill_name not in seen_names:
                seen_names[skill_name] = source
                paths.append(source)
    return paths


def selected_resources(names: list[str]) -> list[Path]:
    """Получить список путей ко всем ресурсам, входящим в выбранные возможности."""
    catalog = capabilities()
    paths: list[Path] = []
    seen_destinations: dict[Path, Path] = {}
    for capability in names:
        for source in resolve_capability_resources(capability, catalog).values():
            destination = source.relative_to(PACKAGE)
            if (
                destination in seen_destinations
                and seen_destinations[destination] != source
            ):
                fail(
                    f"duplicate capability resource {destination}: "
                    f"{seen_destinations[destination]} and {source}"
                )
            if destination not in seen_destinations:
                seen_destinations[destination] = source
                paths.append(source)
    return paths


def _packageable(path: Path) -> bool:
    """Пропустить локальные артефакты сборки, байт-код и кэши при пакетировании."""
    return path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"


def package_files(names: list[str]) -> dict[str, bytes]:
    """Сформировать словарь относительных целевых путей и байтового содержимого файлов пакета."""
    result: dict[str, bytes] = {}
    for source in selected_skills(names):
        for path in sorted(source.rglob("*")):
            if not _packageable(path):
                continue
            relative = path.relative_to(source)
            target = Path(".harness/skills") / source.name / relative
            result[target.as_posix()] = path.read_bytes()
    for source in selected_resources(names):
        if source.is_file():
            if _packageable(source):
                target = Path(".harness") / source.relative_to(PACKAGE)
                result[target.as_posix()] = source.read_bytes()
            continue
        for path in sorted(source.rglob("*")):
            if not _packageable(path):
                continue
            relative = path.relative_to(source)
            target = Path(".harness") / source.relative_to(PACKAGE) / relative
            result[target.as_posix()] = path.read_bytes()
    return result


def render(template: str, values: dict[str, str]) -> str:
    """Подставить значения плейсхолдеров {{KEY}} в шаблонную строку."""
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", value)
    return template


def load_lock(repo: Path) -> JsonObject | None:
    """Загрузить и распарсить файл блокировки .harness/harness.lock репозитория."""
    path = repo / LOCK_REL
    if not path.is_file():
        return None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"cannot read {path}: {exc}")
    if not isinstance(data, dict):
        fail(f"cannot read {path}: expected a JSON object")
    return data


def ensure_git_repo(repo: Path) -> None:
    """Убедиться, что целевая директория является репозиторием Git."""
    result = subprocess.run(
        git_command(repo, "rev-parse", "--show-toplevel"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if result.returncode != 0:
        fail(f"not a Git repository: {repo} (run 'git init' there first)")


def ensure_links(
    repo: Path,
    *,
    replace: bool = False,
    fix_hint: str = "remove or rename it, then retry",
) -> list[str]:
    """Создать или обновить символические ссылки обнаружения скиллов для сред выполнения."""
    changed: list[str] = []
    for relative, target in DISCOVERY_LINKS.items():
        native_target = native_link_target(target)
        path = repo / relative
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            continue
        if path.is_symlink() and os.readlink(path) == native_target and path.is_dir():
            continue
        if path.exists() or path.is_symlink():
            if not replace:
                fail(
                    f"discovery path already exists and is not managed: {path} ({fix_hint})"
                )
            if path.is_dir() and not path.is_symlink():
                fail(f"refusing to replace directory: {path}")
            try:
                path.unlink()
            except PermissionError:
                continue
        try:
            path.symlink_to(native_target)
            changed.append(relative)
        except PermissionError:
            continue
    return changed


def write_snapshot(repo: Path, selected: list[str], *, force: bool) -> list[str]:
    """Записать снимок файлов возможностей, сформировать harness.lock и обновить реестр."""
    files = package_files(selected)
    skills_root = repo / ".harness/skills"
    skills_root.mkdir(parents=True, exist_ok=True)

    if force:
        lock = load_lock(repo) or {}
        for relative in lock.get("files") or {}:
            path = repo / relative
            if path.is_file():
                path.unlink()

    written: list[str] = []
    for relative, data in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if "/scripts/" in f"/{relative}/" and data.startswith(b"#!"):
            path.chmod(path.stat().st_mode | 0o111)
        written.append(relative)

    lock = {
        "schema": 1,
        "package_version": version(),
        "source_revision": source_revision(),
        "capabilities": selected,
        "files": {relative: digest(data) for relative, data in sorted(files.items())},
    }
    lock_path = repo / LOCK_REL
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(lock, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    prune_empty_skill_directories(repo)
    write_registry(repo)
    written.append(LOCK_REL.as_posix())
    written.append(REGISTRY_REL.as_posix())
    return written


def prune_empty_skill_directories(repo: Path) -> None:
    """Удалить пустые директории скиллов верхнего уровня, оставшиеся после удаления управляемых скиллов."""
    root = repo / ".harness/skills"
    if not root.is_dir():
        return
    for directory in root.iterdir():
        if directory.is_dir() and not directory.is_symlink():
            try:
                directory.rmdir()
            except OSError:
                pass


REPO_MAP_REGISTRY_REL = Path(
    ".harness/.sandboxes/cache/repo_map/parser_bundle/registry"
)


def _non_empty_string(value: object) -> bool:
    """Проверить, является ли значение непустой строкой."""
    return isinstance(value, str) and bool(value)


def _string_list(value: object) -> bool:
    """Проверить, является ли значение списком непустых строк."""
    return isinstance(value, list) and all(_non_empty_string(item) for item in value)


def record_integration(
    repo: Path,
    *,
    identifier: str,
    kind: str,
    runtimes: list[str],
    config: Path,
    secret_refs: list[str],
    verify: str,
) -> None:
    """Зарегистрировать или обновить запись интеграции в .harness/integrations.json."""
    path = repo / INTEGRATIONS_REL
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except Exception:
        data = {}
    entries = [
        entry
        for entry in (data.get("integrations") or [])
        if entry.get("id") != identifier
    ]
    entries.append(
        {
            "id": identifier,
            "kind": kind,
            "runtimes": runtimes,
            "config": config.as_posix(),
            "sha256": digest((repo / config).read_bytes()),
            "secret_refs": secret_refs,
            "verify": verify,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": 1, "integrations": entries}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def snapshot_diff(repo: Path, override: list[str] | None = None) -> JsonObject:
    """Сравнить установленный снимок файлов с эталонным пакетом возможностей."""
    lock = load_lock(repo)
    if lock is None:
        return {"state": "missing", "detail": "no .harness/harness.lock"}

    selected = selected_capabilities(override or lock.get("capabilities"))
    current = package_files(selected)
    previous: dict[str, str] = lock.get("files") or {}
    added: list[str] = []
    missing: list[str] = []
    changed: list[str] = []
    local: list[str] = []
    conflict: list[str] = []
    retired: list[str] = []

    for relative, data in current.items():
        package_hash = digest(data)
        disk_hash = file_digest(repo / relative)
        old_hash = previous.get(relative)
        if old_hash is None:
            added.append(relative)
        elif disk_hash is None:
            missing.append(relative)
        elif disk_hash == package_hash:
            continue
        elif old_hash == package_hash:
            local.append(relative)
        elif disk_hash == old_hash:
            changed.append(relative)
        else:
            conflict.append(relative)

    for relative, old_hash in previous.items():
        if relative in current:
            continue
        disk_hash = file_digest(repo / relative)
        if disk_hash is None:
            continue
        if disk_hash == old_hash:
            retired.append(relative)
        else:
            conflict.append(relative)

    state = "clean"
    if added or missing or changed or local or conflict or retired:
        state = "drift"
    return {
        "state": state,
        "package_version": version(),
        "installed_version": lock.get("package_version"),
        "capabilities": selected,
        "added": sorted(added),
        "missing": sorted(missing),
        "package_changed": sorted(changed),
        "local_changed": sorted(local),
        "conflict": sorted(set(conflict)),
        "retired": sorted(retired),
    }


def print_diff(result: JsonObject) -> None:
    """Вывести текстовый отчёт о различиях снимка файлов в консоль."""
    print(f"state: {result['state']}")
    if "capabilities" in result:
        print(f"capabilities: {', '.join(result['capabilities'])}")
    for key in (
        "added",
        "missing",
        "package_changed",
        "local_changed",
        "conflict",
        "retired",
    ):
        for path in result.get(key, []):
            print(f"  {key}: {path}")


PVMALOVE_CAPABILITY = "pvmalove-suite"
PROJECT_TEMPLATE_DIR = PACKAGE / "project"
DOCS_TASKS_GITIGNORE_LINE = "/docs/tasks/"
HARNESS_RUNTIME_GITIGNORE = """# Generated harness runtime data
.sandboxes/
"""
RUNTIME_GITIGNORE_LINES = (
    DOCS_TASKS_GITIGNORE_LINE,
    "/.harness/.sandboxes/",
)


def missing_runtime_gitignore_lines(content: str) -> list[str]:
    """Определить строки игнорирования рантайма, отсутствующие в файле .gitignore."""
    existing = set(content.splitlines())
    harness_ignored = ".harness/" in existing or "/.harness/" in existing
    return [
        line
        for line in RUNTIME_GITIGNORE_LINES
        if line not in existing
        and not (harness_ignored and line.startswith("/.harness/"))
    ]


def _prompt(label: str, default: str) -> str:
    """Запросить строковое значение у пользователя с дефолтным вариантом."""
    if not sys.stdin.isatty():
        return default
    try:
        answer = input(f"{label} [{default}]: ").strip()
    except EOFError:
        return default
    return answer or default


def _copy_if_absent(
    source: Path,
    target: Path,
    *,
    executable: bool = False,
    force: bool = False,
    differing: list[Path] | None = None,
) -> str | None:
    """Скопировать файл, если целевой путь отсутствует, или принудительно при force=True."""
    if target.exists():
        if not force:
            if differing is not None and target.read_bytes() != source.read_bytes():
                differing.append(target)
            return None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    if executable:
        target.chmod(target.stat().st_mode | 0o111)
    return str(target)


def scaffold_pvmalove_extras(
    repo: Path, args: argparse.Namespace, *, orchestration_enabled: bool = False
) -> list[str]:
    """Развернуть дополнительные файлы и настройки для набора возможностей pvmalove-suite."""
    written: list[str] = []
    differing: list[Path] = []
    force_seed = getattr(args, "force_seed_files", False) or getattr(
        args, "force", False
    )

    for doc in sorted((PROJECT_TEMPLATE_DIR / "docs-agents").glob("*.md")):
        result = _copy_if_absent(
            doc, repo / "docs/agents" / doc.name, force=force_seed, differing=differing
        )
        if result:
            written.append(result)

    for hook in sorted((PROJECT_TEMPLATE_DIR / "hooks").iterdir()):
        if not hook.is_file() or hook.suffix not in {".sh", ".py"}:
            continue
        result = _copy_if_absent(
            hook,
            repo / ".claude/hooks" / hook.name,
            executable=True,
            force=force_seed,
            differing=differing,
        )
        if result:
            written.append(result)

    for rule in sorted((PROJECT_TEMPLATE_DIR / "rules").glob("*.md")):
        result = _copy_if_absent(
            rule,
            repo / ".claude/rules" / rule.name,
            force=force_seed,
            differing=differing,
        )
        if result:
            written.append(result)

    for agent in sorted((PROJECT_TEMPLATE_DIR / "agents").glob("*.md")):
        result = _copy_if_absent(
            agent,
            repo / ".claude/agents" / agent.name,
            force=force_seed,
            differing=differing,
        )
        if result:
            written.append(result)

    scratch_gitignore_result = _copy_if_absent(
        PROJECT_TEMPLATE_DIR / "scratch/.gitignore",
        storage_path(repo, "scratch", ".gitignore"),
        force=force_seed,
        differing=differing,
    )
    if scratch_gitignore_result:
        written.append(scratch_gitignore_result)

    harness_gitignore = repo / ".harness/.gitignore"
    if not harness_gitignore.exists() or force_seed:
        harness_gitignore.parent.mkdir(parents=True, exist_ok=True)
        harness_gitignore.write_text(
            HARNESS_RUNTIME_GITIGNORE, encoding="utf-8", newline="\n"
        )
        written.append(str(harness_gitignore))

    root_gitignore = repo / ".gitignore"
    root_gitignore_text = (
        root_gitignore.read_text(encoding="utf-8") if root_gitignore.exists() else ""
    )
    missing_ignore_lines = missing_runtime_gitignore_lines(root_gitignore_text)
    if missing_ignore_lines:
        if root_gitignore_text and not root_gitignore_text.endswith("\n"):
            root_gitignore_text += "\n"
        root_gitignore_text += "\n".join(missing_ignore_lines) + "\n"
        root_gitignore.write_text(root_gitignore_text, encoding="utf-8", newline="\n")
        written.append(str(root_gitignore))

    settings = repo / ".claude/settings.local.json"
    template_text = (PROJECT_TEMPLATE_DIR / "settings.local.json.tmpl").read_text(
        encoding="utf-8"
    )
    if settings.exists() and not force_seed:
        if settings.read_text(encoding="utf-8") != template_text:
            differing.append(settings)
    if not settings.exists() or force_seed:
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(template_text, encoding="utf-8", newline="\n")
        written.append(str(settings))

    record_integration(
        repo,
        identifier="pvmalove-suite-hooks",
        kind="hook",
        runtimes=["claude"],
        config=Path(".claude/settings.local.json"),
        secret_refs=[],
        verify=(
            "Open a fresh Claude Code session and confirm a guarded action "
            "(e.g. a direct commit to the base branch) is blocked by the wired hooks."
        ),
    )
    written.append(str(repo / INTEGRATIONS_REL))

    project_json = repo / ".harness/project.json"
    if not project_json.exists():
        language = getattr(args, "language", None) or _prompt("language (ru/en)", "ru")
        base_branch = getattr(args, "pr_base_branch", None) or _prompt(
            "Release/base branch", getattr(args, "base_branch", None) or "main"
        )
        branch_pattern = getattr(args, "branch_pattern", None) or _prompt(
            "branch_pattern (regex)", "^feature/issue-[0-9]+-.+"
        )
        commands = list(getattr(args, "qa_gate_command", None) or [])
        if not commands and sys.stdin.isatty():
            print(
                "qa_gate_commands (по одной команде на строку, пустая строка — конец):"
            )
            while True:
                line = _prompt("  command", "")
                if not line:
                    break
                commands.append(line)
        # The tracker field is filled from origin without a prompt or flag: a GitHub/GitLab tracker
        # the resolver derives completely is written, a local or default one is left out
        # (docs/adr/0010). The template stays valid JSON either way.
        origin_tracker = resolve_project_tracker(repo).from_origin
        tracker_field = (
            "\n  " + origin_tracker.snippet() + ","
            if origin_tracker.type != "local"
            and not tracker_field_problems(origin_tracker.field())
            else ""
        )
        template = (PROJECT_TEMPLATE_DIR / "project.json.tmpl").read_text(
            encoding="utf-8"
        )
        rendered = render(
            template,
            {
                "LANGUAGE": language,
                "PR_BASE_BRANCH": base_branch,
                "BRANCH_PATTERN": branch_pattern,
                "QA_GATE_COMMANDS": json.dumps(commands, ensure_ascii=False),
                "TRACKER_FIELD": tracker_field,
            },
        )
        project_json.parent.mkdir(parents=True, exist_ok=True)
        project_json.write_text(rendered, encoding="utf-8", newline="\n")
        written.append(str(project_json))

    schema_result = _copy_if_absent(
        PROJECT_TEMPLATE_DIR / "project.schema.json",
        repo / ".harness/project.schema.json",
        force=force_seed,
        differing=differing,
    )
    if schema_result:
        written.append(schema_result)

    if orchestration_enabled:
        # The project's own config starts as a copy of the managed example
        # (.harness/orchestration.example.json, refreshed by every init/adopt/update) and is
        # never overwritten afterwards unless --force-seed-files is given.
        orchestration_result = _copy_if_absent(
            PACKAGE / "orchestration.example.json",
            repo / ".harness/orchestration.json",
            force=force_seed,
            differing=differing,
        )
        if orchestration_result:
            written.append(orchestration_result)

    if differing:
        print("\n[WARNING] Следующие сидируемые файлы отличаются от шаблонов upstream:")
        for path in differing:
            print(f"  - {path.relative_to(repo)}")
        print(
            "Используйте --force-seed-files, чтобы перезаписать их (внимание: локальные изменения будут потеряны).\n"
        )

    return written


def cmd_init(args: argparse.Namespace) -> int:
    """Инициализировать harness в новом проекте."""
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    if load_lock(repo) is not None:
        fail(f"project harness already exists; use 'harness update {repo}'")

    selected = selected_capabilities(args.capability)
    values = {
        "PROJECT_NAME": repo.name,
        "PROJECT_SUMMARY": "{{PROJECT_SUMMARY}}",
        "PROJECT_TYPE": args.project_type,
        "STACK_SUMMARY": ", ".join(args.stack or ["N/A"]),
        "CAPABILITY_SUMMARY": ", ".join(selected),
        "STAGE": "{{STAGE}}",
        "TRACKER": "{{TRACKER}}",
        "BASE_BRANCH": args.base_branch,
        "CMD_SETUP": "{{CMD_SETUP}}",
        "CMD_RUN": "{{CMD_RUN}}",
        "CMD_TEST": "{{CMD_TEST}}",
        "CMD_LINT": "{{CMD_LINT}}",
        "CMD_BUILD": "{{CMD_BUILD}}",
        "STRUCTURE": "{{STRUCTURE}}",
        "ALLOWED": "{{ALLOWED}}",
        "ASK_FIRST": "{{ASK_FIRST}}",
        "FORBIDDEN": "{{FORBIDDEN}}",
        "DOD": "{{DOD}}",
        "MERGE_POLICY": "{{MERGE_POLICY}}",
        "GOTCHAS": "{{GOTCHAS}}",
    }

    agents = repo / "AGENTS.md"
    if not agents.exists():
        template = (PACKAGE / "project/AGENTS.md.tmpl").read_text(encoding="utf-8")
        agents.write_text(render(template, values), encoding="utf-8", newline="\n")

    claude = repo / "CLAUDE.md"
    if not claude.exists():
        claude.write_text(CLAUDE_MD_SEED, encoding="utf-8", newline="\n")

    written = write_snapshot(repo, selected, force=False)
    written.extend(
        ensure_links(
            repo,
            fix_hint="remove it manually, or use 'harness adopt' instead of 'init' if this "
            "project already has its own skills there",
        )
    )
    if PVMALOVE_CAPABILITY in selected or BACKEND_ORCHESTRATION_CAPABILITY in selected:
        written.extend(
            scaffold_pvmalove_extras(
                repo,
                args,
                orchestration_enabled=BACKEND_ORCHESTRATION_CAPABILITY in selected,
            )
        )
    print(f"installed agent-harness {version()} in {repo}")
    print(f"capabilities: {', '.join(selected)}")
    print(f"managed files: {len(written)}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Сравнить текущее состояние проекта с эталонным снимком и вывести различия."""
    repo = Path(args.repo).expanduser().resolve()
    result = snapshot_diff(repo, args.capability)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_diff(result)
    return 0 if result["state"] == "clean" else 1


def cmd_adopt(args: argparse.Namespace) -> int:
    """Внедрить harness в существующий проект с сохранением собственных скиллов."""
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    selected = selected_capabilities(args.capability)
    selected_names = [source.name for source in selected_skills(selected)]
    occupied = [repo / ".harness/skills" / name for name in selected_names]
    occupied = [path for path in occupied if path.exists() or path.is_symlink()]
    if occupied and not args.replace_conflicts:
        for path in occupied:
            print(f"conflict: {path}", file=sys.stderr)
        fail(
            "selected skill names already exist; inspect them or use --replace-conflicts"
        )
    for path in occupied:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)

    written = write_snapshot(repo, selected, force=False)
    ensure_links(
        repo, replace=args.replace_conflicts, fix_hint="re-run with --replace-conflicts"
    )
    if PVMALOVE_CAPABILITY in selected or BACKEND_ORCHESTRATION_CAPABILITY in selected:
        written.extend(
            scaffold_pvmalove_extras(
                repo,
                args,
                orchestration_enabled=BACKEND_ORCHESTRATION_CAPABILITY in selected,
            )
        )
    print(f"adopted agent-harness {version()} in {repo}")
    print(f"preserved project-owned skill names outside: {', '.join(selected_names)}")
    print(f"managed files: {len(written)}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """Обновить управляемые файлы harness в целевом проекте."""
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    lock = load_lock(repo)
    if lock is None:
        fail(f"project harness is missing; use 'harness init {repo}'")
    selected = selected_capabilities(args.capability or lock.get("capabilities"))
    result = snapshot_diff(repo, selected)
    blocked = result.get("local_changed", []) + result.get("conflict", [])
    force_managed = args.force or args.force_managed_files
    if blocked and not force_managed:
        print_diff(result)
        print("\n[ВНИМАНИЕ] Обнаружены изменённые стандартные скиллы (дрейф):")
        for b in blocked:
            print(f"  - {b}")
        print("\nЧтобы просмотреть, что именно изменилось:")
        print(f"  git diff {' '.join(blocked)}")
        print("\nВыберите действие:")
        print("  ЗАМЕНИТЬ (сбросить ваши правки до версии из апстрима):")
        print("    harness update --force-managed-files")
        print("    (или --force, если также нужно перезаписать seed-файлы)")
        print("  ОСТАВИТЬ (сохранить ваши правки):")
        print("    (обновление прервано, ваши файлы не тронуты)")
        fail("update aborted due to local skill changes (drift).")

    previous = set((lock.get("files") or {}).keys())
    current = set(package_files(selected).keys())
    for relative in sorted(previous - current):
        path = repo / relative
        if path.is_file():
            path.unlink()

    written = write_snapshot(repo, selected, force=force_managed)
    ensure_links(
        repo,
        replace=force_managed,
        fix_hint="re-run with --force or --force-managed-files",
    )
    if PVMALOVE_CAPABILITY in selected or BACKEND_ORCHESTRATION_CAPABILITY in selected:
        written.extend(
            scaffold_pvmalove_extras(
                repo,
                args,
                orchestration_enabled=BACKEND_ORCHESTRATION_CAPABILITY in selected,
            )
        )
    print(f"updated agent-harness to {version()} in {repo}")
    print(f"managed files: {len(written)}")
    return 0


def cmd_registry(args: argparse.Namespace) -> int:
    """Перегенерировать файл REGISTRY.md для целевого репозитория."""
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    write_registry(repo)
    print(f"wrote {REGISTRY_REL} for {repo}")
    return 0


def cmd_lock_project_skills(args: argparse.Namespace) -> int:
    """Зафиксировать хэши файлов локальных проектных скиллов в overlay-lock файле."""
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    lock = load_lock(repo)
    if lock is None:
        fail("project harness is missing; use init or adopt")
    claimed: set[str] = set()
    overlay_root = repo / ".harness/overlays"
    if overlay_root.is_dir():
        for path in sorted(overlay_root.iterdir()):
            if not path.is_file() or not path.name.endswith((".lock", ".lock.json")):
                continue
            if path.name == "project-local.lock":
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                fail(f"cannot read existing overlay lock {path}: {exc}")
            for entry in payload.get("skills", []):
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    claimed.add(entry["name"])
    selected = []
    for name, directory, _ in skill_inventory(repo):
        if name in public_skill_names(lock) or name in claimed:
            continue
        selected.append(
            {
                "name": name,
                "source": f".harness/skills/{name}",
                "files": skill_file_hashes(repo, directory),
            }
        )
    payload = {
        "schema": 1,
        "overlay_id": "project-local",
        "source": {"type": "project-local"},
        "bundles": ["project-local"],
        "skills": selected,
    }
    target = overlay_root / "project-local.lock"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    write_registry(repo)
    print(f"locked {len(selected)} project-local skills in {target.relative_to(repo)}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Выполнить все зарегистрированные проверки harness health и вывести отчёт."""
    repo = Path(args.repo).expanduser().resolve()
    report = health_registry.run(
        repo,
        snapshot_diff=snapshot_diff,
        output_encoding=ORIGINAL_STDOUT_ENCODING,
        fix=getattr(args, "fix", False),
        online=getattr(args, "online", False),
        harness_cli=HARNESS_CLI,
    )
    if args.json:
        print(json.dumps(health_report_json.to_json(report), ensure_ascii=False))
    else:
        print(health_render.render_text(report), end="")
    return 1 if report.summary()["fail"] else 0


def cmd_console(args: argparse.Namespace) -> int:
    """Запустить интерактивную консоль TUI для диагностики harness."""
    from harness.console.launcher import run_console

    repo = Path(args.repo).expanduser().resolve()
    return run_console(repo)


def cmd_list(args: argparse.Namespace) -> int:
    """Вывести отсортированный список имён всех доступных в проекте скиллов."""
    repo = Path(args.repo).expanduser().resolve()
    for name in sorted(
        path.parent.name for path in (repo / ".harness/skills").rglob("SKILL.md")
    ):
        print(name)
    return 0


def _add_pvmalove_args(sub: argparse.ArgumentParser) -> None:
    """Добавить аргументы командной строки, специфичные для набора возможностей pvmalove-suite."""
    sub.add_argument(
        "--language",
        choices=["ru", "en"],
        default=None,
        help="pvmalove-suite: .harness/project.json language",
    )
    sub.add_argument(
        "--pr-base-branch",
        default=None,
        help="pvmalove-suite: release/base branch for epic integration branches (default: --base-branch)",
    )
    sub.add_argument(
        "--branch-pattern", default=None, help="pvmalove-suite: branch name regex"
    )
    sub.add_argument(
        "--qa-gate-command",
        action="append",
        default=None,
        help="pvmalove-suite: repeatable, in run order",
    )


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Выполнить предварительный просмотр или удаление временных данных harness."""
    if args.apply and args.mode == "hard" and args.confirm != "HARD":
        fail("hard cleanup requires --confirm HARD")
    try:
        plan = plan_cleanup(
            Path(args.repo), args.mode, min_age_hours=args.min_age_hours
        )
        print(json.dumps({"plan": plan}, ensure_ascii=False, indent=2))
        if args.apply:
            result = apply_cleanup(Path(args.repo), plan, confirm=args.confirm)
            print(json.dumps({"result": result}, ensure_ascii=False, indent=2))
            return 1 if result["failed"] else 0
        return 0
    except ValueError as exc:
        fail(str(exc))


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Показать план полного удаления харнесса или применить его с подтверждением."""
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    if args.apply and args.confirm != CONFIRM_WORD:
        fail(f"uninstall requires --confirm {CONFIRM_WORD}")
    plan = plan_uninstall(repo)
    print(json.dumps({"plan": plan}, ensure_ascii=False, indent=2))
    if not args.apply:
        return 1 if plan["blocked"] else 0
    try:
        result = apply_uninstall(repo, plan, confirm=args.confirm)
    except ValueError as exc:
        fail(str(exc))
    print(json.dumps({"result": result}, ensure_ascii=False, indent=2))
    return 1 if result["failed"] else 0


def parser() -> argparse.ArgumentParser:
    """Сконфигурировать парсер аргументов командной строки CLI harness."""
    root = argparse.ArgumentParser(prog="harness")
    commands = root.add_subparsers(dest="command", required=True)

    cleanup = commands.add_parser(
        "cleanup", help="preview or remove disposable .harness data"
    )
    cleanup.add_argument("repo")
    cleanup.add_argument("--mode", choices=("soft", "hard"), default="soft")
    cleanup.add_argument("--min-age-hours", type=float, default=24)
    cleanup.add_argument(
        "--apply", action="store_true", help="apply the printed cleanup plan"
    )
    cleanup.add_argument("--confirm", help="pass HARD when applying hard cleanup")
    cleanup.set_defaults(func=cmd_cleanup)

    uninstall = commands.add_parser(
        "uninstall", help="preview or remove everything the harness installed"
    )
    uninstall.add_argument("repo")
    uninstall.add_argument(
        "--apply", action="store_true", help="apply the printed uninstall plan"
    )
    uninstall.add_argument(
        "--confirm", help=f"pass {CONFIRM_WORD} when applying the uninstall"
    )
    uninstall.set_defaults(func=cmd_uninstall)

    init = commands.add_parser("init")
    init.add_argument("repo")
    init.add_argument(
        "--project-type", default="unspecified", help="informational project domain"
    )
    init.add_argument("--stack", action="append", help="informational project stack")
    init.add_argument("--capability", action="append")
    init.add_argument("--base-branch", default="main")
    _add_pvmalove_args(init)
    init.set_defaults(func=cmd_init)

    diff = commands.add_parser("diff")
    diff.add_argument("repo")
    diff.add_argument("--capability", action="append")
    diff.add_argument("--json", action="store_true")
    diff.set_defaults(func=cmd_diff)

    adopt = commands.add_parser("adopt")
    adopt.add_argument("repo")
    adopt.add_argument("--capability", action="append")
    adopt.add_argument("--replace-conflicts", action="store_true")
    _add_pvmalove_args(adopt)
    adopt.set_defaults(func=cmd_adopt)

    update = commands.add_parser("update")
    update.add_argument("repo")
    update.add_argument("--capability", action="append")
    update.add_argument("--force", action="store_true")
    update.add_argument(
        "--force-managed-files",
        action="store_true",
        help="overwrite only managed snapshot files; preserve project-owned seed files",
    )
    update.add_argument(
        "--force-seed-files",
        action="store_true",
        help="overwrite seed files even if present",
    )
    _add_pvmalove_args(update)
    update.set_defaults(func=cmd_update)

    registry = commands.add_parser("registry")
    registry.add_argument("repo")
    registry.set_defaults(func=cmd_registry)

    lock_project = commands.add_parser("lock-project-skills")
    lock_project.add_argument("repo")
    lock_project.set_defaults(func=cmd_lock_project_skills)

    health = commands.add_parser("health")
    health.add_argument("repo")
    health.add_argument(
        "--json", action="store_true", help="print the schema_version-1 --json report"
    )
    health.add_argument(
        "--fix",
        action="store_true",
        help="create missing .harness directories and regenerate a stale skill registry; "
        "system settings and worktrees are never changed",
    )
    health.add_argument(
        "--online",
        action="store_true",
        help="also run the tracker group: gh/glab auth, permissions, reachability and label "
        "checks against GitHub/GitLab (10s timeout each); without it every tracker.* check is "
        "skipped",
    )
    health.set_defaults(func=cmd_health)

    list_cmd = commands.add_parser("list")
    list_cmd.add_argument("repo")
    list_cmd.set_defaults(func=cmd_list)

    console = commands.add_parser(
        "console",
        help="interactive TUI diagnostics console (needs uv + network for textual)",
    )
    console.add_argument("repo")
    console.set_defaults(func=cmd_console)
    return root


def main() -> int:
    """Основная точка входа CLI harness."""
    args = parser().parse_args()
    try:
        exit_code: int = args.func(args)
        return exit_code
    except HarnessError as exc:
        return print_and_exit(exc)


if __name__ == "__main__":
    raise SystemExit(main())
