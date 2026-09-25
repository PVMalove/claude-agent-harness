#!/usr/bin/env python3
"""Offline parser bundle loader: locate, verify, install, and run a pinned parser bundle.

This module implements only the *generic* mechanism -- lock parsing, hash verification, an
offline `uv pip install --target` into an isolated cache directory, and a bounded subprocess call
into a worker script. It never imports `tree_sitter*` and carries no real grammar pins, so it
stays in the main `mypy --strict` run. The tree-sitter worker is tree_sitter_worker.py, and
scripts/build_parser_bundle.py assembles a real bundle from pinned wheels -- see
docs/adr/0024-repo-map-parser-bundle-composition-and-delivery.md. It also owns the worker's
`FileFacts` contract and rejects any response outside it.

Every entry point here is called only for `repo_map_policy.tier == "full"` (the default). It never
raises for a missing, mismatched, or misbehaving bundle -- callers get back a `DegradationReason`
string instead, so a failure here always turns into a valid `minimal` Repo Map, never a crash or a
network call.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

from harness.storage import storage_path

LOCK_FILENAME = "parser_bundle.lock.json"
INSTALL_MARKER_FILENAME = ".install-complete"

# A bare, safe file name: no path separators, no `.`/`..` traversal, no absolute path or drive
# letter, no control characters or whitespace (the allowed character set excludes all of these).
_SAFE_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")

# A strict wheel file name (PEP 427): distribution-version(-build)?-pytag-abitag-platformtag.whl.
# The character classes below are a subset of `_SAFE_FILENAME_RE`'s, so a match is automatically a
# safe bare file name too.
_WHEEL_FILENAME_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._]*[A-Za-z0-9])?"
    r"-[A-Za-z0-9][A-Za-z0-9._!+]*"
    r"(?:-[A-Za-z0-9][A-Za-z0-9._]*)?"
    r"-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+"
    r"\.whl"
)

# A bare lowercase sha256 hex digest, the form `hashlib.sha256().hexdigest()` produces. Artifact
# digests are interpolated into the generated requirements file, so nothing else may pass.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

DegradationReason = Literal[
    "offline parser bundle unavailable",
    "parser bundle hash mismatch",
    "parser wheelhouse missing for interpreter/platform pair",
    "parser subprocess exceeded time limit",
    "parser subprocess exceeded output size limit",
    "parser subprocess failed",
    "uv executable unavailable",
]


class BundleFormatError(ValueError):
    """The bundle lock file is not a well-formed `parser_bundle.lock.json`."""


@dataclass(frozen=True)
class ArtifactSpec:
    filename: str
    sha256: str


@dataclass(frozen=True)
class GrammarSpec:
    name: str
    version: str
    abi: int
    sha256: str
    extensions: tuple[str, ...]
    sha256_by_pair: dict[str, str] | None = None
    distribution: str | None = None


@dataclass(frozen=True)
class BundleLock:
    core_version: str
    core_abi_range: str
    worker_script: str
    script_sha256: str
    grammars: tuple[GrammarSpec, ...]
    wheelhouses: dict[str, tuple[ArtifactSpec, ...]]
    raw_sha256: str


class VerifyResult(TypedDict):
    ok: bool
    reason: DegradationReason | None


@dataclass(frozen=True)
class AppliedBundle:
    """A bundle whose lock, wheelhouse, install, and worker script all verified successfully."""

    lock: BundleLock
    install_dir: Path
    worker_script: Path
    python_tag: str
    platform_tag: str
    bundle_source: Literal["local-cache", "internal-registry"]
    worker_extensions: dict[str, str]


def _string_field(obj: dict[str, object], field: str) -> str:
    value = obj.get(field)
    if not isinstance(value, str) or not value:
        raise BundleFormatError(f"parser_bundle.lock.json: {field} must be a non-empty string")
    return value


def _safe_bare_filename(value: str, where: str) -> str:
    """Validate `value` as a bare, safe file name -- reject traversal, separators, or injection.

    Used for the fields interpolated into `Path` joins or a generated requirements file
    (`worker_script`, wheelhouse `filename`s); a lock that fails this is a degradation
    (`BundleFormatError`), never a crash or an install.
    """
    if not _SAFE_FILENAME_RE.fullmatch(value):
        raise BundleFormatError(
            f"parser_bundle.lock.json: {where} must be a safe bare file name, got {value!r}"
        )
    return value


def _wheel_filename(value: str, where: str) -> str:
    if not _WHEEL_FILENAME_RE.fullmatch(value):
        raise BundleFormatError(
            f"parser_bundle.lock.json: {where} must be a valid wheel file name, got {value!r}"
        )
    return value


def _sha256_field(obj: dict[str, object], field: str, where: str) -> str:
    """Validate a lock digest as a bare lowercase sha256 hex string -- reject injection.

    Artifact digests are interpolated into the generated requirements file, so a value carrying
    a newline or installer option would add requirement lines; a lock that fails this is a
    degradation (`BundleFormatError`), never a crash or an install.
    """
    value = _string_field(obj, field)
    if not _SHA256_RE.fullmatch(value):
        raise BundleFormatError(
            f"parser_bundle.lock.json: {where} must be a lowercase sha256 hex digest, got {value!r}"
        )
    return value


def _int_field(obj: dict[str, object], field: str) -> int:
    value = obj.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise BundleFormatError(f"parser_bundle.lock.json: {field} must be an integer")
    return value


def _artifact_specs(value: object, where: str) -> tuple[ArtifactSpec, ...]:
    if not isinstance(value, list):
        raise BundleFormatError(f"parser_bundle.lock.json: {where} must be a list")
    specs: list[ArtifactSpec] = []
    for item in value:
        if not isinstance(item, dict):
            raise BundleFormatError(f"parser_bundle.lock.json: {where} entries must be objects")
        specs.append(
            ArtifactSpec(
                filename=_wheel_filename(_string_field(item, "filename"), f"{where}.filename"),
                sha256=_sha256_field(item, "sha256", f"{where}.sha256"),
            )
        )
    return tuple(specs)


def parse_lock(raw: bytes) -> BundleLock:
    """Parse and structurally validate a `parser_bundle.lock.json` payload.

    Raises `BundleFormatError` for any structural problem; callers treat that the same as a
    missing bundle (`"offline parser bundle unavailable"`).
    """
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BundleFormatError(f"parser_bundle.lock.json: invalid JSON: {exc.msg}") from exc
    if not isinstance(decoded, dict):
        raise BundleFormatError("parser_bundle.lock.json: root must be an object")
    grammars_raw = decoded.get("grammars")
    if not isinstance(grammars_raw, list):
        raise BundleFormatError("parser_bundle.lock.json: grammars must be a list")
    grammars: list[GrammarSpec] = []
    for item in grammars_raw:
        if not isinstance(item, dict):
            raise BundleFormatError("parser_bundle.lock.json: grammars entries must be objects")
        extensions_raw = item.get("extensions")
        if not isinstance(extensions_raw, list) or not all(
            isinstance(entry, str) and entry for entry in extensions_raw
        ):
            raise BundleFormatError(
                "parser_bundle.lock.json: grammars[].extensions must be a list of non-empty strings"
            )
        pair_hashes_raw = item.get("sha256_by_pair")
        pair_hashes: dict[str, str] | None = None
        if pair_hashes_raw is not None:
            if not isinstance(pair_hashes_raw, dict):
                raise BundleFormatError("parser_bundle.lock.json: grammars[].sha256_by_pair must be an object")
            pair_hashes = {}
            for pair, digest in pair_hashes_raw.items():
                if not isinstance(pair, str) or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise BundleFormatError("parser_bundle.lock.json: invalid per-pair grammar hash")
                pair_hashes[pair] = digest
        distribution = item.get("distribution")
        if pair_hashes is not None and (
            not isinstance(distribution, str)
            or re.fullmatch(r"[A-Za-z0-9_]+", distribution) is None
        ):
            raise BundleFormatError("parser_bundle.lock.json: per-pair grammar distribution is invalid")
        grammars.append(
            GrammarSpec(
                name=_string_field(item, "name"),
                version=_string_field(item, "version"),
                abi=_int_field(item, "abi"),
                sha256=_sha256_field(item, "sha256", "grammars[].sha256"),
                extensions=tuple(extensions_raw),
                sha256_by_pair=pair_hashes,
                distribution=distribution if isinstance(distribution, str) else None,
            )
        )
    wheelhouses_raw = decoded.get("wheelhouses")
    if not isinstance(wheelhouses_raw, dict):
        raise BundleFormatError("parser_bundle.lock.json: wheelhouses must be an object")
    wheelhouses: dict[str, tuple[ArtifactSpec, ...]] = {}
    for pair, artifacts_raw in wheelhouses_raw.items():
        if not isinstance(pair, str):
            raise BundleFormatError("parser_bundle.lock.json: wheelhouses keys must be strings")
        wheelhouses[pair] = _artifact_specs(artifacts_raw, f"wheelhouses[{pair!r}]")
    for grammar in grammars:
        if grammar.sha256_by_pair is not None and (
            set(grammar.sha256_by_pair) != set(wheelhouses) or any(
                not any(
                    artifact.filename.startswith(f"{grammar.distribution}-{grammar.version}-")
                    and artifact.sha256 == grammar.sha256_by_pair[pair]
                    for artifact in artifacts
                )
                for pair, artifacts in wheelhouses.items()
            )
        ):
            raise BundleFormatError("parser_bundle.lock.json: per-pair grammar hash does not match its distribution wheel")
    return BundleLock(
        core_version=_string_field(decoded, "core_version"),
        core_abi_range=_string_field(decoded, "core_abi_range"),
        worker_script=_safe_bare_filename(_string_field(decoded, "worker_script"), "worker_script"),
        script_sha256=_sha256_field(decoded, "script_sha256", "script_sha256"),
        grammars=tuple(grammars),
        wheelhouses=wheelhouses,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )


def default_registry_dir(repo: Path) -> Path:
    """The portable, project-local bundle registry: never committed (`.harness/` is gitignored)."""
    return storage_path(repo, ".cache", "repo_map", "parser_bundle", "registry")


def search_dirs(repo: Path, registry_paths: tuple[str, ...]) -> tuple[Path, ...]:
    extra = tuple(
        Path(path) if Path(path).is_absolute() else repo / path for path in registry_paths
    )
    return (default_registry_dir(repo), *extra)


def find_bundle(dirs: tuple[Path, ...]) -> Path | None:
    """Return the first directory in `dirs` that holds a `parser_bundle.lock.json`, else None."""
    for candidate in dirs:
        if (candidate / LOCK_FILENAME).is_file():
            return candidate
    return None


def python_platform_tags(python_executable: str, timeout_seconds: int) -> tuple[str, str]:
    """The (python_tag, platform_tag) pair of the interpreter that will run the parser subprocess.

    Runs a short `-c` subprocess with that interpreter rather than reading the current process's
    own tags, since the parsing subprocess and this loader are not guaranteed to be the same
    interpreter (ADR 0024).
    """
    script = (
        "import sys, sysconfig, json;"
        "print(json.dumps(["
        "f'cp{sys.version_info[0]}{sys.version_info[1]}',"
        "sysconfig.get_platform().replace('-', '_').replace('.', '_'),"
        "]))"
    )
    result = subprocess.run(
        [python_executable, "-c", script],
        capture_output=True,
        check=False,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        raise BundleFormatError(
            f"could not determine python/platform tags: {result.stderr.decode('utf-8', 'replace')}"
        )
    decoded: object = json.loads(result.stdout.decode("utf-8"))
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not all(isinstance(item, str) for item in decoded)
    ):
        raise BundleFormatError("could not determine python/platform tags: malformed output")
    python_tag, platform_tag = cast(list[str], decoded)
    return python_tag, platform_tag


def verify_wheelhouse(lock: BundleLock, wheelhouse_dir: Path, pair: str) -> VerifyResult:
    """Verify the sha256 of every artifact this pair needs, and the worker script itself."""
    artifacts = lock.wheelhouses.get(pair)
    if artifacts is None or not wheelhouse_dir.is_dir():
        return {"ok": False, "reason": "parser wheelhouse missing for interpreter/platform pair"}
    for artifact in artifacts:
        artifact_path = wheelhouse_dir / artifact.filename
        if not artifact_path.is_file():
            return {"ok": False, "reason": "parser bundle hash mismatch"}
        digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        if digest != artifact.sha256:
            return {"ok": False, "reason": "parser bundle hash mismatch"}
    return {"ok": True, "reason": None}


def verify_worker_script(lock: BundleLock, worker_script: Path) -> bool:
    if not worker_script.is_file():
        return False
    digest = hashlib.sha256(worker_script.read_bytes()).hexdigest()
    return digest == lock.script_sha256


class BundleInstallError(RuntimeError):
    """`uv pip install --offline --no-index --target` failed or produced an unexpected result."""


class UvUnavailableError(BundleInstallError):
    """No `uv` executable on PATH: the harness installs bundles with uv only, never pip."""


@contextmanager
def _installation_lock(path: Path, timeout_seconds: int) -> Iterator[None]:
    """Держать межпроцессную блокировку на время установки общего bundle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise BundleInstallError("parser bundle installation lock failed") from exc
                if time.monotonic() >= deadline:
                    raise BundleInstallError("parser bundle installation lock timed out") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def install_bundle(
    lock: BundleLock,
    wheelhouse_dir: Path,
    install_dir: Path,
    python_executable: str,
    *,
    pair: str,
    timeout_seconds: int,
) -> None:
    """Установить bundle один раз для всех процессов, разделяющих каталог кэша."""
    try:
        with _installation_lock(install_dir.parent / ".install.lock", timeout_seconds * 2):
            _install_bundle_unlocked(
                lock, wheelhouse_dir, install_dir, python_executable,
                pair=pair, timeout_seconds=timeout_seconds,
            )
    except OSError as exc:
        raise BundleInstallError("parser bundle installation failed") from exc


