"""Editable configuration drafts; no TUI dependency and no implicit writes."""

from __future__ import annotations

import copy
import difflib
import json
from dataclasses import dataclass
from harness.runtime_adapters import AdapterStatus
from pathlib import Path

from harness.configuration import parse_object, read_config_bundle, reject_secrets
from harness.configuration_schema import PACKAGE
from harness.configuration_store import json_bytes
from harness.health.project_files import validate_project_data
from harness.json_types import JsonObject

CONFIG_NAMES = ("project.json", "orchestration.json")


def default_document(repo: Path, name: str) -> JsonObject:
    if name == "orchestration.json":
        path = repo / ".harness/orchestration.example.json"
        if not path.is_file():
            path = PACKAGE / "orchestration.example.json"
        return parse_object(path.read_bytes(), name)
    from harness.bin.harness import render
    from harness.health.project_tracker import resolve_project_tracker

    tracker = resolve_project_tracker(repo).effective
    field: JsonObject = {"type": tracker.type}
    if tracker.type != "local":
        field.update(host=tracker.host, project=tracker.project)
    template = (PACKAGE / "project/project.json.tmpl").read_text(encoding="utf-8")
    rendered = render(
        template,
        {
            "LANGUAGE": "ru",
            "PR_BASE_BRANCH": "main",
            "BRANCH_PATTERN": "^feature/issue-[0-9]+-.+",
            "QA_GATE_COMMANDS": "[]",
            "TRACKER_FIELD": '\n  "tracker": ' + json.dumps(field) + ",",
            # Like `harness init` without --test-path and --test-runner-command: no test fields.
            "TEST_FIELDS": "",
        },
    )
    return parse_object(rendered.encode("utf-8"), name)


_MISSING = object()


def _lookup(document: JsonObject, path: tuple[str, ...]) -> object:
    target: object = document
    for key in path:
        if not isinstance(target, dict) or key not in target:
            return _MISSING
        target = target[key]
    return target


