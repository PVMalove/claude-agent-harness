"""Synthetic parser-bundle fixtures shared by test_parser_bundle.py and test_repo_map.py.

Everything here is assembled directly with `zipfile`/plain text -- never `pip wheel`/`build` -- so
building a fixture never triggers build isolation or a network call. The wheel is pure Python with
no compiled extension.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

WHEEL_FILENAME = "stubparser-1.0.0-py3-none-any.whl"

WORKER_SCRIPT_SOURCE = """\
import base64
import json
import sys


def main() -> None:
    install_dir = sys.argv[1]
    sys.path.insert(0, install_dir)
    import stubparser  # proves the offline `pip install --target` succeeded

    assert stubparser.STUB_PARSER_INSTALLED
    request = json.loads(sys.stdin.read())
    files = {}
    for path, encoded in request.get("paths", {}).items():
        content = base64.b64decode(encoded).decode("utf-8", "replace")
        files[path] = {
            "signatures": [f"stub-signature:{len(content)}"],
            "parser_status": "ok",
        }
    sys.stdout.write(json.dumps({"files": files}))


if __name__ == "__main__":
    main()
"""

SLOW_WORKER_SCRIPT_SOURCE = """\
import sys
import time

sys.stdin.read()
time.sleep(5)
sys.stdout.write("{}")
"""

OVERSIZED_WORKER_SCRIPT_SOURCE = """\
import sys

sys.stdin.read()
sys.stdout.write("x" * 4096)
"""

FAILING_WORKER_SCRIPT_SOURCE = """\
import sys

sys.stdin.read()
sys.exit(1)
"""

MINIMAL_WORKER_SCRIPT_SOURCE = """\
import sys

sys.stdin.read()
sys.stdout.write('{"files": {}}')
"""

# Never drains stdin at all -- a synchronous stdin write on the caller's main thread, issued
# before the stdout reader starts, would deadlock against this worker once the request exceeds
# the OS pipe buffer (the caller blocks writing; this worker never unblocks it by reading).
OUTPUT_BEFORE_STDIN_DRAIN_WORKER_SCRIPT_SOURCE = """\
import sys

sys.stdout.write('{"files": {}}')
"""


def build_synthetic_wheel(dest_dir: Path) -> tuple[Path, str]:
    """Assemble a tiny pure-Python wheel with `zipfile` and return (path, sha256)."""
    wheel_path = dest_dir / WHEEL_FILENAME
    metadata = "Metadata-Version: 2.1\nName: stubparser\nVersion: 1.0.0\n"
    wheel_meta = (
        "Wheel-Version: 1.0\nGenerator: repo-map-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    init_py = "STUB_PARSER_INSTALLED = True\n"
    record = (
        "stubparser/__init__.py,,\n"
        "stubparser-1.0.0.dist-info/METADATA,,\n"
        "stubparser-1.0.0.dist-info/WHEEL,,\n"
        "stubparser-1.0.0.dist-info/RECORD,,\n"
    )
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("stubparser/__init__.py", init_py)
        archive.writestr("stubparser-1.0.0.dist-info/METADATA", metadata)
        archive.writestr("stubparser-1.0.0.dist-info/WHEEL", wheel_meta)
        archive.writestr("stubparser-1.0.0.dist-info/RECORD", record)
    digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    return wheel_path, digest


def write_worker_script(dest_dir: Path, source: str, *, filename: str = "worker.py") -> tuple[Path, str]:
    script_path = dest_dir / filename
    script_path.write_text(source, encoding="utf-8", newline="\n")
    digest = hashlib.sha256(script_path.read_bytes()).hexdigest()
    return script_path, digest


def build_bundle_dir(
    bundle_dir: Path,
    *,
    pair: str,
    worker_source: str = WORKER_SCRIPT_SOURCE,
) -> Path:
    """Assemble a full synthetic bundle directory: lock + worker script + wheelhouse for `pair`."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    wheelhouse_dir = bundle_dir / "wheelhouse" / pair
    wheelhouse_dir.mkdir(parents=True, exist_ok=True)
    _, wheel_sha256 = build_synthetic_wheel(wheelhouse_dir)
    _, script_sha256 = write_worker_script(bundle_dir, worker_source)
    lock = {
        "core_version": "0.1.0",
        "core_abi_range": ">=1,<2",
        "worker_script": "worker.py",
        "script_sha256": script_sha256,
        "grammars": [
            {
                "name": "stub-lang",
                "version": "1.0.0",
                "abi": 14,
                "sha256": wheel_sha256,
                "extensions": [".stub"],
            }
        ],
        "wheelhouses": {pair: [{"filename": WHEEL_FILENAME, "sha256": wheel_sha256}]},
    }
    (bundle_dir / "parser_bundle.lock.json").write_text(
        json.dumps(lock), encoding="utf-8"
    )
    return bundle_dir
