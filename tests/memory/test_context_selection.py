"""Context Package memory selection at the freeze boundary uses validated sources."""

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from harness.memory import build
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.workflow.context_package import _persist_context_package
from .test_build import configure, git, source


def prepare(repo: Path, **policy: object) -> tuple[Path, LifecycleLedger, JsonObject]:
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    configure(repo, **{"min_similarity": 0, **policy})
    source(repo, "README.md", "# Fixture")
    git(repo, "add", "README.md")
    git(repo, "commit", "-qm", "fixture")
    snapshot = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    root = repo / "state"
    ledger = LifecycleLedger(root)
    ledger.ensure()
    batch: JsonObject = {
        "batch_id": "batch-memory",
        "base_commit": snapshot,
        "goal": "transaction",
        "definition_of_done": [],
        "context_packages": [],
    }
    return root, ledger, batch


def freeze(
    repo: Path, prepared: tuple[Path, LifecycleLedger, JsonObject]
) -> JsonObject:
    root, ledger, batch = prepared
    return _persist_context_package(
        repo,
        root,
        ledger,
        batch,
        role="shared",
        snapshot=batch["base_commit"],
        inclusion_reason="test",
    )


def test_cosine_threshold_degrades_before_opening_or_creating_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = prepare(tmp_path, min_similarity=0.1)

    def forbid_index(*args: object, **kwargs: object) -> None:
        raise AssertionError("no vector backend means no SQLite access")

    monkeypatch.setattr(sqlite3, "connect", forbid_index)
    package = freeze(tmp_path, prepared)
    assert package["memory"]["status"] == "vector_threshold_unavailable"
    assert package["memory"]["pointers"] == []
    assert not (tmp_path / ".harness/.sandboxes/cache/memory").exists()


@pytest.mark.parametrize("top_k", [1, 5, 9])
def test_quotas_use_validated_report_projection_types(
    tmp_path: Path, top_k: int
) -> None:
    from collections import Counter

    prefix = ".harness/orchestration/state"
    prepared = prepare(
        tmp_path,
        top_k=top_k,
        max_tokens=10000,
        source_types=["adr", "qa_finding", "completion_report"],
        allow_paths=["docs/adr/*.md", prefix + "/generations/**/*.json"],
    )
    source(
        tmp_path,
        prefix + "/ledger.json",
        json.dumps(
            {"version": 3, "generation": "generation-one", "selected_at": "fixture"}
        ),
    )
    for number in range(3):
        source(
            tmp_path,
            f"docs/adr/{number}.md",
            "# Decision\nStatus: accepted\ntransaction",
        )
        source(
            tmp_path,
            prefix + f"/generations/generation-one/reports/completion-{number}.json",
            json.dumps(
                {
                    "role": "developer",
                    "ticket": f"#{number}",
                    "outcome": "completed",
                    "lessons": ["transaction private lesson"],
                }
            ),
        )
        source(
            tmp_path,
            prefix + f"/generations/generation-one/reports/qa-{number}.json",
            json.dumps(
                {
                    "role": "qa",
                    "ticket": f"#{number}",
                    "outcome": "completed",
                    "output": "transaction private finding",
                }
            ),
        )
    build(tmp_path)
    package = freeze(tmp_path, prepared)
    pointers = package["memory"]["pointers"]
    counts = Counter(pointer["source_type"] for pointer in pointers)
    assert len(pointers) == min(top_k, 6)
    assert all(count <= min(top_k, 2) for count in counts.values())
    if top_k == 9:
        assert counts == {"adr": 2, "qa_finding": 2, "completion_report": 2}
    assert all(
        pointer["status"] == "не подтверждено человеком"
        for pointer in pointers
        if pointer["source_type"] == "completion_report"
    )
    assert "private lesson" not in json.dumps(package["memory"])
    assert "private finding" not in json.dumps(package["memory"])


def test_freeze_queries_once_readonly_and_reuses_without_network_or_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    prepared = prepare(tmp_path)
    source(tmp_path, "docs/adr/prior.md", "# Prior\ntransaction")
    build(tmp_path)
    original_connect = sqlite3.connect
    connections: list[str] = []

    def readonly_connect(
        database: str, *, uri: bool = False, timeout: float = 5.0
    ) -> sqlite3.Connection:
        connections.append(str(database))
        assert str(database).endswith("?mode=ro")
        return original_connect(database, uri=uri, timeout=timeout)

    def forbid_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("freezing memory cannot access the network")

    monkeypatch.setattr(sqlite3, "connect", readonly_connect)
    monkeypatch.setattr(socket, "create_connection", forbid_network)
    package = freeze(tmp_path, prepared)
    assert package["memory"]["pointers"]
    assert freeze(tmp_path, prepared) == package
    assert len(connections) == 1


def test_legacy_v2_integrity_is_read_without_rewriting_or_v3_reuse(
    tmp_path: Path,
) -> None:
    import hashlib

    from harness.orchestration.core.utils import _canonical
    from harness.orchestration.workflow.history import _validate_context_package

    prepared = prepare(tmp_path)
    package = freeze(tmp_path, prepared)
    legacy = {
        key: value
        for key, value in package.items()
        if key not in {"goal", "definition_of_done", "memory"}
    }
    legacy["schema_version"] = 2
    root, _, batch = prepared
    record = root / "generations"
    # The fixture ledger selected its own generation; retain the original v2 record bytes.
    generation = json.loads((root / "ledger.json").read_text())["generation"]
    path = (
        record
        / generation
        / "context-packages"
        / (legacy["context_package_id"] + ".json")
    )
    original_bytes = (json.dumps(legacy, ensure_ascii=False, indent=2) + "\n").encode()
    path.write_bytes(original_bytes)
    batch["context_packages"][-1]["record_sha256"] = hashlib.sha256(
        _canonical(legacy).encode()
    ).hexdigest()
    _validate_context_package(root, batch, legacy)
    new = freeze(tmp_path, prepared)
    assert new["schema_version"] == 3
    assert new["context_package_id"] != legacy["context_package_id"]
    assert path.read_bytes() == original_bytes


def test_single_source_type_underfills_in_stable_bm25_path_order(
    tmp_path: Path,
) -> None:
    prepared = prepare(tmp_path, source_types=["adr"], top_k=5, max_tokens=10000)
    for name in ("c", "a", "b"):
        source(tmp_path, f"docs/adr/{name}.md", "# Same\ntransaction")
    build(tmp_path)
    pointers = freeze(tmp_path, prepared)["memory"]["pointers"]
    assert [pointer["path"] for pointer in pointers] == [
        "docs/adr/a.md",
        "docs/adr/b.md",
    ]