def _changed_paths(
    old: object, new: object, prefix: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Leaf paths whose value differs; objects are compared key by key."""
    if isinstance(old, dict) and isinstance(new, dict):
        paths: list[tuple[str, ...]] = []
        for key in dict.fromkeys([*old, *new]):
            if key not in old or key not in new:
                paths.append((*prefix, key))
            else:
                paths.extend(_changed_paths(old[key], new[key], (*prefix, key)))
        return paths
    return [] if old == new else [prefix]


def bundle_diff(
    before: dict[str, bytes | None],
    after: dict[str, bytes | None],
    old_label: str,
    new_label: str,
) -> str:
    """Unified diff of two configuration sets; a deleted file is announced explicitly."""
    lines: list[str] = []
    for name, raw in after.items():
        old = (before[name] or b"").decode("utf-8").splitlines(keepends=True)
        new = (raw or b"").decode("utf-8").splitlines(keepends=True)
        lines.extend(
            difflib.unified_diff(
                old, new, fromfile=f"{old_label}/{name}", tofile=f"{new_label}/{name}"
            )
        )
        if raw is None and before[name] is not None:
            lines.append(f"Удалить {name}\n")
    return "".join(lines)


@dataclass
class ConfigurationDraft:
    repo: Path
    documents: dict[str, JsonObject]
    original: dict[str, bytes | None]

    @classmethod
    def load(
        cls, repo: Path, names: tuple[str, ...] = CONFIG_NAMES
    ) -> ConfigurationDraft:
        repo = repo.expanduser().resolve()
        original: dict[str, bytes | None] = {}
        documents: dict[str, JsonObject] = {}
        bundle = read_config_bundle(repo)
        for name in names:
            if name not in CONFIG_NAMES:
                raise ValueError("Неизвестный файл конфигурации")
            if (
                name == "orchestration.json"
                and not (repo / ".harness/orchestration/roles").is_dir()
            ):
                raise ValueError("Сначала установите capability backend-orchestration")
            raw = bundle[name]
            original[name] = raw
            documents[name] = (
                parse_object(raw, name)
                if raw is not None
                else default_document(repo, name)
            )
        return cls(repo, documents, original)

    def set_value(self, name: str, path: tuple[str, ...], value: object) -> None:
        target = self.documents[name]
        for key in path[:-1]:
            if key not in target:
                target[key] = {}
            if not isinstance(target[key], dict):
                raise ValueError("Родительское поле должно быть объектом")
            target = target[key]
        target[path[-1]] = copy.deepcopy(value)

    def remove_value(self, name: str, path: tuple[str, ...]) -> None:
        target: object = self.documents[name]
        for key in path[:-1]:
            if not isinstance(target, dict) or key not in target:
                return
            target = target[key]
        if isinstance(target, dict):
            target.pop(path[-1], None)

    def rebase(self, current: dict[str, bytes | None]) -> list[str]:
        """Adopt externally changed files and replay this draft's own edits onto them.

        Returns the fields changed both by the draft and from outside; the draft wins there.
        """
        conflicts: list[str] = []
        for name, mine in list(self.documents.items()):
            base, theirs = self.original[name], current[name]
            old = (
                parse_object(base, name)
                if base is not None
                else default_document(self.repo, name)
            )
            self.documents[name] = (
                parse_object(theirs, name)
                if theirs is not None
                else default_document(self.repo, name)
            )
            for path in _changed_paths(old, mine):
                value = _lookup(mine, path)
                label = name + ": " + ".".join(path)
                if _lookup(old, path) != _lookup(self.documents[name], path) != value:
                    conflicts.append(label)
                try:
                    if value is _MISSING:
                        self.remove_value(name, path)
                    else:
                        self.set_value(name, path, value)
                except ValueError:
                    conflicts.append(label)
        self.original = dict(current)
        return conflicts

    def changed(self) -> bool:
        return any(self.original[name] != raw for name, raw in self.encoded().items())

    def problems(self) -> list[str]:
        problems: list[str] = []
        try:
            reject_secrets(self.documents)
        except ValueError as exc:
            return [str(exc)]
        for name, document in self.documents.items():
            from harness.configuration_schema import schema_problems

            try:
                problems.extend(schema_problems(self.repo, name, document))
            except ValueError as exc:
                problems.append(str(exc))
            if name == "project.json":
                validate_project_data(document, problems)
            else:
                from harness.orchestration.contract import configuration_problems

                problems.extend(
                    configuration_problems(
                        document, self.repo / ".harness/orchestration/roles"
                    )
                )
        return problems

    def encoded(self) -> dict[str, bytes | None]:
        return {name: json_bytes(data) for name, data in self.documents.items()}

    def diff(self) -> str:
        """Unified diff of the saved files against this draft; empty when unchanged."""
        return bundle_diff(self.original, self.encoded(), "текущий", "новый")


# The words of an update plan, one per `harness diff --json` field: a symbol and a word, never a
# key. The Update Harness screen tags its file list with the same words.
SNAPSHOT_TAGS = {
    "package_changed": "✔ обновится",
    "added": "✔ добавится",
    "missing": "⚠ восстановится",
    "local_changed": "⚠ ваши правки",
    "conflict": "✖ конфликт",
    "retired": "⚠ удалится",
}
_NO_CAPABILITY = "⚠ Capability backend-orchestration не выбрана"
_BLOCKED_BY_FILES = "⚠ Сначала устраните локальные изменения и коллизии"


@dataclass(frozen=True)
class RuntimeAdapterPreview:
    adapters: list["AdapterStatus"]
    capabilities: list[str]
    fingerprint: str
    text: str
    blocked: bool
    # The blocking line of the plan (the reason `Подтвердить` is unavailable); empty when not blocked.
    reason: str = ""


def preview_runtime_adapters(repo: Path) -> RuntimeAdapterPreview:
    """Preview adapter installation through update without changing capability selection."""
    import hashlib
    from harness.bin.harness import (
        load_lock,
        package_files,
        snapshot_diff,
        seed_differing,
        seed_template_files,
    )
    from harness.health.project_files import file_digest
    from harness.runtime_adapters import adapter_statuses

    try:
        lock = load_lock(repo)
    except SystemExit as exc:
        raise ValueError("Не удалось прочитать snapshot харнесса") from exc
    if lock is None:
        raise ValueError("Сначала установите харнесс проекта")
    selected = list(lock.get("capabilities") or [])
    enabled = "backend-orchestration" in selected
    sources = package_files(selected if enabled else ["backend-orchestration"])
    previous = lock.get("files") or {}
    adapters = adapter_statuses(repo, sources, previous)
    snapshot = snapshot_diff(repo, selected)
    fingerprints = {
        path: "symlink" if (repo / path).is_symlink() else file_digest(repo / path)
        for path in set(sources)
        | set(previous)
        | set(seed_template_files(repo, orchestration_enabled=True))
        | {".harness/harness.lock"}
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "disk": fingerprints,
                "source": {
                    path: hashlib.sha256(data).hexdigest()
                    for path, data in sources.items()
                },
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    lines = [
        "Возможности: " + ", ".join(selected),
        "Обновятся управляемые файлы харнесса:",
    ]
    for key in (
        "added",
        "missing",
        "package_changed",
        "local_changed",
        "conflict",
        "retired",
    ):
        lines.extend(f"{SNAPSHOT_TAGS[key]}  {path}" for path in snapshot.get(key, []))
    differing = seed_differing(repo)
    if differing:
        lines.append("Seed-файлы сохраняются: " + ", ".join(differing))
    blocked = not enabled or bool(
        snapshot.get("local_changed") or snapshot.get("conflict")
    )
    if not enabled:
        import shlex

        lines.append(_NO_CAPABILITY + ". Выберите состав явно в CLI:")
        lines.append(
            shlex.join(
                [
                    "harness",
                    "update",
                    str(repo),
                    "--capability",
                    "backend-orchestration",
                ]
            )
        )
    if enabled and blocked:
        lines.append(_BLOCKED_BY_FILES + ". Принудительное обновление не включено.")
    reason = "" if not blocked else _NO_CAPABILITY if not enabled else _BLOCKED_BY_FILES
    return RuntimeAdapterPreview(
        adapters, selected, fingerprint, "\n".join(lines), blocked, reason
    )


def apply_runtime_adapters(
    repo: Path, preview: RuntimeAdapterPreview, *, confirmed: bool = False
) -> str:
    """Apply only a confirmed fresh preview through the existing CLI update implementation."""
    import subprocess
    from harness.bin.harness import HARNESS_CLI

    if not confirmed:
        raise ValueError("Требуется явное подтверждение просмотра")
    current = preview_runtime_adapters(repo)
    if current.fingerprint != preview.fingerprint:
        raise ValueError("План изменился: выполните просмотр заново")
    if current.blocked:
        raise ValueError("Сначала устраните локальные изменения и коллизии")
    command = [*HARNESS_CLI, "update", str(repo)]
    for capability in preview.capabilities:
        command.extend(["--capability", capability])
    result = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=300
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()