def _install_bundle_unlocked(
    lock: BundleLock,
    wheelhouse_dir: Path,
    install_dir: Path,
    python_executable: str,
    *,
    pair: str,
    timeout_seconds: int,
) -> None:
    """Установить проверенные wheels через uv; вызывать под межпроцессной блокировкой."""
    marker = install_dir / INSTALL_MARKER_FILENAME
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == lock.raw_sha256:
        return
    uv = shutil.which("uv")
    if uv is None:
        raise UvUnavailableError("uv executable not found on PATH")
    artifacts = lock.wheelhouses.get(pair, ())
    install_dir.mkdir(parents=True, exist_ok=True)
    requirements_path = install_dir / ".requirements.txt"
    lines = []
    for artifact in artifacts:
        name, version = _wheel_name_version(artifact.filename)
        lines.append(f"{name}=={version} --hash=sha256:{artifact.sha256}")
    requirements_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # `--no-config` ignores uv.toml/pyproject configuration; stripping `UV_*` (and legacy `PIP_*`,
    # which uv also honors for some settings) from the child's environment closes the rest of that
    # gap (e.g. UV_INDEX_URL, UV_FIND_LINKS, UV_CONFIG_FILE) so nothing in the parent process's
    # environment can redirect this offline, `--no-index` install.
    isolated_env = {
        key: value for key, value in os.environ.items() if not key.startswith(("UV_", "PIP_"))
    }
    try:
        result = subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--offline",
                "--no-config",
                "--no-cache",
                "--no-index",
                "--find-links",
                str(wheelhouse_dir),
                "--require-hashes",
                "--only-binary",
                ":all:",
                "--python",
                python_executable,
                "--target",
                str(install_dir),
                "--requirement",
                str(requirements_path),
            ],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env=isolated_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise BundleInstallError("uv pip install timed out") from exc
    finally:
        requirements_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise BundleInstallError(result.stderr.decode("utf-8", "replace").strip())
    marker.write_text(lock.raw_sha256, encoding="utf-8")


