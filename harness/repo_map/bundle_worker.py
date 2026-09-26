"""Протокол tree-sitter worker и его ограниченный запуск в отдельном процессе.

Здесь задан контракт `FileFacts`: любой ответ worker вне него отклоняется целиком. Запуск ограничен
по времени и объёму вывода, а любой сбой возвращается строкой `WorkerFailure`, а не исключением.
При сбое хвост stderr worker сохраняется в диагностический лог рядом с установкой bundle.
"""

from __future__ import annotations

import hashlib
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import IO, Literal, TypedDict, cast

WorkerFailure = Literal[
    "parser bundle hash mismatch",
    "parser subprocess exceeded time limit",
    "parser subprocess exceeded output size limit",
    "parser subprocess failed",
]
ParserStatus = Literal["ok", "syntax_error", "invalid_encoding"]
# Grammar names the tree-sitter worker has extractors for; a lock naming none of them cannot map any
# real source file (a test stub bundle, for example).
SUPPORTED_GRAMMARS = frozenset({"python", "typescript", "tsx", "javascript", "go", "java", "csharp"})

# Size of one read from a worker pipe.
STDOUT_CHUNK_BYTES = 65536
# How much of the worker's stderr survives into the diagnostic log after a failure.
STDERR_TAIL_BYTES = 4096
# How long a failed run waits for the stderr reader to finish after the worker exited or was killed.
STDERR_JOIN_SECONDS = 1.0
WORKER_ERROR_LOG_FILENAME = "last-worker-error.log"


class SignatureFact(TypedDict):
    """Сериализованная сигнатура и все раскрываемые ею символы: редактирование по политике остаётся в основном процессе."""

    text: str
    symbols: list[str]


class ImportFact(TypedDict):
    """Импорт: `module` относительно `level` ведущих точек; `names` — имена из `from ... import`, если есть."""

    module: str
    level: int
    names: list[str]


class FileFacts(TypedDict):
    """Контракт worker для одного файла: только факты; рёбра и редактирование строит repo_map."""

    parser_status: ParserStatus
    signatures: list[SignatureFact]
    imports: list[ImportFact]
    definitions: list[str]
    references: list[str]


class BundleParseResult(TypedDict):
    """Проверенный ответ worker: факты по путям файлов."""

    files: dict[str, FileFacts]


