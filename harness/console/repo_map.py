"""Stdlib-only facts for the console's Repo Map section: the map for HEAD from the Repo Map result
cache, or one built on request by the Repo Map CLI, and everything the screen derives from it -
summary, file tree with signatures, a file's relations, hubs, diagnostics and the Markdown/JSON
export.

Only schema v1 fields (harness/repo_map/repo_map.schema.json, .harness/repo_map/README.md) are
read, and a payload is accepted only after `harness.repo_map.contract.validation_error` passes. The
cache is read through `harness.repo_map.cache.read_cache`, which re-checks each entry's key, SHA-256,
commit and schema; opening the section never runs the CLI. Building does: the full tier may install
the offline parser bundle, so the screen asks first (`BUILD_WARNING`) and runs the CLI through the
injected CommandRunner, exactly like context_builder: `python -B <repo_map.py> --repo --commit`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..repo_map.cache import read_cache
from ..repo_map.contract import validation_error
from ..storage import storage_path
from .export import MarkdownDocument, MarkdownSection
from .runner import CommandRunner

REPO_MAP_SCRIPT_REL = Path(".harness") / "repo_map" / "repo_map.py"
_PACKAGE_SCRIPT = Path(__file__).resolve().parents[1] / "repo_map" / "repo_map.py"
HUBS_LIMIT = 10
_ERROR_TAIL_LINES = 20

BUILD_WARNING = (
    "Построение карты запускает Repo Map CLI для HEAD. В режиме tier=full CLI может установить "
    "offline parser bundle (uv pip install --offline) в "
    ".harness/.sandboxes/cache/repo_map/parser_bundle/; сеть не используется. Результат "
    "сохраняется в кэш Repo Map."
)

# Schema v1 enum order: the order relations are listed in.
EDGE_KINDS = ("import", "unique-name-ref", "ambiguous-name-ref")
CONFIDENCES = ("high", "medium", "low")


@dataclass(frozen=True)
class MapFile:
    path: str
    signatures: tuple[str, ...]
    parser_status: str | None  # absent in tier=minimal


@dataclass(frozen=True)
class MapEdge:
    source: str
    target: str
    kind: str
    confidence: str


@dataclass(frozen=True)
class MapDiagnostic:
    code: str
    path: str


@dataclass(frozen=True)
class RepoMapView:
    payload: dict[str, object]
    origin: str  # "кэш" or "построена"
    commit: str
    tier: str
    parser: str
    degradation_reason: str
    estimated_tokens: int
    provenance: dict[str, object]
    files: tuple[MapFile, ...]
    edges: tuple[MapEdge, ...]
    diagnostics: tuple[MapDiagnostic, ...]


@dataclass(frozen=True)
class Relation:
    direction: str  # "исходящие" or "входящие"
    kind: str
    confidence: str
    paths: tuple[str, ...]


def _dicts(value: object) -> list[dict[str, object]]:
    return (
        [item for item in value if isinstance(item, dict)]
        if isinstance(value, list)
        else []
    )


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def parse_map(payload: object, *, origin: str) -> RepoMapView | str:
    """A view of a schema v1 payload, or the contract violation that rejects it."""
    problem = validation_error(payload)
    if problem is not None:
        return f"карта не соответствует схеме v1: {problem}"
    assert isinstance(payload, dict)
    files = tuple(
        MapFile(
            path=_text(item.get("path")),
            signatures=_strings(item.get("signatures")),
            parser_status=_text(item.get("parser_status")) or None,
        )
        for item in _dicts(payload["files"])
    )
    edges = tuple(
        MapEdge(
            _text(item.get("source")),
            _text(item.get("target")),
            _text(item.get("kind")),
            _text(item.get("confidence")),
        )
        for item in _dicts(payload["edges"])
    )
    diagnostics = tuple(
        MapDiagnostic(_text(item.get("code")) or "?", _text(item.get("path")))
        for item in _dicts(payload["diagnostics"])
    )
    provenance = payload["parser_provenance"]
    estimated = payload["estimated_tokens"]
    return RepoMapView(
        payload=payload,
        origin=origin,
        commit=_text(payload["commit"]),
        tier=_text(payload["tier"]),
        parser=_text(payload["parser"]),
        degradation_reason=_text(payload["degradation_reason"]),
        estimated_tokens=estimated if isinstance(estimated, int) else 0,
        provenance=provenance if isinstance(provenance, dict) else {},
        files=files,
        edges=edges,
        diagnostics=diagnostics,
    )


def head_commit(repo: Path, *, timeout: float = 30.0) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def cache_directory(repo: Path) -> Path | None:
    """The Repo Map CLI's own result cache (`storage_path`, shared by linked worktrees)."""
    try:
        return storage_path(repo, "cache", "repo_map", "results")
    except ValueError:
        return None