def _wheel_name_version(filename: str) -> tuple[str, str]:
    stem = filename.removesuffix(".whl")
    parts = stem.split("-")
    if len(parts) < 2:
        raise BundleFormatError(f"parser_bundle.lock.json: malformed wheel filename {filename!r}")
    return parts[0], parts[1]


class SignatureFact(TypedDict):
    """One serialized signature plus every symbol it exposes, so policy redaction stays in-process."""

    text: str
    symbols: list[str]


class ImportFact(TypedDict):
    """`module` relative to `level` leading dots; `names` are the `from ... import` names, if any."""

    module: str
    level: int
    names: list[str]


class FileFacts(TypedDict):
    """The per-file worker contract: facts only -- edges and redaction are built by repo_map."""

    parser_status: Literal["ok", "syntax_error", "invalid_encoding"]
    signatures: list[SignatureFact]
    imports: list[ImportFact]
    definitions: list[str]
    references: list[str]


class BundleParseResult(TypedDict):
    files: dict[str, FileFacts]


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return cast(list[str], value)


def _signature_fact(value: object) -> SignatureFact | None:
    if not isinstance(value, dict) or set(value) != {"text", "symbols"}:
        return None
    text = value["text"]
    symbols = _string_list(value["symbols"])
    if not isinstance(text, str) or symbols is None:
        return None
    return {"text": text, "symbols": symbols}


