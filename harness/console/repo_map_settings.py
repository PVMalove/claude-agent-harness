"""Repo Map settings behind the console: tier, project languages and project memory.

Plain data and file operations, no Textual: the screen shows what these return and runs the CLI
commands of `catalog` for anything that builds or downloads. Configuration is written the same way
as by the setup wizard: a validated draft applied through the configuration store, which keeps the
previous files as a version."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from harness.configuration import read_config_bundle
from harness.configuration_store import ConfigurationStore
from harness.memory.index import context as memory_context
from harness.repo_map.bundle_fetch import ParserStatus
from harness.repo_map.bundle_pins import LANGUAGES
from .configuration import ConfigurationDraft
from .repo_map import RepoMapView

TIERS = ("minimal", "full")
GIT_TIMEOUT_SECONDS = 20

# Memory starts with the documents that most often hold decisions; the wizard widens it.
DEFAULT_SOURCE_TYPES: tuple[str, ...] = ("adr", "glossary")
DEFAULT_MEMORY_POLICY: dict[str, object] = {
    "source_types": list(DEFAULT_SOURCE_TYPES),
    "allow_paths": ["docs/adr/**/*.md", "CONTEXT.md", "CONTEXT-MAP.md"],
    "redact_rules": [],
    "min_similarity": 0.0,
    "top_k": 5,
    "max_tokens": 1000,
}


@dataclass(frozen=True)
class TierSetting:
    """The tier the project's policy asks for, and whether the console can change it."""

    tier: str
    editable: bool
    reason: str = ""


def tier_setting(repo: Path) -> TierSetting:
    """`repo_map_policy.tier` of orchestration.json; `full` when the policy does not say."""
    if not (repo / ".harness/orchestration/roles").is_dir():
        return TierSetting(
            "full",
            False,
            "Режим хранится в orchestration.json: нужна capability backend-orchestration.",
        )
    raw = read_config_bundle(repo).get("orchestration.json")
    try:
        document = json.loads(raw) if raw else {}
    except ValueError:
        return TierSetting("full", False, "orchestration.json не читается.")
    policy = document.get("repo_map_policy") if isinstance(document, dict) else None
    tier = policy.get("tier") if isinstance(policy, dict) else None
    return TierSetting(tier if tier in TIERS else "full", True)


def apply_changes(repo: Path, name: str, changes: dict[tuple[str, ...], object]) -> str:
    """Write `changes` into one configuration file through a validated draft; return the backup id."""
    draft = ConfigurationDraft.load(repo, (name,))
    for path, value in changes.items():
        draft.set_value(name, path, value)
    problems = draft.problems()
    if problems:
        raise ValueError(problems[0])
    backup = ConfigurationStore(repo).apply(draft.encoded(), expected=draft.original)
    return backup.id


def apply_tier(repo: Path, tier: str) -> str:
    """Set `repo_map_policy.tier`; return the id of the version that keeps the previous files."""
    if tier not in TIERS:
        raise ValueError("Режим карты: minimal или full")
    return apply_changes(
        repo, "orchestration.json", {("repo_map_policy", "tier"): tier}
    )


@dataclass(frozen=True)
class MemoryStatus:
    """Whether project memory is on, what it reads, and how much of it is indexed."""

    enabled: bool
    active: bool
    source_types: tuple[str, ...]
    indexed: int | None
    problem: str = ""
    inactive: str = ""  # why memory is on but reads nothing, e.g. no allow_paths
    # The search limits of the memory policy: hits per query and tokens per answer.
    top_k: int | None = None
    max_tokens: int | None = None


def memory_status(repo: Path) -> MemoryStatus:
    try:
        _canonical, index, policy = memory_context(repo)
    except ValueError as error:
        return MemoryStatus(False, False, (), None, str(error))
    indexed: int | None = None
    if index.is_file():
        try:
            connection = sqlite3.connect(f"file:{index}?mode=ro", uri=True)
            try:
                indexed = int(
                    connection.execute("SELECT count(*) FROM documents").fetchone()[0]
                )
            finally:
                connection.close()
        except sqlite3.Error:
            indexed = None
    inactive = ""
    if policy.enabled and not policy.active:
        inactive = (
            "не заданы allow_paths"
            if not policy.allow_paths
            else "не заданы source_types"
        )
    return MemoryStatus(
        policy.enabled,
        policy.active,
        policy.source_types,
        indexed,
        "",
        inactive,
        policy.top_k,
        policy.max_tokens,
    )


# Where each kind of memory source lives, as allow_paths patterns; only existing ones are used.
_SOURCE_PATHS: dict[str, tuple[str, ...]] = {
    "adr": ("docs/adr/**/*.md",),
    "glossary": ("CONTEXT.md", "CONTEXT-MAP.md"),
    "task_archive": ("docs/tasks/**/*.md",),
    "ledger": (".harness/orchestration/state/generations/**/*.json",),
    "qa_finding": (".harness/orchestration/state/generations/**/*.json",),
    "completion_report": (
        ".harness/orchestration/state/generations/**/*.json",
        ".harness/.sandboxes/memory/snapshot/records/*.json",
    ),
}


def default_allow_paths(repo: Path, source_types: tuple[str, ...]) -> list[str]:
    """allow_paths for `source_types` that point at something the project actually has."""
    found: list[str] = []
    for kind in source_types:
        for pattern in _SOURCE_PATHS.get(kind, ()):
            prefix = pattern.split("*")[0].rstrip("/")
            exists = (repo / pattern).is_file() or (repo / prefix).exists()
            if exists and pattern not in found:
                found.append(pattern)
    return found


