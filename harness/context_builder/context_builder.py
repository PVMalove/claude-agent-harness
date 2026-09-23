"""Deterministic, LLM-free Context Package builder for a base/candidate commit pair.

Sibling module to gate_runner.py: it reads a repository at two pinned commits (via git plumbing,
never the working tree) and returns one immutable, reproducible record — the exact diff, a bounded
set of starting files with a stated reason for each, an import-based dependency graph bounded to a
configurable depth, related tests, short ADR/precedent cards, and a hash of every included file.
It makes no model call, chooses no candidate, and writes no ledger or coordinator state; the caller
decides how (or whether) to persist the result.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from ..errors import HarnessError
from ..token_estimator import estimate_tokens as estimate_tokens


class ContextPackageError(HarnessError):
    """The package could not be built, or would exceed its configured size limit."""


_FROM_IMPORT_RE: re.Pattern[str] = re.compile(
    r"^\s*from\s+([\w.]+)\s+import\s+(.+)$", re.MULTILINE
)
_PLAIN_IMPORT_RE: re.Pattern[str] = re.compile(
    r"^\s*import\s+([\w.,\s]+)$", re.MULTILINE
)
_ADR_HEADING_RE: re.Pattern[str] = re.compile(r"^#\s+(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class StartingFile:
    path: str
    reason: str


@dataclass(frozen=True)
class PrecedentCard:
    id: str
    title: str
    summary: str


@dataclass(frozen=True)
class ContextPackage:
    base_commit: str
    candidate_commit: str
    diff: str
    starting_files: list[StartingFile]
    symbol_graph: dict[str, dict[str, list[str]]]
    related_tests: list[str]
    precedent_cards: list[PrecedentCard]
    file_hashes: dict[str, str]
    size_bytes: int
    estimated_tokens: int

    def to_json(self) -> str:
        return (
            json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )


def _run_git(repository: Path, *args: str) -> str:
    result: subprocess.CompletedProcess[str] = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ContextPackageError(
            f"git {' '.join(args)} failed: {detail or 'unknown error'}",
            remedy=f"inspect the git error above and fix the repository/commit refs before retrying 'git {' '.join(args)}'",
        )
    return result.stdout


def _list_files(repository: Path, commit: str) -> list[str]:
    output: str = _run_git(repository, "ls-tree", "-r", "--name-only", commit)
    return sorted(line for line in output.splitlines() if line)


def _read_file(repository: Path, commit: str, path: str) -> str:
    return _run_git(repository, "show", f"{commit}:{path}")


def _changed_files(
    repository: Path, base_commit: str, candidate_commit: str
) -> list[tuple[str, str]]:
    output: str = _run_git(
        repository, "diff", "--name-status", base_commit, candidate_commit
    )
    statuses: dict[str, str] = {"A": "added", "M": "modified", "D": "deleted"}
    changes: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts: list[str] = line.split("\t")
        code: str = parts[0]
        if code.startswith("R"):
            changes.append((parts[-1], "renamed"))
        else:
            changes.append((parts[-1], statuses.get(code[0], code)))
    return sorted(changes, key=lambda entry: entry[0])


def _module_name(path: str) -> str | None:
    if "." not in path.rsplit("/", 1)[-1]:
        return None
    stem: str = path.rsplit(".", 1)[0]
    if path.endswith(".py") and stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    return stem.replace("/", ".")


def _build_import_graph(
    repository: Path, commit: str, files: list[str]
) -> dict[str, set[str]]:
    module_to_path: dict[str, str] = {}
    for path in files:
        name: str | None = _module_name(path)
        if name and (name not in module_to_path or path.endswith(".py")):
            # Keep real Python modules authoritative when a resource shares their module-like name.
            module_to_path[name] = path
    graph: dict[str, set[str]] = {path: set() for path in files}
    for path in files:
        if not path.endswith(".py"):
            continue
        content: str = _read_file(repository, commit, path)
        candidates: set[str] = set()
        for match in _FROM_IMPORT_RE.finditer(content):
            base_module: str = match.group(1)
            candidates.add(base_module)
            for name in match.group(2).split(","):
                name = name.strip().split(" as ")[0].strip()
                if name and name != "*":
                    candidates.add(f"{base_module}.{name}")
        for match in _PLAIN_IMPORT_RE.finditer(content):
            for name in match.group(1).split(","):
                name = name.strip().split(" as ")[0].strip()
                if name:
                    candidates.add(name)
        for module in candidates:
            target: str | None = module_to_path.get(module)
            if target and target != path:
                graph[path].add(target)
    return graph


def _bounded_symbol_graph(
    graph: dict[str, set[str]], seeds: list[str], depth: int
) -> dict[str, dict[str, list[str]]]:
    imported_by: dict[str, set[str]] = {path: set() for path in graph}
    for path, imports in graph.items():
        for target in imports:
            imported_by[target].add(path)

    visited: set[str] = set()
    frontier: list[str] = list(dict.fromkeys(seeds))
    for _ in range(depth + 1):
        if not frontier:
            break
        visited.update(frontier)
        next_frontier: list[str] = []
        for node in frontier:
            for neighbour in graph.get(node, set()) | imported_by.get(node, set()):
                if neighbour not in visited:
                    next_frontier.append(neighbour)
        frontier = list(dict.fromkeys(next_frontier))

    return {
        path: {
            "imports": sorted(graph.get(path, ())),
            "imported_by": sorted(imported_by.get(path, ())),
        }
        for path in sorted(visited)
    }


def _fallback_excerpt(text: str) -> list[str]:
    """Return the deterministic bounded context for a file we cannot analyse."""
    return text.splitlines()[:30]


def _signature_for(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]
) -> str:
    """Extract a definition header selected by the AST, excluding its body."""
    body: list[ast.stmt] = node.body
    end_line: int | None = body[0].lineno - 1 if body else node.end_lineno
    header: str = "\n".join(lines[node.lineno - 1 : end_line]).strip()
    if header:
        return header

    # A one-line suite puts the first body node on the header line.  The final colon is the
    # definition delimiter even when parameters have annotations or defaults.
    line = lines[node.lineno - 1]
    return line[node.col_offset : line.rfind(":") + 1].strip()


def _extract_python_signatures(text: str) -> list[str]:
    """Return the module and top-level definition signatures from valid Python source."""
    tree: ast.Module = ast.parse(text)
    lines: list[str] = text.splitlines()
    signatures: list[str] = ["module"]
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            signatures.append(_signature_for(node, lines))
    return signatures


def _dependency_context(
    import_graph: dict[str, set[str]], seeds: list[str], files: dict[str, str]
) -> dict[str, list[str]]:
    """Build context for direct local dependencies only; never traverse a dependency's imports."""
    seed_paths: set[str] = set(seeds)
    direct_dependencies: list[str] = sorted(
        {
            target
            for seed in seed_paths
            for target in import_graph.get(seed, set())
            if target not in seed_paths
        }
    )
    context: dict[str, list[str]] = {}
    for path in direct_dependencies:
        text: str = files.get(path, "")
        if path.endswith(".py"):
            try:
                context[path] = _extract_python_signatures(text)
                continue
            except (SyntaxError, ValueError):
                pass
        context[path] = _fallback_excerpt(text)
    return context