def _import_fact(value: object) -> ImportFact | None:
    if not isinstance(value, dict) or set(value) != {"module", "level", "names"}:
        return None
    module = value["module"]
    level = value["level"]
    names = _string_list(value["names"])
    if (
        not isinstance(module, str)
        or isinstance(level, bool)
        or not isinstance(level, int)
        or level < 0
        or names is None
    ):
        return None
    return {"module": module, "level": level, "names": names}


def _file_facts(value: object) -> FileFacts | None:
    """Validate one worker record against the `FileFacts` contract; None for any deviation."""
    if not isinstance(value, dict) or set(value) != set(FileFacts.__annotations__):
        return None
    status = value["parser_status"]
    raw_signatures = value["signatures"]
    raw_imports = value["imports"]
    definitions = _string_list(value["definitions"])
    references = _string_list(value["references"])
    if (
        status not in ("ok", "syntax_error", "invalid_encoding")
        or not isinstance(raw_signatures, list)
        or not isinstance(raw_imports, list)
        or definitions is None
        or references is None
    ):
        return None
    signatures = [_signature_fact(item) for item in raw_signatures]
    imports = [_import_fact(item) for item in raw_imports]
    if any(item is None for item in signatures) or any(item is None for item in imports):
        return None
    return {
        "parser_status": cast(Literal["ok", "syntax_error", "invalid_encoding"], status),
        "signatures": [item for item in signatures if item is not None],
        "imports": [item for item in imports if item is not None],
        "definitions": definitions,
        "references": references,
    }


