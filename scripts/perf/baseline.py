"""Базовый замер производительности 1.x: история `/implement` из ledger и время команд."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import NotRequired, TypedDict

from harness.json_types import JsonObject

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_STAGE_LINE = re.compile(
    r"^\[verify\] (?P<stage>\S+): (?P<status>passed|failed) in (?P<seconds>\d+(?:\.\d+)?)s$",
    re.MULTILINE,
)


class Sample(TypedDict):
    """Один прогон команды: время по часам, код возврата и хвост stderr при отказе."""

    seconds: float
    returncode: int
    stderr_tail: NotRequired[list[str]]
    summary_line: NotRequired[str]


def redact(text: str, replacements: dict[str, str]) -> str:
    """Убрать ANSI-коды и заменить локальные пути плейсхолдерами в порядке `replacements`."""
    text = _ANSI.sub("", text)
    for path, placeholder in replacements.items():
        text = text.replace(path, placeholder)
    return text


def percentiles(samples: Sequence[float]) -> dict[str, float | int]:
    """Вернуть `n`, `min`, `p50`, `p95`, `p99`, `max` выборки методом nearest-rank.

    p-й перцентиль — значение с рангом ⌈p·n/100⌉ в упорядоченной выборке. Пустая выборка —
    отсутствие замера, а не нули, поэтому `ValueError`."""
    if not samples:
        raise ValueError("percentiles of an empty sample")
    ordered = sorted(samples)
    n = len(ordered)
    result: dict[str, float | int] = {"n": n, "min": ordered[0]}
    for p in (50, 95, 99):
        result[f"p{p}"] = ordered[math.ceil(p * n / 100) - 1]
    result["max"] = ordered[-1]
    return result


def is_full_run(command: str) -> bool:
    """Ответить, запускает ли команда проверки полный набор тестов.

    Полный прогон — `scripts/verify.py` без `--only`, `make verify` или pytest по всему каталогу
    `tests` (или без путей: `testpaths = ["tests"]`) без опций отбора `-k`, `-m`, `--lf`.
    Команда делится по `&&`, `||`, `;`, `|`, а `bash -c`/`bash -lc` раскрывается. Команду, которую
    нельзя разобрать, полным прогоном не считаем: число полных прогонов — оценка снизу."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return any(_segment_is_full_run(segment) for segment in _segments(words))


_SEPARATORS = frozenset({"&&", "||", ";", "|"})
_PYTEST_VALUE_OPTIONS = frozenset(
    {"-n", "-p", "-o", "-c", "-W", "--rootdir", "--maxfail", "--dist", "--basetemp"}
)
_PYTEST_SELECTION_OPTIONS = frozenset(
    {"-k", "-m", "--deselect", "--lf", "--last-failed", "--sw", "--stepwise"}
)