def _select_starting_files(
    changed: list[tuple[str, str]],
    graph: dict[str, set[str]],
    imported_by: dict[str, set[str]],
    *,
    min_files: int,
    max_files: int,
) -> list[StartingFile]:
    reasons: dict[str, str] = {
        path: f"changed in diff ({status})" for path, status in changed
    }
    ordered: list[str] = sorted(reasons)

    if len(ordered) > max_files:
        return [
            StartingFile(path=path, reason=reasons[path])
            for path in ordered[:max_files]
        ]

    if len(ordered) >= min_files:
        return [StartingFile(path=path, reason=reasons[path]) for path in ordered]

    changed_set: set[str] = set(ordered)
    candidates: list[tuple[str, str]] = []
    for path in ordered:
        for target in sorted(graph.get(path, ())):
            if target not in changed_set:
                candidates.append((target, f"imports changed file {path}"))
        for source in sorted(imported_by.get(path, ())):
            if source not in changed_set:
                candidates.append((source, f"imported by changed file {path}"))

    for extra_path, reason in sorted(candidates, key=lambda entry: entry[0]):
        if extra_path in reasons:
            continue
        reasons[extra_path] = reason
        ordered.append(extra_path)
        if len(ordered) >= min_files:
            break

    if len(ordered) < min_files:
        raise ContextPackageError(
            f"only {len(ordered)} starting file(s) available (changed files plus their direct "
            f"import-graph neighbours) but min_starting_files={min_files}; fails clearly rather "
            "than silently returning fewer than the configured minimum",
            remedy=f"lower min_starting_files below {min_files}, widen the diff, or pass explicit seed_paths",
        )

    ordered.sort()
    return [StartingFile(path=path, reason=reasons[path]) for path in ordered]