def _read_stdout(stream: object, output_queue: queue.Queue[bytes | None]) -> None:
    assert hasattr(stream, "read")
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            output_queue.put(chunk)
    finally:
        output_queue.put(None)


def _write_stdin(stream: object, payload: bytes) -> None:
    assert hasattr(stream, "write") and hasattr(stream, "close")
    try:
        stream.write(payload)
    except OSError:
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_bundle_parser(
    python_executable: str,
    worker_script: Path,
    install_dir: Path,
    request: dict[str, object],
    *,
    timeout_seconds: int,
    max_output_bytes: int,
    expected_script_sha256: str,
) -> BundleParseResult | DegradationReason:
    """Run `worker_script` as a subprocess, feeding it `request` as JSON on stdin.

    Bounded on both dimensions: `timeout_seconds` wall-clock and `max_output_bytes` of stdout.
    Re-verifies `worker_script`'s sha256 immediately before executing it, closing the TOCTOU gap
    between an earlier `acquire_bundle` verification and this call. Reads stdout on a background
    thread, and writes stdin on a second background thread started together with it, so a request
    larger than the OS pipe buffer can never deadlock against a worker that produces output before
    draining stdin (a plain synchronous write-then-read could block forever on either side). Never
    raises -- every failure mode becomes a `DegradationReason`.
    """
    try:
        script_digest = hashlib.sha256(worker_script.read_bytes()).hexdigest()
    except OSError:
        return "parser bundle hash mismatch"
    if script_digest != expected_script_sha256:
        return "parser bundle hash mismatch"
    try:
        proc = subprocess.Popen(
            [python_executable, str(worker_script), str(install_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return "parser subprocess failed"
    assert proc.stdin is not None and proc.stdout is not None
    payload = json.dumps(request).encode("utf-8")
    deadline = time.monotonic() + timeout_seconds
    output_queue: queue.Queue[bytes | None] = queue.Queue()
    reader = threading.Thread(target=_read_stdout, args=(proc.stdout, output_queue), daemon=True)
    writer = threading.Thread(target=_write_stdin, args=(proc.stdin, payload), daemon=True)
    reader.start()
    writer.start()
    chunks: list[bytes] = []
    total = 0
    reason: DegradationReason | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "parser subprocess exceeded time limit"
            break
        try:
            item = output_queue.get(timeout=remaining)
        except queue.Empty:
            reason = "parser subprocess exceeded time limit"
            break
        if item is None:
            break
        chunks.append(item)
        total += len(item)
        if total > max_output_bytes:
            reason = "parser subprocess exceeded output size limit"
            break
    if reason is not None:
        proc.kill()
        proc.wait()
        return reason
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return "parser subprocess exceeded time limit"
    if proc.returncode != 0:
        return "parser subprocess failed"
    try:
        decoded: object = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "parser subprocess failed"
    if not isinstance(decoded, dict):
        return "parser subprocess failed"
    files_obj = decoded.get("files")
    if not isinstance(files_obj, dict):
        return "parser subprocess failed"
    files: dict[str, FileFacts] = {}
    for key, value in files_obj.items():
        facts = _file_facts(value)
        if not isinstance(key, str) or facts is None:
            return "parser subprocess failed"
        files[key] = facts
    return {"files": files}


def build_provenance(
    lock: BundleLock | None,
    *,
    bundle_mode: Literal["applied", "degraded"],
    bundle_source: Literal["local-cache", "internal-registry", "none"],
    python_tag: str | None,
    platform_tag: str | None,
) -> dict[str, object]:
    """The `parser_provenance` fields this module owns, additive on top of repo_map's own."""
    provenance: dict[str, object] = {
        "bundle_mode": bundle_mode,
        "bundle_source": bundle_source,
    }
    if python_tag is not None:
        provenance["python_tag"] = python_tag
    if platform_tag is not None:
        provenance["platform_tag"] = platform_tag
    if bundle_mode == "applied":
        assert lock is not None
        provenance["lock_sha256"] = lock.raw_sha256
        provenance["script_hash"] = lock.script_sha256
        provenance["core_version"] = lock.core_version
        provenance["core_abi_range"] = lock.core_abi_range
        provenance["grammars"] = [
            {
                "name": grammar.name,
                "version": grammar.version,
                "abi": grammar.abi,
                "sha256": (
                    grammar.sha256_by_pair[f"{python_tag}-{platform_tag}"]
                    if grammar.sha256_by_pair is not None and python_tag is not None and platform_tag is not None
                    else grammar.sha256
                ),
            }
            for grammar in lock.grammars
        ]
    return provenance


def acquire_bundle(
    *,
    repo: Path,
    registry_paths: tuple[str, ...],
    python_executable: str,
    timeout_seconds: int,
) -> AppliedBundle | DegradationReason:
    """Locate, verify, and install a parser bundle for `python_executable`'s tags.

    Returns an `AppliedBundle` ready for `run_bundle_parser`, or a `DegradationReason` -- never
    raises. No step here makes a network call: every path below reads only the local filesystem
    and runs `uv pip install --offline --no-index --find-links <local wheelhouse dir>`.
    """
    dirs = search_dirs(repo, registry_paths)
    bundle_dir = find_bundle(dirs)
    if bundle_dir is None:
        return "offline parser bundle unavailable"
    bundle_source: Literal["local-cache", "internal-registry"] = (
        "local-cache" if bundle_dir == dirs[0] else "internal-registry"
    )
    try:
        lock = parse_lock((bundle_dir / LOCK_FILENAME).read_bytes())
    except (OSError, BundleFormatError):
        return "offline parser bundle unavailable"
    try:
        python_tag, platform_tag = python_platform_tags(python_executable, timeout_seconds)
    except (OSError, subprocess.TimeoutExpired, BundleFormatError, json.JSONDecodeError):
        return "parser subprocess failed"
    pair = f"{python_tag}-{platform_tag}"
    wheelhouse_dir = bundle_dir / "wheelhouse" / pair
    verify = verify_wheelhouse(lock, wheelhouse_dir, pair)
    if not verify["ok"]:
        assert verify["reason"] is not None
        return verify["reason"]
    worker_script = bundle_dir / lock.worker_script
    if not verify_worker_script(lock, worker_script):
        return "parser bundle hash mismatch"
    cache_root = storage_path(
        repo, ".cache", "repo_map", "parser_bundle",
        f"{lock.raw_sha256[:16]}-{python_tag}-{platform_tag}",
    )
    install_dir = cache_root / "install"
    try:
        install_bundle(
            lock,
            wheelhouse_dir,
            install_dir,
            python_executable,
            pair=pair,
            timeout_seconds=timeout_seconds,
        )
    except UvUnavailableError:
        return "uv executable unavailable"
    except BundleInstallError:
        return "parser subprocess failed"
    worker_extensions = {
        extension: grammar.name for grammar in lock.grammars for extension in grammar.extensions
    }
    return AppliedBundle(
        lock=lock,
        install_dir=install_dir,
        worker_script=worker_script,
        python_tag=python_tag,
        platform_tag=platform_tag,
        bundle_source=bundle_source,
        worker_extensions=worker_extensions,
    )