def _segments(words: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for word in words:
        if word in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(word)
    return segments


def _segment_is_full_run(words: list[str]) -> bool:
    while words and "=" in words[0] and not words[0].startswith("-"):
        words = words[1:]
    if (
        len(words) >= 3
        and Path(words[0]).name in {"bash", "sh"}
        and words[1] in {"-c", "-lc"}
    ):
        return is_full_run(words[2])
    if words[:2] == ["make", "verify"]:
        return True
    for index, word in enumerate(words):
        if word.endswith("scripts/verify.py"):
            rest = words[index + 1 :]
            return not any(arg == "--only" or arg.startswith("--only=") for arg in rest)
    if "pytest" in words:
        return _pytest_runs_whole_suite(words[words.index("pytest") + 1 :])
    return False


def _pytest_runs_whole_suite(args: list[str]) -> bool:
    targets: list[str] = []
    takes_value = False
    for arg in args:
        if takes_value:
            takes_value = False
        elif arg.split("=", 1)[0] in _PYTEST_SELECTION_OPTIONS:
            return False
        elif arg in _PYTEST_VALUE_OPTIONS:
            takes_value = True
        elif not arg.startswith("-"):
            targets.append(arg.rstrip("/"))
    return targets in ([], ["tests"])


def implement_runs(generation: Path) -> list[dict[str, object]]:
    """Собрать по каждому batch поколения ledger длительность и число полных прогонов.

    Длительность — от `created_at` batch до последнего `updated_at` статусов его dispatch; без
    статусов она `None` («нет данных»), а не 0. Полные прогоны считаются по `checks_run` отчётов
    dispatch с разбивкой по ролям. Batch упорядочены по `created_at`."""
    finished: dict[str, str] = {}
    full_runs: dict[str, Counter[str]] = {}
    for path in sorted((generation / "dispatches").glob("*.json")):
        dispatch = _read_json(path)
        batch_id, dispatch_id = dispatch["batch_id"], dispatch["dispatch_id"]
        status_path = generation / "dispatch-status" / f"{dispatch_id}.json"
        if status_path.is_file():
            updated_at = _read_json(status_path)["updated_at"]
            if batch_id not in finished or _parse_time(updated_at) > _parse_time(
                finished[batch_id]
            ):
                finished[batch_id] = updated_at
        report_path = generation / "reports" / f"{dispatch_id}.json"
        if report_path.is_file():
            commands = [
                str(check.get("command", ""))
                for check in _read_json(report_path).get("checks_run") or []
            ]
            count = sum(1 for command in commands if is_full_run(command))
            if count:
                full_runs.setdefault(batch_id, Counter())[dispatch["role"]] += count
    runs: list[dict[str, object]] = []
    for path in (generation / "batches").glob("*.json"):
        batch = _read_json(path)
        batch_id = batch["batch_id"]
        finished_at = finished.get(batch_id)
        duration = (
            (
                _parse_time(finished_at) - _parse_time(batch["created_at"])
            ).total_seconds()
            if finished_at is not None
            else None
        )
        runs.append(
            {
                "batch_id": batch_id,
                "ticket": batch["ticket"],
                "state": batch["state"],
                "created_at": batch["created_at"],
                "finished_at": finished_at,
                "duration_seconds": duration,
                "full_runs": dict(full_runs.get(batch_id, {})),
            }
        )
    return sorted(runs, key=lambda run: _parse_time(str(run["created_at"])))


def local_qa_results(generation: Path) -> list[dict[str, object]]:
    """Вернуть записанные результаты integration local QA: время, команды и длительность."""
    results = []
    for path in sorted(
        (generation / "reports" / "integration-local-qa").glob("*.json")
    ):
        record = _read_json(path)
        if record.get("event") != "result":
            continue
        commands = list(record.get("verification_commands") or [])
        results.append(
            {
                "local_qa_id": record.get("local_qa_id"),
                "finished_at": record.get("finished_at"),
                "state": record.get("state"),
                "target_sha": record.get("target_sha"),
                "duration_seconds": record.get("duration_seconds"),
                "commands": commands,
                "full_run": any(is_full_run(command) for command in commands),
            }
        )
    return sorted(results, key=lambda result: str(result["finished_at"]))


def qa_artifact_stages(generation: Path) -> dict[str, dict[str, StageResult]]:
    """Вернуть длительности стадий `scripts/verify.py` из QA-логов поколения."""
    stages = {}
    for path in sorted((generation / "qa-artifacts").glob("*.log")):
        found = verify_stage_durations(
            path.read_text(encoding="utf-8", errors="replace")
        )
        if found:
            stages[path.name] = found
    return stages


# State ledger внутри `--repo`: QA lane не принимает `--state-dir`, поэтому state копируется туда.
STATE_REL = Path(".harness/orchestration/state")

# Команда, первый прогон которой дольше SLOW_SECONDS, получает `--slow-repeats` повторов вместо
# `--repeats`: на копии state этого репозитория `dispatch status` идёт около 250 с.
SLOW_SECONDS = 10.0
COORDINATOR_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ledger validate", ("ledger", "validate")),
    ("ledger status", ("ledger", "status")),
    ("ledger lock-status", ("ledger", "lock-status")),
    ("batch list", ("batch", "list")),
    ("qa status", ("qa", "status")),
    ("dispatch status", ("dispatch", "status")),
)

# Focused-набор — команды проверки из ADR 0023 без cargo и `make verify`.
FOCUSED_TESTS = (
    "tests/orchestration/test_verification_profiles.py",
    "tests/orchestration/test_retry_verification.py",
    "tests/core/test_gate_runner.py",
    "tests/orchestration/test_qa_lane.py",
    "tests/scripts/test_verify_temp.py",
)

# Failed-only: pytest `--lf --lfnf=none` по всему `tests`, в `lastfailed` записан qa_gate-тест.
FAILED_NODE_IDS = (
    "tests/orchestration/test_verification_profiles.py::VerificationProfileBatchTests::"
    "test_a_profile_covering_every_allowed_path_is_frozen_in_the_batch",
)


def time_command(
    argv: Sequence[str], *, cwd: Path, repeats: int, keep_summary_line: bool = False
) -> list[Sample]:
    """Запустить команду до `repeats` раз и вернуть время и код возврата каждого прогона.

    После первого отказа повторы прекращаются: отказ воспроизводим и времени не меряет.
    `keep_summary_line` сохраняет последнюю строку stdout — итог pytest с числом тестов."""
    samples: list[Sample] = []
    for _ in range(repeats):
        started = time.perf_counter()
        completed = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, check=False
        )
        sample: Sample = {
            "seconds": time.perf_counter() - started,
            "returncode": completed.returncode,
        }
        if keep_summary_line:
            sample["summary_line"] = (completed.stdout.strip().splitlines() or [""])[-1]
        if completed.returncode != 0:
            sample["stderr_tail"] = completed.stderr.strip().splitlines()[-3:]
            samples.append(sample)
            break
        samples.append(sample)
    return samples


