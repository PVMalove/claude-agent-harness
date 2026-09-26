"""Граф Repo Map: записи файлов, рёбра из фактов парсера и ранжирование путей.

Факты worker превращаются в рёбра только здесь, после применения политики символов, поэтому правило
видимости задаётся в одном месте для всех языков.
"""

from __future__ import annotations

import posixpath
import sys
from collections import deque
from pathlib import Path
from typing import Literal, TypedDict

from harness.repo_map.bundle_worker import FileFacts, ImportFact, SignatureFact
from harness.repo_map.policy import RepoMapPolicy, symbol_visible

# Names defined in this many files are too common to provide useful references.
DEFINITION_FILE_FANOUT_THRESHOLD = 5
# Ranking distance of a file that no seed reaches through the graph: after every reachable file.
UNREACHABLE_DISTANCE = sys.maxsize
# Name-ref edges never cross a family: two languages that happen to share an identifier (e.g. Go
# and TS both defining `Parse`) must not collide. The family is derived from the grammar the bundle
# lock assigns to a file extension; a grammar outside this table is its own family.
GRAMMAR_FAMILIES = {
    "python": "python",
    "typescript": "js",
    "tsx": "js",
    "javascript": "js",
    "go": "go",
    "java": "java",
    "csharp": "csharp",
}
# Relative TS/JS import resolution tries these suffixes, in this order, for an extensionless target.
JS_RESOLUTION_ORDER = (".ts", ".tsx", ".js", ".jsx")
JS_EXTENSIONS = frozenset(JS_RESOLUTION_ORDER)


class FileRecord(TypedDict):
    """Запись файла в выходной карте: путь, видимые сигнатуры и статус разбора."""

    path: str
    signatures: list[str]
    parser_status: Literal["ok", "syntax_error", "invalid_encoding", "too_large"]


class EdgeRecord(TypedDict):
    """Ребро графа: источник, цель, вид связи и уверенность."""

    source: str
    target: str
    kind: str
    confidence: str


class Diagnostic(TypedDict):
    """Диагностика разбора файла для поля `diagnostics`."""

    code: Literal["syntax_error", "invalid_encoding", "file_too_large"]
    path: str


Edge = tuple[str, str, str, str]


def visible_signatures(signatures: list[SignatureFact], policy: RepoMapPolicy) -> list[str]:
    """Оставить сигнатуры, чей текст укладывается в лимит, а каждый сериализуемый символ проходит политику."""
    return [
        signature["text"]
        for signature in signatures
        if len(signature["text"]) <= policy.max_signature_length
        and all(symbol_visible(symbol, policy) for symbol in signature["symbols"])
    ]


def python_module(path: str) -> str:
    """Преобразовать путь `.py` в dotted-имя модуля."""
    return path.removesuffix(".py").replace("/", ".")


def module_paths(paths: set[str]) -> dict[str, str]:
    """Построить соответствие dotted-имени модуля его файлу; `pkg/__init__.py` становится `pkg`."""
    modules: dict[str, str] = {}
    for path in paths:
        module = path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
        if module:
            modules[module] = path
    return modules


def import_modules(fact: ImportFact, module: str) -> list[str]:
    """Вернуть кандидатов dotted-модулей для одного импорта: базовый модуль и каждое имя из `from ... import`.

    Относительный импорт разрешается от модуля `module` на `level` уровней вверх.
    """
    level = fact["level"]
    parent = module.rsplit(".", level)[0] if level else ""
    base = ".".join(part for part in (parent, fact["module"]) if part)
    if not base:
        return []
    return [base, *(f"{base}.{name}" for name in fact["names"] if name != "*")]


def _name_and_parts_visible(name: str, parts: list[str], policy: RepoMapPolicy) -> bool:
    """Проверить, что имя целиком и каждая его часть проходят политику символов."""
    return symbol_visible(name, policy) and all(symbol_visible(part, policy) for part in parts)


