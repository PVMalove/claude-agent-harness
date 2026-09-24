"""Context Packages: the bounded evidence a role is given instead of the whole repository.

A package pins the diff, starting files, symbol graph and related tests to one base/candidate pair,
hashes every included file, and is registered as an immutable record so a brief can reference it
without the package changing underneath it.
"""

from __future__ import annotations

import argparse
import hashlib
import uuid
from dataclasses import asdict
from pathlib import Path

from harness.context_builder.context_builder import (
    ContextPackageError,
    build_context_package,
)
from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core import config as core_config
from harness.orchestration.core import utils
from harness.orchestration.core.config import (
    _context_package_policy,
    _reject_sensitive,
)
from harness.orchestration.core.git_utils import (
    _candidate_commit,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _repo,
    _safe_id,
)
from harness.orchestration.ledger.ledger_ops import (
    _ledger_lock,
    _load_batch,
    _replace_record,
    _state_root,
    _write_record,
)
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    ContextPackageRecord,
    LifecycleLedger,
)
from harness.orchestration.workflow.history import (
    _latest_developer_candidate,
    _reusable_context_package,
    _validate_batch_integrity,
)


def _persist_context_package(
    repo: Path,
    root: Path,
    ledger: LifecycleLedger,
    batch: JsonObject,
    *,
    role: str,
    snapshot: str,
    inclusion_reason: str,
    min_starting_files: int = 1,
    max_starting_files: int | None = None,
    max_package_size_bytes: int | None = None,
    max_package_tokens: int | None = None,
    symbol_graph_depth: int | None = None,
    max_related_tests: int | None = None,
) -> JsonObject:
    """Build once and register a reusable Context Package for one pinned diff."""
    package_base = batch.get("integration_base_commit") or batch["base_commit"]
    reusable = _reusable_context_package(root, batch, package_base, snapshot)
    if reusable is not None:
        return reusable
    if role != "shared":
        raise CoordinatorError(
            "automatic Context Packages must be shared; role focus belongs in the immutable brief",
            remedy="remove role-specific focus from the shared Context Package; put it in the immutable dispatch brief instead",
        )
    policy = _context_package_policy(core_config._config(repo))
    if max_starting_files is None:
        max_starting_files = policy["max_starting_files"]
    # `max_package_tokens` is documented as an optional *stricter* ceiling (see coordinator_cli.py
    # --max-package-tokens help text). Silently honouring a caller-supplied value above the
    # project's configured budget is exactly how a review context package was pushed to ~130k
    # tokens against an 80k policy without any recorded decision; fail loudly instead.
    if max_package_tokens is not None and max_package_tokens > policy["max_tokens"]:
        raise CoordinatorError(
            f"--max-package-tokens={max_package_tokens} exceeds the configured "
            f"context_package_policy.max_tokens={policy['max_tokens']}; raise context_package_policy.max_tokens "
            "in project orchestration config instead of overriding it ad hoc per dispatch",
            remedy="raise context_package_policy.max_tokens in the project orchestration config instead of a per-dispatch override",
        )
    token_limit = (
        max_package_tokens if max_package_tokens is not None else policy["max_tokens"]
    )
    depth = (
        symbol_graph_depth
        if symbol_graph_depth is not None
        else policy["symbol_graph_depth"]
    )
    related_tests_cap = (
        max_related_tests
        if max_related_tests is not None
        else policy["max_related_tests"]
    )
    scope = batch.get("scope_preflight")
    expected_files = scope.get("expected_files", []) if isinstance(scope, dict) else []
    task_files = [path for path in expected_files if isinstance(path, str)]
    seed_files = [
        *task_files,
        "AGENTS.md",
        "README.md",
        ".harness/orchestration/roles/_common.md",
        ".harness/orchestration/contract.py",
    ]
    try:
        built = build_context_package(
            repo,
            package_base,
            snapshot,
            symbol_graph_depth=depth,
            min_starting_files=min_starting_files,
            max_starting_files=max_starting_files,
            max_package_size_bytes=max_package_size_bytes,
            max_package_tokens=token_limit,
            max_related_tests=related_tests_cap,
            seed_paths=seed_files,
        )
    except ContextPackageError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc
    package = {
        "context_package_id": f"context-package-{uuid.uuid4()}",
        "batch_id": batch["batch_id"],
        "base_commit": built.base_commit,
        "candidate_commit": built.candidate_commit,
        "diff": built.diff,
        "starting_files": [asdict(item) for item in built.starting_files],
        "symbol_graph": built.symbol_graph,
        "related_tests": built.related_tests,
        "precedent_cards": [asdict(item) for item in built.precedent_cards],
        "file_hashes": built.file_hashes,
        "size_bytes": built.size_bytes,
        "created_at": utils._now(),
        "estimated_tokens": built.estimated_tokens,
        "role": "shared",
        "inclusion_reason": inclusion_reason,
        "schema_version": built.schema_version,
        "parser": built.parser,
        "parser_provenance": built.parser_provenance,
    }
    _reject_sensitive(package, "context package")
    _safe_id(package["context_package_id"], "context package")
    _write_record(ledger, ContextPackageRecord.from_dict(package))
    batch.setdefault("context_packages", []).append(
        {
            "context_package_id": package["context_package_id"],
            "base_commit": package["base_commit"],
            "candidate_commit": package["candidate_commit"],
            "role": "shared",
            "record_sha256": hashlib.sha256(
                _canonical(package).encode("utf-8")
            ).hexdigest(),
        }
    )
    return package