def summarize(samples: Sequence[Sample]) -> dict[str, object]:
    """Перцентили только по успешным прогонам; без успешных — `not_measured`."""
    ok = [sample["seconds"] for sample in samples if sample["returncode"] == 0]
    if not ok:
        return {"status": "not_measured", "failed_runs": len(samples)}
    return {
        "status": "measured",
        "failed_runs": len(samples) - len(ok),
        **percentiles(ok),
    }


def _coordinator_timings(
    repo: Path, state: Path, *, repeats: int, slow_repeats: int
) -> dict[str, object]:
    base = [
        sys.executable,
        str(repo / "harness/orchestration/coordinator.py"),
        "--repo",
        str(repo),
    ]
    timings: dict[str, object] = {}
    for name, args in COORDINATOR_COMMANDS:
        timings[name] = _timed_entry(
            [*base, *args], cwd=repo, repeats=repeats, slow_repeats=slow_repeats
        ) | {"argv": list(args)}
    working, live_state = _working_dispatch(state), "recorded"
    if working is None:
        working, live_state = _make_dispatch_live(state), "synthetic"
    if working is None:
        timings["dispatch heartbeat"] = {
            "argv": ["dispatch", "heartbeat"],
            "summary": {"status": "not_run", "reason": "no reported dispatch"},
        }
    else:
        args = ("dispatch", "heartbeat", "--dispatch", working)
        timings["dispatch heartbeat"] = _timed_entry(
            [*base, *args], cwd=repo, repeats=repeats, slow_repeats=slow_repeats
        ) | {"argv": list(args), "live_state": live_state}
    return timings


def _timed_entry(
    argv: Sequence[str], *, cwd: Path, repeats: int, slow_repeats: int
) -> dict[str, object]:
    samples = time_command(argv, cwd=cwd, repeats=1)
    if samples[0]["returncode"] == 0:
        slow = samples[0]["seconds"] > SLOW_SECONDS
        samples += time_command(
            argv, cwd=cwd, repeats=(slow_repeats if slow else repeats) - 1
        )
    return {"samples": samples, "summary": summarize(samples)}


def _audit_timing(repo: Path, state: Path, *, repeats: int) -> dict[str, object]:
    """Время `LifecycleLedger._validate_audit` измеряемого кода на audit-каталоге поколения."""
    audit = _generation(state) / "audit"
    probe = (
        "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "from harness.orchestration.ledger.lifecycle import LifecycleLedger; "
        "LifecycleLedger._validate_audit(Path(sys.argv[2]))"
    )
    samples = time_command(
        [sys.executable, "-c", probe, str(repo), str(audit)], cwd=repo, repeats=repeats
    )
    return {
        "records": sum(1 for _ in audit.glob("*.json")),
        "samples": samples,
        "summary": summarize(samples),
        "note": "время включает запуск интерпретатора и импорт lifecycle",
    }


def _make_dispatch_live(state: Path) -> str | None:
    """Перевести в копии state последний `reported`-статус в `working` для замера heartbeat.

    Heartbeat принимает только живой dispatch; путь записи (lock, перезапись статуса, audit)
    от исходного состояния не зависит."""
    statuses = [
        (path, _read_json(path))
        for path in (_generation(state) / "dispatch-status").glob("*.json")
    ]
    reported = [item for item in statuses if item[1].get("state") == "reported"]
    if not reported:
        return None
    path, status = max(reported, key=lambda item: _parse_time(item[1]["updated_at"]))
    path.write_text(json.dumps({**status, "state": "working"}), encoding="utf-8")
    return str(status["dispatch_id"])


def _working_dispatch(state: Path) -> str | None:
    for path in sorted((_generation(state) / "dispatch-status").glob("*.json")):
        status = _read_json(path)
        if status.get("state") == "working":
            return str(status["dispatch_id"])
    return None