def module_visible(module: str, policy: RepoMapPolicy) -> bool:
    """Применить политику символов к dotted-модулю и каждому его сегменту до создания ребра импорта."""
    return _name_and_parts_visible(module, [part for part in module.split(".") if part], policy)


def specifier_visible(specifier: str, policy: RepoMapPolicy) -> bool:
    """Применить политику символов к спецификатору импорта JS/TS и каждому сегменту его пути."""
    parts = [
        part
        for part in posixpath.splitext(specifier)[0].split("/")
        if part not in {".", ".."}
    ]
    return _name_and_parts_visible(specifier, parts, policy)


def js_import_target(source: str, specifier: str, paths: set[str]) -> str | None:
    """Разрешить относительный импорт TS/JS в tracked-файл.

    Пробует точный путь, расширения `.ts`, `.tsx`, `.js`, `.jsx` и `index`-файлы. Пакеты, aliases и выход
    за корень репозитория не разрешаются.
    """
    if not specifier.startswith(("./", "../")):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))
    if base == ".." or base.startswith(("../", "/")):
        return None
    candidates = [base]
    if Path(base).suffix not in JS_EXTENSIONS:
        candidates.extend(f"{base}{extension}" for extension in JS_RESOLUTION_ORDER)
        candidates.extend(f"{base}/index{extension}" for extension in JS_RESOLUTION_ORDER)
    return next((candidate for candidate in candidates if candidate in paths), None)


def family(path: str, extension_grammars: dict[str, str]) -> str:
    """Вернуть языковое семейство файла для ссылок по имени.

    Семейство выводится из грамматики, которую lock назначил расширению. Файл без грамматики образует
    собственное семейство по суффиксу: имя семейства никогда не начинается с точки, поэтому такой файл
    не смешивается с известными семействами.
    """
    suffix = Path(path).suffix
    grammar = extension_grammars.get(suffix)
    if grammar is None:
        return suffix
    return GRAMMAR_FAMILIES.get(grammar, grammar)


def _js_import_edges(
    path: str, facts: FileFacts, js_paths: set[str], policy: RepoMapPolicy
) -> set[Edge]:
    """Рёбра `import/high` из относительных статических импортов одного TS/JS-файла."""
    edges: set[Edge] = set()
    for import_fact in facts["imports"]:
        specifier = import_fact["module"]
        if not specifier_visible(specifier, policy):
            continue
        target = js_import_target(path, specifier, js_paths)
        if target is not None and target != path:
            edges.add((path, target, "import", "high"))
    return edges


def _python_import_edges(
    path: str, facts: FileFacts, modules: dict[str, str], policy: RepoMapPolicy
) -> set[Edge]:
    """Рёбра `import/high` из разрешённых импортов одного Python-файла."""
    edges: set[Edge] = set()
    current_module = python_module(path)
    for import_fact in facts["imports"]:
        for imported_module in import_modules(import_fact, current_module):
            if not module_visible(imported_module, policy):
                continue
            target = modules.get(imported_module)
            if target and target != path and module_visible(python_module(target), policy):
                edges.add((path, target, "import", "high"))
    return edges


def _name_ref_edges(
    facts: dict[str, FileFacts],
    definitions: dict[tuple[str, str], set[str]],
    extension_grammars: dict[str, str],
    policy: RepoMapPolicy,
) -> set[Edge]:
    """Рёбра по ссылкам на имя: `unique-name-ref/medium` или `ambiguous-name-ref/low` в семействе."""
    edges: set[Edge] = set()
    for path in sorted(facts):
        path_family = family(path, extension_grammars)
        for name in set(facts[path]["references"]):
            if not symbol_visible(name, policy):
                continue
            definition_paths = definitions.get((path_family, name), set())
            if len(definition_paths) >= DEFINITION_FILE_FANOUT_THRESHOLD:
                continue
            targets = definition_paths - {path}
            kind, confidence = (
                ("unique-name-ref", "medium") if len(targets) == 1 else ("ambiguous-name-ref", "low")
            )
            for target in targets:
                edges.add((path, target, kind, confidence))
    return edges


