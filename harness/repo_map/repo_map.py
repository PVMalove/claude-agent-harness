#!/usr/bin/env python3
"""Детерминированная offline-карта репозитория (Repo Map) для закреплённого коммита Git."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Literal

# `harness/bin/harness` copies this file verbatim into target projects as
# `.harness/repo_map/repo_map.py`. Alias `harness` to whichever of the two this
# file actually lives under so this standalone CLI has the same imports in both
# source and installed layouts. See docs/adr/0018.
_HARNESS_ROOT: Path = Path(__file__).resolve().parents[1]
_REPO_ROOT: Path = _HARNESS_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if _HARNESS_ROOT.name != "harness":
    _spec = importlib.util.spec_from_file_location(
        "harness",
        _HARNESS_ROOT / "__init__.py",
        submodule_search_locations=[str(_HARNESS_ROOT)],
    )
    assert _spec is not None and _spec.loader is not None
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["harness"] = _pkg
    _spec.loader.exec_module(_pkg)

from harness.errors import HarnessError, PolicyError, print_and_exit
from harness.repo_map.backend import BundleParserBackend, ParserBackend
from harness.repo_map.budget import select_within_budget, sized
from harness.repo_map.cache import cache_key, read_cache, write_cache
from harness.repo_map.git_source import RepoMapGitError, resolve_commit, tracked_paths
from harness.repo_map.graph import Diagnostic, EdgeRecord, build_graph, rank_paths
from harness.repo_map.policy import (
    DEFAULT_MAX_TOKENS,
    RepoMapPolicy,
    load_policy,
    path_allowed,
)
from harness.storage import storage_path
from harness.token_estimator import TOKEN_ESTIMATOR_VERSION

__all__ = [
    "BundleParserBackend",
    "ParserBackend",
    "RepoMapGitError",
    "RepoMapPolicy",
    "build_map",
    "load_policy",
    "main",
]

MINIMAL_POLICY_REASON = "policy requested minimal tier"


def _policy_provenance(policy: RepoMapPolicy, max_tokens: int) -> dict[str, object]:
    """Поля `parser_provenance`, описывающие политику и лимиты запуска."""
    return {
        "policy_mode": "enforced" if policy.enforced else "portable",
        "policy_sha256": policy.sha256,
        "policy_tier": policy.tier,
        "max_file_bytes": policy.max_file_bytes,
        "max_files": policy.max_files,
        "max_path_length": policy.max_path_length,
        "max_symbol_length": policy.max_symbol_length,
        "max_signature_length": policy.max_signature_length,
        "timeout_seconds": policy.timeout_seconds,
        "max_tokens": max_tokens,
    }


def _build_map(
    pinned: str,
    paths: list[str],
    max_tokens: int,
    seeds: list[str],
    policy: RepoMapPolicy,
    backend: ParserBackend | None,
) -> str:
    """Построить карту для уже отобранных путей: разбор, граф, ранжирование и отбор файлов в бюджет.

    Без backend (уровень `minimal`) карта содержит только пути. Если backend деградировал, карта тоже
    `minimal`, а причина попадает в `degradation_reason`.
    """
    files: dict[str, dict[str, object]] = {path: {"path": path} for path in paths}
    edges: list[EdgeRecord] = []
    diagnostics: list[Diagnostic] = []
    tier: Literal["minimal", "full"] = "minimal"
    parser: Literal["path-only", "bundle"] = "path-only"
    degradation_reason = MINIMAL_POLICY_REASON
    bundle_provenance: dict[str, object] = {}
    if backend is not None:
        parsed = backend.parse(pinned, paths)
        degradation_reason = parsed.reason
        bundle_provenance = parsed.provenance
        if parsed.applied:
            tier, parser = "full", "bundle"
            files = parsed.records
            diagnostics = parsed.diagnostics
            edges = build_graph(
                files, parsed.facts, diagnostics, policy, parsed.extension_grammars
            )
    ordered = rank_paths(list(files), edges, sorted(set(seeds) & files.keys()))
    payload: dict[str, object] = {
        "schema_version": 1,
        "commit": pinned,
        "tier": tier,
        "parser": parser,
        "degradation_reason": degradation_reason,
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
        "parser_provenance": {**_policy_provenance(policy, max_tokens), **bundle_provenance},
        "files": [],
        "edges": [],
        "diagnostics": [],
        "estimated_tokens": 0,
    }
    if sized(payload)[1] > max_tokens:
        raise PolicyError(
            "--max-tokens is too small for Repo Map metadata",
            remedy="raise --max-tokens or use a smaller policy tier and limits",
        )
    return select_within_budget(payload, ordered, files, edges, diagnostics, max_tokens)


def _check_budget(max_tokens: int, policy: RepoMapPolicy) -> None:
    """Проверить бюджет запроса против политики."""
    if max_tokens < 1:
        raise PolicyError(
            "--max-tokens must be positive",
            remedy="pass a positive integer to --max-tokens",
        )
    if policy.max_tokens is not None and max_tokens > policy.max_tokens:
        raise PolicyError(
            "--max-tokens exceeds repo_map_policy.max_tokens",
            remedy="lower --max-tokens or raise repo_map_policy.max_tokens in the project config",
        )


def _allowed_paths(repo: Path, pinned: str, policy: RepoMapPolicy) -> list[str]:
    """Tracked-пути коммита, разрешённые политикой, с проверкой лимита `max_files`."""
    paths = [
        path for path in tracked_paths(repo, pinned, policy.timeout_seconds)
        if path_allowed(path, policy)
    ]
    if len(paths) > policy.max_files:
        raise PolicyError(
            f"Repo Map has {len(paths)} policy-approved files, above repo_map_policy.max_files={policy.max_files}",
            remedy="narrow repo_map_policy.allow_paths or raise repo_map_policy.max_files deliberately",
        )
    return paths


def build_map(
    repo: Path,
    commit: str,
    max_tokens: int,
    seeds: list[str],
    policy: RepoMapPolicy | None = None,
    *,
    cache_dir: Path | None = None,
    backend: ParserBackend | None = None,
) -> str:
    """Построить Repo Map для коммита, по возможности переиспользуя проверенную запись кэша.

    Проверяет бюджет и политику, закрепляет коммит и отбирает tracked-пути. Для уровня `full` парсер
    берётся из `backend`, по умолчанию — `BundleParserBackend`. Результат кэшируется, если запрос
    `full` не деградировал.
    """
    effective_policy = policy or RepoMapPolicy()
    _check_budget(max_tokens, effective_policy)
    pinned = resolve_commit(repo, commit, effective_policy.timeout_seconds)
    paths = _allowed_paths(repo, pinned, effective_policy)
    normalized_seeds = sorted(set(seeds) & set(paths))
    if effective_policy.tier == "full" and backend is None:
        backend = BundleParserBackend(repo, effective_policy)
    elif effective_policy.tier == "minimal":
        backend = None
    root = cache_dir if cache_dir is not None else storage_path(repo, ".cache", "repo_map", "results")
    key = cache_key(
        pinned,
        normalized_seeds,
        max_tokens,
        effective_policy,
        backend.identity() if backend is not None else None,
    )
    cached = read_cache(root, key, pinned)
    if cached is not None and (backend is None or json.loads(cached).get("tier") == "full"):
        return cached
    result = _build_map(pinned, paths, max_tokens, normalized_seeds, effective_policy, backend)
    if backend is None or json.loads(result)["tier"] == "full":
        write_cache(root, key, result)
    return result


def main() -> int:
    """Точка входа CLI: разобрать аргументы, загрузить политику и напечатать карту; вернуть код выхода."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--commit", required=True)
    parser.add_argument("--seed", action="append", default=[])
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    try:
        policy_path = args.policy or args.repo / ".harness" / "orchestration.json"
        policy = load_policy(policy_path, explicit=args.policy is not None)
        if args.max_tokens is not None:
            max_tokens = args.max_tokens
        elif policy.max_tokens is not None:
            max_tokens = policy.max_tokens
        else:
            max_tokens = DEFAULT_MAX_TOKENS
        sys.stdout.write(
            build_map(
                args.repo,
                args.commit,
                max_tokens,
                args.seed,
                policy,
                cache_dir=args.cache_dir,
            )
        )
    except HarnessError as exc:
        return print_and_exit(exc)
    except ValueError as exc:
        return print_and_exit(
            PolicyError(
                str(exc),
                remedy="inspect the Repo Map input and project policy, then retry",
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
