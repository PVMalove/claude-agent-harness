"""Editable configuration drafts; no TUI dependency and no implicit writes."""

from __future__ import annotations

import copy
import difflib
import json
from dataclasses import dataclass
from pathlib import Path

from harness.configuration import parse_config_json, read_config_bundle, reject_secrets
from harness.health.project_files import validate_project_data
from harness.json_types import JsonObject

PACKAGE = Path(__file__).resolve().parent.parent
CONFIG_NAMES = ("project.json", "orchestration.json")


def json_bytes(value: JsonObject) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def parse_object(raw: bytes, label: str) -> JsonObject:
    try:
        data = parse_config_json(raw)
    except (ValueError, RecursionError):
        raise ValueError(f"{label}: требуется корректный JSON в UTF-8") from None
    if not isinstance(data, dict):
        raise ValueError(f"{label}: требуется JSON-объект")
    return data


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
        lines: list[str] = []
        for name, raw in self.encoded().items():
            before = (
                (self.original[name] or b"").decode("utf-8").splitlines(keepends=True)
            )
            after = (raw or b"").decode("utf-8").splitlines(keepends=True)
            lines.extend(
                difflib.unified_diff(
                    before, after, fromfile=f"текущий/{name}", tofile=f"новый/{name}"
                )
            )
        return "".join(lines) or "Изменений нет"