def _related_tests(
    import_graph: dict[str, set[str]],
    files: list[str],
    starting_paths: set[str],
) -> list[str]:
    related = []
    for path in files:
        name: str = path.rsplit("/", 1)[-1]
        is_test_file: bool = "/tests/" in f"/{path}" and (
            name.startswith("test_") or name.endswith("_test.py")
        )
        if not is_test_file:
            continue
        if import_graph.get(path, set()) & starting_paths:
            related.append(path)
    return sorted(related)


def _precedent_cards(
    repository: Path, commit: str, files: list[str], keywords: set[str]
) -> list[PrecedentCard]:
    adr_files: list[str] = sorted(
        path for path in files if path.startswith("docs/adr/") and path.endswith(".md")
    )
    scored: list[tuple[int, str, PrecedentCard]] = []
    for path in adr_files:
        content: str = _read_file(repository, commit, path)
        haystack: str = f"{path} {content}".casefold()
        score: int = sum(
            1 for keyword in keywords if keyword and keyword.casefold() in haystack
        )
        if score <= 0:
            continue
        heading_match: re.Match[str] | None = _ADR_HEADING_RE.search(content)
        title: str = heading_match.group(1).strip() if heading_match else path
        paragraphs: list[str] = [
            block.strip()
            for block in content.split("\n\n")
            if block.strip() and not block.strip().startswith("#")
        ]
        summary: str = (paragraphs[0] if paragraphs else "")[:400]
        stem: str = path.rsplit("/", 1)[-1].removesuffix(".md")
        scored.append(
            (score, path, PrecedentCard(id=stem, title=title, summary=summary))
        )
    scored.sort(key=lambda entry: (-entry[0], entry[1]))
    return [card for _, _, card in scored]


def _keywords_for(paths: list[str]) -> set[str]:
    keywords: set[str] = set()
    for path in paths:
        stem: str = path.rsplit("/", 1)[-1].split(".")[0]
        keywords.update(part for part in re.split(r"[_\-]+", stem) if len(part) > 2)
    return keywords