def cached_map(repo: Path, commit: str) -> RepoMapView | None:
    """The best verified cache entry for `commit`: tier=full before minimal, then the newest.

    Entries are keyed by every build input (seeds, budget, policy, parser identity), so a commit can
    have several; each is accepted only if `read_cache` verifies it for this exact commit."""
    directory = cache_directory(repo)
    if directory is None or not directory.is_dir():
        return None
    best: tuple[tuple[bool, float], RepoMapView] | None = None
    for path in directory.glob("*.json"):
        payload = read_cache(directory, path.stem, commit)
        if payload is None:
            continue
        view = parse_map(json.loads(payload), origin="кэш")
        if isinstance(view, str):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        rank = (view.tier == "full", mtime)
        if best is None or rank > best[0]:
            best = (rank, view)
    return best[1] if best is not None else None


def repo_map_script(repo: Path) -> Path:
    """The project's installed Repo Map CLI, or this harness's own copy when it is not installed."""
    installed = repo / REPO_MAP_SCRIPT_REL
    return installed if installed.is_file() else _PACKAGE_SCRIPT


def build_argv(repo: Path, commit: str) -> list[str]:
    # -B: no __pycache__ inside the target repository, as context_builder runs the same CLI.
    return [
        sys.executable,
        "-B",
        str(repo_map_script(repo)),
        "--repo",
        str(repo),
        "--commit",
        commit,
    ]


def build_map(repo: Path, commit: str, runner: CommandRunner) -> RepoMapView | str:
    """Run the Repo Map CLI for `commit`; the view, or why there is none."""
    try:
        result = runner(build_argv(repo, commit), cwd=repo)
    except OSError as exc:
        return f"не удалось запустить Repo Map CLI: {exc}"
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-_ERROR_TAIL_LINES:]
        return "\n".join(
            [f"Repo Map CLI завершился с кодом {result.returncode}", *tail]
        )
    try:
        payload: object = json.loads(result.stdout or "")
    except json.JSONDecodeError as exc:
        return f"Repo Map CLI вернул не JSON: {exc}"
    return parse_map(payload, origin="построена")


def summary_lines(view: RepoMapView) -> list[str]:
    return [
        f"tier: {view.tier} (parser: {view.parser})",
        f"причина деградации: {view.degradation_reason}",
        f"commit: {view.commit}",
        f"файлов: {len(view.files)}",
        f"рёбер: {len(view.edges)}",
        f"токенов (оценка): {view.estimated_tokens}",
        f"источник: {view.origin}",
    ]


def provenance_lines(view: RepoMapView) -> list[str]:
    lines: list[str] = []
    for key, value in view.provenance.items():
        if key == "grammars":
            for grammar in _dicts(value):
                lines.append(
                    "grammar: "
                    f"{grammar.get('name')}@{grammar.get('version')} "
                    f"abi={grammar.get('abi')} sha256={grammar.get('sha256')}"
                )
        else:
            lines.append(f"{key}: {value}")
    return lines


def search_files(view: RepoMapView, query: str) -> list[MapFile]:
    """Files with a signature containing `query` (case-insensitive); every file for an empty one."""
    needle = query.strip().casefold()
    if not needle:
        return list(view.files)
    return [
        item
        for item in view.files
        if any(needle in signature.casefold() for signature in item.signatures)
    ]


def _order(value: str, known: tuple[str, ...]) -> int:
    return known.index(value) if value in known else len(known)