def _pytest_timings(repo: Path, scratch: Path, *, repeats: int) -> dict[str, object]:
    # `-c` фиксирует rootdir, confcutdir и ini в измеряемом checkout: настройки
    # объемлющего проекта (addopts) не должны попадать в замер.
    pytest = [sys.executable, "-m", "pytest", "-q", "-c", str(repo / "pyproject.toml")]
    focused = time_command(
        [*pytest, *FOCUSED_TESTS], cwd=repo, repeats=repeats, keep_summary_line=True
    )
    failed_only = []
    for index in range(repeats):
        cache = scratch / f"pytest-cache-{index}"
        lastfailed = cache / "v" / "cache" / "lastfailed"
        lastfailed.parent.mkdir(parents=True)
        lastfailed.write_text(
            json.dumps(dict.fromkeys(FAILED_NODE_IDS, True)), encoding="utf-8"
        )
        argv = [*pytest, "-o", f"cache_dir={cache}", "--lf", "--lfnf=none", "tests"]
        failed_only.extend(
            time_command(argv, cwd=repo, repeats=1, keep_summary_line=True)
        )
    return {
        "focused": {
            "tests": list(FOCUSED_TESTS),
            "samples": focused,
            "summary": summarize(focused),
        },
        "failed_only": {
            "node_ids": list(FAILED_NODE_IDS),
            "samples": failed_only,
            "summary": summarize(failed_only),
        },
    }


def _full_verify(
    repo: Path, out: Path, replacements: dict[str, str]
) -> dict[str, object]:
    started = time.perf_counter()
    completed = subprocess.run(
        ["make", "verify"], cwd=repo, capture_output=True, text=True, check=False
    )
    seconds = time.perf_counter() - started
    (out / "make-verify.log").write_text(
        redact(completed.stdout + completed.stderr, replacements), encoding="utf-8"
    )
    return {
        "seconds": seconds,
        "returncode": completed.returncode,
        "stages": verify_stage_durations(completed.stdout),
        "log": "make-verify.log",
    }


def _generation(state: Path) -> Path:
    pointer = _read_json(state / "ledger.json")
    return state / "generations" / str(pointer["generation"])


def _environment(repo: Path) -> dict[str, object]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "commit": commit,
        "worktree_clean": not dirty,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
    }


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _dump(out: Path, name: str, data: object) -> None:
    (out / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="одноразовый чистый checkout измеряемого кода; state копируется в него",
    )
    parser.add_argument(
        "--state-dir", type=Path, required=True, help="state ledger (копируется)"
    )
    parser.add_argument("--out", type=Path, required=True, help="каталог сырых данных")
    parser.add_argument("--repeats", type=_positive_int, default=30)
    parser.add_argument("--slow-repeats", type=_positive_int, default=5)
    parser.add_argument("--test-repeats", type=_positive_int, default=3)
    parser.add_argument(
        "--full-verify", action="store_true", help="один прогон `make verify`"
    )
    args = parser.parse_args(argv)
    repo, out = args.repo.resolve(), args.out.resolve()
    state = repo / STATE_REL
    if state.exists():
        parser.error(f"{state} already exists; --repo must be a disposable checkout")
    out.mkdir(parents=True, exist_ok=True)
    replacements = {
        str(repo): "<repo>",
        str(args.state_dir.resolve().parents[2]): "<main-checkout>",
        str(Path.home()): "<home>",
    }
    started = time.perf_counter()
    _dump(out, "environment.json", _environment(repo))
    shutil.copytree(
        args.state_dir, state, ignore=shutil.ignore_patterns(".coordinator.lock")
    )
    try:
        generation = _generation(state)
        _dump(out, "implement-runs.json", implement_runs(generation))
        _dump(out, "local-qa.json", local_qa_results(generation))
        _dump(out, "qa-artifact-stages.json", qa_artifact_stages(generation))
        _dump(
            out,
            "ledger-size.json",
            {
                directory.name: sum(1 for _ in directory.iterdir())
                for directory in sorted(generation.iterdir())
                if directory.is_dir()
            },
        )
        _dump(out, "audit.json", _audit_timing(repo, state, repeats=args.slow_repeats))
        _dump(
            out,
            "coordinator.json",
            _coordinator_timings(
                repo, state, repeats=args.repeats, slow_repeats=args.slow_repeats
            ),
        )
        with tempfile.TemporaryDirectory(prefix="perf-baseline-") as tmp:
            _dump(
                out,
                "pytest.json",
                _pytest_timings(repo, Path(tmp), repeats=args.test_repeats),
            )
    finally:
        shutil.rmtree(state)
    if args.full_verify:
        _dump(out, "make-verify.json", _full_verify(repo, out, replacements))
    _dump(out, "run.json", {"elapsed_seconds": time.perf_counter() - started})
    return 0


def _read_json(path: Path) -> JsonObject:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class StageResult(TypedDict):
    """Итог стадии `scripts/verify.py`: `passed` или `failed` и время в секундах."""

    status: str
    seconds: float


def verify_stage_durations(log: str) -> dict[str, StageResult]:
    """Извлечь статус и длительность стадий из строк `[verify] <stage>: passed|failed in X.XXs`."""
    return {
        match["stage"]: {"status": match["status"], "seconds": float(match["seconds"])}
        for match in _STAGE_LINE.finditer(log)
    }


if __name__ == "__main__":
    raise SystemExit(main())