def build_context_package(
    repository: Path,
    base_commit: str,
    candidate_commit: str,
    *,
    symbol_graph_depth: int = 2,
    min_starting_files: int = 5,
    max_starting_files: int = 10,
    max_package_size_bytes: int | None = None,
    max_package_tokens: int | None = 80_000,
    max_related_tests: int | None = None,
    seed_paths: list[str] | None = None,
) -> ContextPackage:
    """Build one immutable Context Package for `base_commit`..`candidate_commit`.

    Reads only pinned git history (`git show`/`git diff`/`git ls-tree`), never the working tree, so
    the same inputs always produce the same output regardless of local checkout state. Raises
    `ContextPackageError` instead of silently truncating when the assembled package would exceed
    `max_package_size_bytes` (pass `None` to disable the legacy diagnostic limit), the token-aware
    `max_package_tokens` limit, or -- when `max_related_tests` is set -- an import-graph fan-out
    that pulls in more related tests than a misscoped batch should. The token/byte limits are the
    admission control used by the coordinator; bytes are retained only for explicit
    backwards-compatible callers.
    """
    if min_starting_files < 1 or max_starting_files < min_starting_files:
        raise ContextPackageError(
            "min_starting_files must be >=1 and <= max_starting_files",
            remedy=f"set min_starting_files>=1 and max_starting_files>=min_starting_files (got min={min_starting_files}, max={max_starting_files})",
        )

    diff: str = _run_git(
        repository, "diff", "--no-color", base_commit, candidate_commit
    )
    changed: list[tuple[str, str]] = _changed_files(
        repository, base_commit, candidate_commit
    )

    files: list[str] = _list_files(repository, candidate_commit)
    import_graph: dict[str, set[str]] = _build_import_graph(
        repository, candidate_commit, files
    )
    imported_by: dict[str, set[str]] = {path: set() for path in import_graph}
    for path, imports in import_graph.items():
        for target in imports:
            imported_by[target].add(path)

    available_changed: list[tuple[str, str]] = [
        entry for entry in changed if entry[0] in files
    ]
    if available_changed:
        starting_files: list[StartingFile] = _select_starting_files(
            available_changed,
            import_graph,
            imported_by,
            min_files=min_starting_files,
            max_files=max_starting_files,
        )
    else:
        requested = [path for path in (seed_paths or []) if path in files]
        if not requested:
            requested = [path for path in ("AGENTS.md", "README.md") if path in files]
        if not requested:
            requested = files[:max_starting_files]
        requested = list(dict.fromkeys(requested))[:max_starting_files]
        if len(requested) < min_starting_files:
            raise ContextPackageError(
                f"only {len(requested)} static starting file(s) available but min_starting_files={min_starting_files}",
                remedy=f"lower min_starting_files below {min_starting_files} or pass more seed_paths that exist at {candidate_commit}",
            )
        starting_files = [
            StartingFile(path=path, reason="role preflight seed at pinned snapshot")
            for path in requested
        ]
    starting_paths: list[str] = [item.path for item in starting_files]

    symbol_graph: dict[str, dict[str, list[str]]] = _bounded_symbol_graph(
        import_graph, starting_paths, symbol_graph_depth
    )
    changed_paths: set[str] = {path for path, _ in changed}
    dependency_paths: list[str] = sorted(
        {
            target
            for path in changed_paths
            for target in import_graph.get(path, set())
            if target not in changed_paths
        }
    )
    dependency_files: dict[str, str] = {
        path: _read_file(repository, candidate_commit, path)
        for path in dependency_paths
    }
    direct_context: dict[str, list[str]] = _dependency_context(
        import_graph, sorted(changed_paths), dependency_files
    )
    for path, context in direct_context.items():
        symbol_graph.setdefault(
            path,
            {
                "imports": sorted(import_graph.get(path, ())),
                "imported_by": sorted(imported_by.get(path, ())),
            },
        )["context"] = context

    related_tests: list[str] = _related_tests(import_graph, files, set(starting_paths))
    if max_related_tests is not None and len(related_tests) > max_related_tests:
        raise ContextPackageError(
            f"related_tests count {len(related_tests)} exceeds max_related_tests={max_related_tests}; "
            "narrow the batch scope or raise context_package_policy.max_related_tests",
            remedy=f"narrow the batch's changed files or raise context_package_policy.max_related_tests above {len(related_tests)}",
        )

    keywords: set[str] = _keywords_for(starting_paths)
    precedent_cards: list[PrecedentCard] = _precedent_cards(
        repository, candidate_commit, files, keywords
    )

    included_paths: list[str] = sorted(
        set(starting_paths)
        | set(related_tests)
        | {f"docs/adr/{card.id}.md" for card in precedent_cards}
    )
    contents: dict[str, str] = {
        path: _read_file(repository, candidate_commit, path) for path in included_paths
    }
    file_hashes: dict[str, str] = {
        path: hashlib.sha256(content.encode("utf-8")).hexdigest()
        for path, content in contents.items()
    }

    # An added file's unified diff already contains 100% of its content as `+` lines, so counting
    # `contents[path]` again for the token/size estimate would double-count the exact same bytes
    # without adding information (unlike a modified file, where the diff is only hunks and
    # `contents[path]` genuinely adds the rest of the file).
    added_paths: set[str] = {path for path, status in changed if status == "added"}
    payload_text: str = "\n".join(
        [
            diff,
            json.dumps(symbol_graph, ensure_ascii=False, sort_keys=True),
            json.dumps(
                [asdict(card) for card in precedent_cards],
                ensure_ascii=False,
                sort_keys=True,
            ),
            *[contents[path] for path in sorted(contents) if path not in added_paths],
        ]
    )
    size_bytes: int = len(payload_text.encode("utf-8"))
    estimated_tokens: int = estimate_tokens(payload_text)
    if max_package_size_bytes is not None and size_bytes > max_package_size_bytes:
        raise ContextPackageError(
            f"context package size {size_bytes} bytes exceeds max_package_size_bytes={max_package_size_bytes}",
            remedy=f"narrow the batch scope or raise max_package_size_bytes above {size_bytes}",
        )
    if max_package_tokens is not None and estimated_tokens > max_package_tokens:
        raise ContextPackageError(
            f"context package estimate {estimated_tokens} tokens exceeds max_package_tokens={max_package_tokens}",
            remedy=f"narrow the batch scope or raise context_package_policy.max_package_tokens above {estimated_tokens}",
        )

    return ContextPackage(
        base_commit=base_commit,
        candidate_commit=candidate_commit,
        diff=diff,
        starting_files=starting_files,
        symbol_graph=symbol_graph,
        related_tests=related_tests,
        precedent_cards=precedent_cards,
        file_hashes=file_hashes,
        size_bytes=size_bytes,
        estimated_tokens=estimated_tokens,
    )