def plan_memory_enable(repo: Path) -> dict[tuple[str, ...], object]:
    """The configuration changes that make memory read something; empty when it already does."""
    document = ConfigurationDraft.load(repo, ("project.json",)).documents[
        "project.json"
    ]
    changes: dict[tuple[str, ...], object] = {}
    memory = document.get("memory")
    if not (isinstance(memory, dict) and memory.get("enabled") is True):
        changes[("memory", "enabled")] = True
    policy = document.get("memory_policy")
    if not isinstance(policy, dict):
        types = DEFAULT_SOURCE_TYPES
        changes[("memory_policy",)] = {
            **DEFAULT_MEMORY_POLICY,
            "allow_paths": default_allow_paths(repo, types)
            or DEFAULT_MEMORY_POLICY["allow_paths"],
        }
        return changes
    types = tuple(str(t) for t in policy.get("source_types", ()) or ())
    if not types:
        types = DEFAULT_SOURCE_TYPES
        changes[("memory_policy", "source_types")] = list(types)
    if not policy.get("allow_paths"):
        paths = default_allow_paths(repo, types)
        if not paths:
            raise ValueError(
                "В проекте нет документов для памяти (docs/adr, CONTEXT.md, docs/tasks): "
                "задайте allow_paths в «Настроить память»."
            )
        changes[("memory_policy", "allow_paths")] = paths
    return changes


def enable_memory(repo: Path) -> str:
    """Apply `plan_memory_enable`; return the id of the version keeping the previous files ("" if none)."""
    changes = plan_memory_enable(repo)
    return apply_changes(repo, "project.json", changes) if changes else ""


def language_files(repo: Path) -> dict[str, int]:
    """How many tracked files each supported language has (by extension); empty on any git error."""
    from harness.repo_map.bundle_pins import GRAMMARS

    by_extension = {
        extension: language.key
        for language in LANGUAGES
        for grammar in GRAMMARS
        if grammar.name in language.grammars
        for extension in grammar.extensions
    }
    try:
        listing = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-z"],
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return {}
    counts: dict[str, int] = {}
    for path in listing.split("\0"):
        key = by_extension.get(Path(path).suffix.lower())
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


@dataclass(frozen=True)
class Check:
    """One line of the Repo Map check: `level` is ok, warn, fail or skipped."""

    level: str
    text: str


def repo_map_checks(
    *,
    head: str | None,
    tier: TierSetting,
    parsers: ParserStatus | None,
    files: dict[str, int],
    memory: MemoryStatus,
    view: RepoMapView | None,
    t: Callable[[str], str] = lambda text: text,
) -> list[Check]:
    """What stands between the project and a full Repo Map, each with the step that fixes it.

    Pure over what the screen already read; `t` translates the fixed phrases."""
    checks: list[Check] = []
    if head is None:
        checks.append(
            Check(
                "fail",
                t("HEAD не найден: это не Git-репозиторий или в нём нет коммитов.")
                + " "
                + t("Сделайте первый коммит."),
            )
        )
    else:
        checks.append(Check("ok", f"HEAD {head[:12]}"))
    checks.append(Check("ok", t("Режим в политике проекта: ") + tier.tier))
    full = tier.tier == "full"
    if not full:
        checks.append(
            Check(
                "skipped",
                t("Режим minimal: парсер не нужен, карта хранит только пути."),
            )
        )
    elif parsers is None:
        checks.append(Check("warn", t("Состояние парсеров не прочитано.")))
    elif parsers.ready:
        checks.append(Check("ok", t("Bundle парсеров готов: ") + str(parsers.pair)))
    else:
        checks.append(
            Check(
                "fail",
                t("Bundle парсеров не готов: ")
                + str(parsers.problem)
                + ". "
                + t("Вкладка «Настройки» → «Парсеры языков» → «Установить выбранные»."),
            )
        )
    if full and parsers is not None and parsers.ready:
        for language in LANGUAGES:
            count = files.get(language.key, 0)
            if not count:
                continue
            if language.key in parsers.languages:
                checks.append(
                    Check(
                        "ok",
                        f"{language.label}: "
                        + t("парсер установлен, файлов: ")
                        + str(count),
                    )
                )
            else:
                checks.append(
                    Check(
                        "warn",
                        f"{language.label}: "
                        + t(
                            "парсер не установлен, файлы попадут в карту без сигнатур; файлов: "
                        )
                        + str(count),
                    )
                )
    if view is None:
        checks.append(
            Check(
                "skipped",
                t("Карта для HEAD не построена: нажмите «Собрать карту» (b)."),
            )
        )
    else:
        if full and view.tier != "full":
            checks.append(
                Check(
                    "warn",
                    t("Построенная карта в режиме minimal: ")
                    + view.degradation_reason
                    + ". "
                    + t("Установите парсер и соберите карту заново."),
                )
            )
        else:
            checks.append(Check("ok", t("Построенная карта: ") + view.tier))
        if view.diagnostics:
            checks.append(
                Check(
                    "warn",
                    t("Диагностик в карте: ")
                    + str(len(view.diagnostics))
                    + ". "
                    + t("Список ниже."),
                )
            )
    if memory.problem:
        checks.append(Check("warn", t("Память недоступна: ") + memory.problem))
    elif not memory.active:
        checks.append(
            Check(
                "skipped",
                t("Память проекта выключена: «Настройки» → «Память проекта»."),
            )
        )
    elif memory.indexed is None:
        checks.append(
            Check(
                "warn",
                t("Память включена, индекс не построен: нажмите «Инициализировать»."),
            )
        )
    else:
        checks.append(
            Check("ok", t("Память: индекс, документов: ") + str(memory.indexed))
        )
    return checks
