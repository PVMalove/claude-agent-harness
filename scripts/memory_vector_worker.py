"""Real offline #431 probe. Run only in the separately locked experiment runtime."""

import argparse
import hashlib
import json
import importlib.metadata
import math
import platform
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import zipfile

import numpy as np
import onnxruntime as ort
import sqlite_vec
from tokenizers import Tokenizer


def run(artifacts: Path) -> dict:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from harness.memory import build, evaluate_memory
    from harness.memory.eval import (
        QueryEvalResult,
        MemoryEvalReport,
        load_golden_dataset,
    )
    from harness.memory.index import context
    from harness.memory.search import search_candidates
    from harness.memory.sources import collect_sources
    from scripts.memory_vector_probe import verify_artifacts, fuse_ranks, recommend
    from tests.memory.test_build import configure, source

    lock = json.loads((root / "tests/memory/vector_probe.lock.json").read_text())
    runtime_lock = root / lock["runtime_lock"]
    if (
        hashlib.sha256(runtime_lock.read_bytes()).hexdigest()
        != lock["runtime_lock_sha256"]
    ):
        raise ValueError("experiment runtime lock changed")
    runtime_project = tomllib.loads(
        runtime_lock.with_name("pyproject.toml").read_text()
    )
    for dependency in runtime_project["project"]["dependencies"]:
        package, version = dependency.split("==")
        if importlib.metadata.version(package) != version:
            raise ValueError(f"experiment runtime version changed: {package}")
    verify_artifacts(lock, artifacts)
    # Verify installed native bytes against the pinned wheel, not just a version string.
    selected = "win_amd64" if sys.platform == "win32" else "manylinux_2_17_x86_64"
    if platform.machine().lower() not in {"amd64", "x86_64"} or sys.platform not in {
        "linux",
        "win32",
    }:
        raise ValueError("unsupported experiment platform")
    wheel = next(w for w in lock["sqlite_vec"]["wheels"] if selected in w["filename"])
    verify_artifacts({"artifacts": [{**wheel, "path": wheel["filename"]}]}, artifacts)
    native = Path(sqlite_vec.loadable_path()).with_suffix(
        ".dll" if sys.platform == "win32" else ".so"
    )
    with zipfile.ZipFile(artifacts / wheel["filename"]) as archive:
        expected = archive.read("sqlite_vec/" + native.name)
    if native.read_bytes() != expected:
        raise ValueError("installed sqlite-vec differs from pinned wheel")

    # Imports above are complete. Network connections are forbidden from this point onwards.
    def offline(*args, **kwargs):
        raise RuntimeError("network forbidden during offline experiment")

    socket.socket.connect = offline
    socket.socket.connect_ex = offline
    socket.create_connection = offline
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(
        str(artifacts / "onnx/model.onnx"),
        sess_options=options,
        providers=["CPUExecutionProvider"],
    )
    tokenizer = Tokenizer.from_file(str(artifacts / "onnx/tokenizer.json"))
    tokenizer.enable_truncation(max_length=512)

    def encode(text, prefix):
        item = tokenizer.encode(prefix + text)
        ids = np.array([item.ids], dtype=np.int64)
        mask = np.array([item.attention_mask], dtype=np.int64)
        inputs = {"input_ids": ids, "attention_mask": mask}
        if any(x.name == "token_type_ids" for x in session.get_inputs()):
            inputs["token_type_ids"] = np.zeros_like(ids)
        hidden = session.run(None, inputs)[0]
        vector = (hidden * mask[:, :, None]).sum(axis=1) / mask.sum(axis=1)[:, None]
        vector = vector[0] / np.linalg.norm(vector[0])
        if vector.shape != (384,) or not np.isfinite(vector).all():
            raise ValueError("invalid embedding")
        return vector.astype(np.float32)

    ru = encode("Поиск прошлых решений проекта", "query: ")
    en = encode("Search previous project decisions", "query: ")
    if not all(abs(float(np.linalg.norm(v)) - 1) < 1e-5 for v in (ru, en)):
        raise ValueError("non-normalized embedding")
    baseline = json.loads((root / "tests/memory/baseline_fts5.json").read_text())
    dataset_path = root / "tests/memory/golden_tickets.json"
    if (
        hashlib.sha256(dataset_path.read_bytes()).hexdigest()
        != baseline["dataset_hash"]
    ):
        raise ValueError("dataset changed")
    run_dir = root / ".harness/.sandboxes/runs/issue-431-vector"
    run_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=run_dir) as temporary:
        repo = Path(temporary)
        subprocess.run(
            ["git", "init", "-q", str(repo)], check=True, capture_output=True
        )
        configure(repo)
        for path, digest in baseline["source_hashes"].items():
            raw = (root / path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError(f"corpus changed: {path}")
            source(repo, path, raw.decode("utf-8"))
        build(repo)
        fts5 = evaluate_memory(repo).to_dict()
        if fts5 != baseline["report"]:
            raise ValueError("FTS5 baseline changed")
        _, _, policy = context(repo)
        docs = collect_sources(repo, policy)
        connection = sqlite3.connect(":memory:")
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
            version = connection.execute("select vec_version()").fetchone()[0]
            if version != "v" + lock["sqlite_vec"]["version"]:
                raise ValueError("sqlite-vec version changed")
            connection.execute(
                "create virtual table smoke using vec0(embedding float[2] distance_metric=cosine)"
            )
            for i, v in enumerate(([1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]), 1):
                connection.execute(
                    "insert into smoke(rowid,embedding) values (?,?)",
                    (i, sqlite_vec.serialize_float32(v)),
                )
            order = [
                row[0]
                for row in connection.execute(
                    "select rowid from smoke where embedding match ? and k=3 order by distance",
                    (sqlite_vec.serialize_float32([1.0, 0.0]),),
                )
            ]
            if order != [1, 2, 3]:
                raise ValueError("cosine SQL probe failed")
            connection.execute(
                "create virtual table corpus using vec0(embedding float[384] distance_metric=cosine)"
            )
            paths = {}
            for i, doc in enumerate(docs, 1):
                paths[i] = doc.path
                vector = encode(doc.title + "\n" + doc.body, "passage: ")
                connection.execute(
                    "insert into corpus(rowid,embedding) values (?,?)",
                    (i, vector.tobytes()),
                )
            rows = load_golden_dataset(dataset_path)
            methods = {"vector": []}
            methods.update({f"hybrid@{t}": [] for t in lock["protocol"]["thresholds"]})
            for row in rows:
                vector = encode(row["query"], "query: ")
                scores = [
                    (paths[i], 1 - distance)
                    for i, distance in connection.execute(
                        "select rowid,distance from corpus where embedding match ? and k=? order by distance",
                        (vector.tobytes(), len(docs)),
                    )
                ]
                scores.sort(key=lambda item: (-item[1], item[0]))
                lexical = [
                    p["path"] for p in search_candidates(repo, row["query"])["pointers"]
                ]
                from harness.memory.eval import (
                    calculate_recall_at_k,
                    calculate_noise_ratio,
                )

                for name, results in methods.items():
                    ranked = (
                        [p for p, _ in scores]
                        if name == "vector"
                        else fuse_ranks(lexical, scores, float(name.split("@")[1]))
                    )
                    retrieved = ranked[:5]
                    results.append(
                        QueryEvalResult(
                            row["id"],
                            row["query"],
                            retrieved,
                            row["expected_sources"],
                            {
                                k: calculate_recall_at_k(
                                    retrieved, row["expected_sources"], k
                                )
                                for k in (1, 3, 5)
                            },
                            calculate_noise_ratio(retrieved, row["expected_sources"]),
                            "ok",
                        )
                    )
            reports = {}
            for name, results in methods.items():
                recall = {
                    k: math.fsum(r.recall_at_k[k] for r in results) / len(results)
                    for k in (1, 3, 5)
                }
                noise = math.fsum(r.noise_ratio for r in results) / len(results)
                reports[name] = MemoryEvalReport(
                    len(results),
                    recall,
                    noise,
                    results,
                    recall[5] >= 0.6 and noise <= 0.7,
                ).to_dict()
        finally:
            connection.close()
    return {
        "artifact_lock_sha256": hashlib.sha256(
            (root / "tests/memory/vector_probe.lock.json").read_bytes()
        ).hexdigest(),
        "model": lock["model"],
        "dataset_hash": baseline["dataset_hash"],
        "source_hashes": baseline["source_hashes"],
        "runtime": {
            "python": platform.python_version(),
            "os": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "onnxruntime": ort.__version__,
            "tokenizers": __import__("tokenizers").__version__,
            "numpy": np.__version__,
        },
        "sqlite_vec": {"version": version, "known_vector_order": order},
        "smoke": {
            "dimension": 384,
            "languages": ["ru", "en"],
            "ru_en_cosine": float(ru @ en),
        },
        "baseline": baseline["report"],
        "fts5": fts5,
        "methods": reports,
        "recommendation": recommend(fts5, reports["hybrid@0.8"], False),
        "limitation": "in-sample threshold grid; Windows/WSL/CI evidence required; maintainer decides",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.artifacts.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                name: {"recall": data["mean_recall"], "noise": data["mean_noise_ratio"]}
                for name, data in report["methods"].items()
            }
        )
    )