def relations(view: RepoMapView, path: str) -> list[Relation]:
    """The file's outgoing, then incoming, edges grouped by kind and confidence."""
    groups: dict[tuple[str, str, str], set[str]] = {}
    for edge in view.edges:
        if edge.source == path:
            groups.setdefault(("исходящие", edge.kind, edge.confidence), set()).add(
                edge.target
            )
        if edge.target == path:
            groups.setdefault(("входящие", edge.kind, edge.confidence), set()).add(
                edge.source
            )
    return [
        Relation(direction, kind, confidence, tuple(sorted(paths)))
        for (direction, kind, confidence), paths in sorted(
            groups.items(),
            key=lambda item: (
                item[0][0] != "исходящие",
                _order(item[0][1], EDGE_KINDS),
                _order(item[0][2], CONFIDENCES),
            ),
        )
    ]


def relations_text(view: RepoMapView, path: str) -> str:
    groups = relations(view, path)
    if not groups:
        return f"{path}\nсвязей нет"
    lines = [path]
    for group in groups:
        lines.append(
            f"{group.direction} · {group.kind} · {group.confidence}: {', '.join(group.paths)}"
        )
    return "\n".join(lines)


def hubs(view: RepoMapView, limit: int = HUBS_LIMIT) -> list[tuple[str, int]]:
    """The files with the most distinct incoming neighbours, ties broken by path."""
    incoming = Counter(
        target for _, target in {(e.source, e.target) for e in view.edges}
    )
    return sorted(incoming.items(), key=lambda item: (-item[1], item[0]))[:limit]


def hubs_text(view: RepoMapView) -> str:
    top = hubs(view)
    if not top:
        return "рёбер нет — хабов нет"
    return "\n".join(f"{count:>4}  {path}" for path, count in top)


def diagnostics_text(view: RepoMapView) -> str:
    if not view.diagnostics:
        return "диагностик нет"
    return "\n".join(f"{item.code}: {item.path}" for item in view.diagnostics)


def file_label(item: MapFile) -> str:
    name = item.path.rsplit("/", 1)[-1]
    return f"{name} [{item.parser_status}]" if item.parser_status else name


def _files_text(view: RepoMapView) -> str:
    lines: list[str] = []
    for item in view.files:
        status = f" [{item.parser_status}]" if item.parser_status else ""
        lines.append(f"{item.path}{status}")
        lines.extend(f"    {signature}" for signature in item.signatures)
    return "\n".join(lines) or "файлов нет"


def _edges_text(view: RepoMapView) -> str:
    return (
        "\n".join(
            f"{edge.source} -> {edge.target} ({edge.kind}, {edge.confidence})"
            for edge in view.edges
        )
        or "рёбер нет"
    )


def map_json(view: RepoMapView) -> str:
    """The payload as the CLI emitted it (schema v1), pretty-printed."""
    return json.dumps(view.payload, ensure_ascii=False, indent=2) + "\n"


def export_slug(view: RepoMapView) -> str:
    return f"repo-map-{view.commit[:12]}"


def map_document(view: RepoMapView) -> MarkdownDocument:
    return MarkdownDocument(
        title=f"Repo Map {view.commit[:12]}",
        slug=export_slug(view),
        meta=[
            ("Commit", f"`{view.commit}`"),
            ("Tier", f"{view.tier} ({view.parser})"),
            ("Причина деградации", view.degradation_reason),
            ("Файлов", str(len(view.files))),
            ("Рёбер", str(len(view.edges))),
            ("Токенов (оценка)", str(view.estimated_tokens)),
        ],
        sections=[
            MarkdownSection(
                "Provenance парсера",
                "\n".join(provenance_lines(view)),
                preformatted=True,
            ),
            MarkdownSection(
                "Хабы (входящая степень)", hubs_text(view), preformatted=True
            ),
            MarkdownSection("Диагностики", diagnostics_text(view), preformatted=True),
            MarkdownSection("Файлы и сигнатуры", _files_text(view), preformatted=True),
            MarkdownSection("Рёбра", _edges_text(view), preformatted=True),
        ],
    )