def register_context_package(args: argparse.Namespace) -> JsonObject:
    """Build one Context Package for the batch's accepted developer candidate and register it as a
    new immutable, versioned, hashed ledger record -- the same ownership pattern as a risk
    assessment. Read-only over the repository; writes no coordinator or batch state beyond the
    package itself and its pointer entry."""
    repo = _repo(args)
    root = _state_root(args, repo)
    candidate = _candidate_commit(repo, args.candidate_commit)
    requested_role = getattr(args, "role", "shared")
    if requested_role != "shared":
        raise CoordinatorError(
            "Context Packages are batch-shared; role-specific focus stays in the dispatch brief",
            remedy="remove --inclusion-reason role-specific focus; put it in the dispatch brief instead",
        )
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        if batch.get("state") != "awaiting-approval":
            raise CoordinatorError(
                "a context package requires a batch awaiting coordinator approval",
                remedy="move the batch to awaiting coordinator approval before registering a context package",
            )
        base = batch.get("base_commit")
        if args.base_commit:
            requested_base = _candidate_commit(repo, args.base_commit)
            if requested_base != base:
                raise CoordinatorError(
                    "context package base must match the batch-captured base commit",
                    remedy="the context package base does not match the batch-captured base commit -- "
                    + INTERNAL_INVARIANT_REMEDY,
                )
        if candidate != _latest_developer_candidate(repo, root, batch):
            raise CoordinatorError(
                "candidate commit does not match the accepted developer report",
                remedy="pass the candidate_commit from the accepted developer report",
            )
        package = _persist_context_package(
            repo,
            root,
            ledger,
            batch,
            role="shared",
            snapshot=candidate,
            inclusion_reason=getattr(
                args, "inclusion_reason", "manual immutable context registration"
            ),
            min_starting_files=(
                args.min_starting_files
                if args.min_starting_files is not None
                else _context_package_policy(core_config._config(repo))["min_starting_files"]
            ),
            max_starting_files=args.max_starting_files,
            max_package_size_bytes=args.max_package_size_bytes,
            max_package_tokens=getattr(args, "max_package_tokens", None),
            symbol_graph_depth=getattr(args, "symbol_graph_depth", None),
            max_related_tests=getattr(args, "max_related_tests", None),
        )
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return package