def _string_list(value: object) -> list[str] | None:
    """Вернуть значение как список строк или `None`, если оно им не является."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return cast(list[str], value)


def _signature_fact(value: object) -> SignatureFact | None:
    """Проверить запись сигнатуры из ответа worker; `None` при любом отклонении."""
    if not isinstance(value, dict) or set(value) != {"text", "symbols"}:
        return None
    text = value["text"]
    symbols = _string_list(value["symbols"])
    if not isinstance(text, str) or symbols is None:
        return None
    return {"text": text, "symbols": symbols}


def _import_fact(value: object) -> ImportFact | None:
    """Проверить запись импорта из ответа worker; `None` при любом отклонении."""
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
    """Проверить одну запись worker по контракту `FileFacts`; `None` при любом отклонении."""
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
        "parser_status": cast(ParserStatus, status),
        "signatures": [item for item in signatures if item is not None],
        "imports": [item for item in imports if item is not None],
        "definitions": definitions,
        "references": references,
    }


def _decode_response(raw: bytes) -> BundleParseResult | None:
    """Разобрать stdout worker по контракту; `None`, если ответ не соответствует ему целиком."""
    try:
        decoded: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    files_obj = decoded.get("files")
    if not isinstance(files_obj, dict):
        return None
    files: dict[str, FileFacts] = {}
    for key, value in files_obj.items():
        facts = _file_facts(value)
        if not isinstance(key, str) or facts is None:
            return None
        files[key] = facts
    return {"files": files}


def _read_stdout(stream: IO[bytes], output_queue: queue.Queue[bytes | None]) -> None:
    """Читать stdout worker кусками в очередь; `None` в очереди означает конец потока."""
    try:
        while True:
            chunk = stream.read(STDOUT_CHUNK_BYTES)
            if not chunk:
                break
            output_queue.put(chunk)
    finally:
        output_queue.put(None)


def _read_stderr_tail(stream: IO[bytes], tail: bytearray) -> None:
    """Читать stderr worker до конца, сохраняя только последние `STDERR_TAIL_BYTES` байт."""
    try:
        while True:
            chunk = stream.read(STDOUT_CHUNK_BYTES)
            if not chunk:
                break
            tail.extend(chunk)
            del tail[:-STDERR_TAIL_BYTES]
    except (OSError, ValueError):
        return


def _write_stdin(stream: IO[bytes], payload: bytes) -> None:
    """Записать запрос в stdin worker и закрыть поток, игнорируя разрыв канала."""
    try:
        stream.write(payload)
    except OSError:
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _collect_stdout(
    output_queue: queue.Queue[bytes | None], deadline: float, max_output_bytes: int
) -> bytes | WorkerFailure:
    """Собрать stdout worker до конца потока с учётом срока и лимита объёма."""
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "parser subprocess exceeded time limit"
        try:
            item = output_queue.get(timeout=remaining)
        except queue.Empty:
            return "parser subprocess exceeded time limit"
        if item is None:
            return b"".join(chunks)
        chunks.append(item)
        total += len(item)
        if total > max_output_bytes:
            return "parser subprocess exceeded output size limit"


def _record_failure(error_log: Path | None, reason: WorkerFailure, stderr_tail: bytes) -> None:
    """Записать причину сбоя и хвост stderr worker в диагностический лог, если он задан."""
    if error_log is None:
        return
    try:
        error_log.parent.mkdir(parents=True, exist_ok=True)
        error_log.write_bytes(f"reason: {reason}\n".encode() + stderr_tail)
    except OSError:
        return


def run_bundle_parser(
    python_executable: str,
    worker_script: Path,
    install_dir: Path,
    request: dict[str, object],
    *,
    timeout_seconds: int,
    max_output_bytes: int,
    expected_script_sha256: str,
    error_log: Path | None = None,
) -> BundleParseResult | WorkerFailure:
    """Запустить `worker_script` в subprocess и передать ему `request` как JSON через stdin.

    Запуск ограничен по времени (`timeout_seconds`) и по объёму stdout (`max_output_bytes`).
    Непосредственно перед запуском SHA-256 worker проверяется повторно, что закрывает окно TOCTOU после
    проверки при поиске bundle. Stdout и stderr читаются в фоновых потоках, а stdin пишется в третьем,
    поэтому запрос больше буфера канала ОС не приводит к взаимной блокировке с worker. Исключений не
    бросает: любой сбой становится `WorkerFailure`, а хвост stderr попадает в `error_log`.
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
            stderr=subprocess.PIPE,
        )
    except OSError:
        return "parser subprocess failed"
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    deadline = time.monotonic() + timeout_seconds
    output_queue: queue.Queue[bytes | None] = queue.Queue()
    stderr_tail = bytearray()
    stderr_reader = threading.Thread(
        target=_read_stderr_tail, args=(proc.stderr, stderr_tail), daemon=True
    )
    threads = (
        threading.Thread(target=_read_stdout, args=(proc.stdout, output_queue), daemon=True),
        threading.Thread(
            target=_write_stdin, args=(proc.stdin, json.dumps(request).encode("utf-8")), daemon=True
        ),
        stderr_reader,
    )
    for thread in threads:
        thread.start()
    output = _collect_stdout(output_queue, deadline, max_output_bytes)
    result: BundleParseResult | WorkerFailure
    if isinstance(output, str):
        proc.kill()
        proc.wait()
        result = output
    else:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            result = "parser subprocess exceeded time limit"
        else:
            decoded = _decode_response(output) if proc.returncode == 0 else None
            result = decoded if decoded is not None else "parser subprocess failed"
    if isinstance(result, str):
        stderr_reader.join(STDERR_JOIN_SECONDS)
        _record_failure(error_log, result, bytes(stderr_tail))
    return result