def build_graph(
    records: dict[str, dict[str, object]],
    facts: dict[str, FileFacts],
    diagnostics: list[Diagnostic],
    policy: RepoMapPolicy,
    extension_grammars: dict[str, str],
) -> list[EdgeRecord]:
    """Применить политику символов к фактам worker, дополнить записи файлов и построить список рёбер.

    Импорты дают `import/high` (Python и относительные TS/JS), ссылки по имени —
    `unique-name-ref/medium` или `ambiguous-name-ref/low` в пределах одного семейства. Имена,
    определённые в `DEFINITION_FILE_FANOUT_THRESHOLD` и более файлах, отбрасываются.
    """
    modules = module_paths({path for path in records if path.endswith(".py")})
    js_paths = {path for path in records if Path(path).suffix in JS_EXTENSIONS}
    edges: set[Edge] = set()
    definitions: dict[tuple[str, str], set[str]] = {}
    for path in sorted(facts):
        record = facts[path]
        status = record["parser_status"]
        records[path]["parser_status"] = status
        if status != "ok":
            diagnostics.append({"code": status, "path": path})
        records[path]["signatures"] = visible_signatures(record["signatures"], policy)
        path_family = family(path, extension_grammars)
        for name in record["definitions"]:
            if symbol_visible(name, policy):
                definitions.setdefault((path_family, name), set()).add(path)
        if path in js_paths:
            edges |= _js_import_edges(path, record, js_paths, policy)
        elif path.endswith(".py"):
            edges |= _python_import_edges(path, record, modules, policy)
    edges |= _name_ref_edges(facts, definitions, extension_grammars, policy)
    diagnostics.sort(key=lambda item: (item["path"], item["code"]))
    return [
        {"source": source, "target": target, "kind": kind, "confidence": confidence}
        for source, target, kind, confidence in sorted(edges)
    ]


def _ranking_edges(paths: list[str], edges: list[EdgeRecord]) -> list[EdgeRecord]:
    """Рёбра высокой и средней уверенности между файлами карты: только они влияют на ранжирование."""
    known = set(paths)
    return [
        edge
        for edge in edges
        if edge["confidence"] != "low" and edge["source"] in known and edge["target"] in known
    ]


def _seed_distances(paths: list[str], edges: list[EdgeRecord], seeds: list[str]) -> dict[str, int]:
    """Расстояние каждого достижимого файла до ближайшего seed по неориентированному графу."""
    neighbors: dict[str, set[str]] = {path: set() for path in paths}
    for edge in edges:
        neighbors[edge["source"]].add(edge["target"])
        neighbors[edge["target"]].add(edge["source"])
    distances = {seed: 0 for seed in seeds}
    queue = deque(seeds)
    while queue:
        path = queue.popleft()
        for neighbor in sorted(neighbors[path]):
            if neighbor not in distances:
                distances[neighbor] = distances[path] + 1
                queue.append(neighbor)
    return distances


def rank_paths(paths: list[str], edges: list[EdgeRecord], seeds: list[str]) -> list[str]:
    """Упорядочить файлы по важности для чтения.

    С seeds — по расстоянию в графе через рёбра высокой и средней уверенности, без seeds — по
    входящей степени таких рёбер; при равенстве — по пути.
    """
    ranking_edges = _ranking_edges(paths, edges)
    if seeds:
        distances = _seed_distances(paths, ranking_edges, seeds)
        return sorted(paths, key=lambda path: (distances.get(path, UNREACHABLE_DISTANCE), path))
    indegree = dict.fromkeys(paths, 0)
    for edge in ranking_edges:
        indegree[edge["target"]] += 1
    return sorted(paths, key=lambda path: (-indegree[path], path))
