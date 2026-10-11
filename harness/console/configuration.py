"""Editable configuration drafts; no TUI dependency and no implicit writes."""

from __future__ import annotations

import copy
import difflib
import json
from dataclasses import dataclass
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
