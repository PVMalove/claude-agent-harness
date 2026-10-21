#!/usr/bin/env python3
"""Конверт ответа управляемых hooks: JSON-вердикт, код возврата и профиль политики (issue #794).

Каждый управляемый hook событий перезапускается через этот модуль: ``hook_response.py run
<hook_id> <команда...>`` читает stdin, запускает исходный сценарий с тем же stdin и печатает вердикт
``{hook_id, profile, decision, reason_code, remedy, evidence_ref}`` последней строкой stderr.
Код возврата: 0 для allow/warn, 2 для block. JSON не пишется в stdout: Claude Code разбирает stdout
при коде 0 как собственный протокол, а значения ``allow``/``warn`` в нём не определены.

Профиль берётся только из ключа ``hook_policy.profile`` файла ``.harness/project.json`` проекта, где
лежит этот модуль (``.claude/hooks/hook_response.py``). Переменные окружения, включая
``HOOK_PROFILE``, и входные данные инструмента не читаются. Проект без ключа получает ``standard``.
Эскалации по ролям и путям здесь не вычисляются: hook не знает роль, а пути команды неизвестны.

Критичный hook при внутренней ошибке блокирует (код 2); поведенческий (``require-bounded-check``) и
учётные (``count-skill-usage``, ``mark-qa-gate-passed``) разрешают с предупреждением (код 0). Профиль
``minimal`` понижает блокировку поведенческого hook до предупреждения; критичные hooks блокируют в любом
профиле (ADR 0024). Только стандартная библиотека, Python 3.9+.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# hook_id -> (reason_code блокировки, критичность); ``ok`` — hook не блокирует по замыслу.
# Значения зарегистрированы в systemFields.v1 (hook.id, hook.reason_code);
# tests/orchestration/test_system_fields_contract.py сверяет их.
HOOKS: Dict[str, Tuple[str, bool]] = {
    "block-dangerous-git": ("dangerous-git", True),
    "block-direct-master": ("direct-commit", True),
    "block-pr-merge": ("pr-merge", True),
    "block-public-attribution": ("public-attribution", True),
    "check-branch-name": ("branch-name", True),
    "check-worktree-branch-name": ("worktree-branch-name", True),
    "block-scratch-outside-docs-tasks": ("scratch-location", True),
    "require-qa-gate": ("qa-gate-missing", True),
    "require-bounded-check": ("unbounded-check", False),
    "count-skill-usage": ("ok", False),
    "mark-qa-gate-passed": ("ok", False),
}
PROFILES = ("minimal", "standard", "strict")
DEFAULT_PROFILE = "standard"
DECISIONS = ("allow", "warn", "block")
REASON_OK = "ok"
REASON_INTERNAL_ERROR = "internal-error"
REASON_CODES = tuple(
    dict.fromkeys(
        [REASON_OK, REASON_INTERNAL_ERROR, *(reason for reason, _ in HOOKS.values())]
    )
)
ENVELOPE_FIELDS = (
    "hook_id",
    "profile",
    "decision",
    "reason_code",
    "remedy",
    "evidence_ref",
)
EXIT_CODES = {"allow": 0, "warn": 0, "block": 2}
# Маркер повторного запуска: исходный сценарий видит его и не оборачивает себя второй раз.
ENVELOPE_MARKER = "HARNESS_HOOK_ENVELOPE"
REMEDY_LIMIT = 1000


def build_response(
    hook_id: str,
    profile: str,
    decision: str,
    reason_code: str,
    remedy: str = "",
    evidence_ref: Optional[str] = None,
) -> Dict[str, object]:
    """Собрать вердикт; незарегистрированное значение — ошибка контракта (ValueError)."""
    for field, value, allowed in (
        ("hook_id", hook_id, tuple(HOOKS)),
        ("profile", profile, PROFILES),
        ("decision", decision, DECISIONS),
        ("reason_code", reason_code, REASON_CODES),
    ):
        if value not in allowed:
            raise ValueError(
                f"{field} {value!r} is not registered; allowed: {', '.join(allowed)}"
            )
    return {
        "hook_id": hook_id,
        "profile": profile,
        "decision": decision,
        "reason_code": reason_code,
        "remedy": remedy,
        "evidence_ref": evidence_ref,
    }


def project_root() -> Path:
    """Корень проекта по расположению модуля: ``<проект>/.claude/hooks/hook_response.py``."""
    return Path(__file__).resolve().parents[2]


def read_profile(root: Path) -> str:
    """Базовый профиль из ``hook_policy.profile``; всё нечитаемое или неизвестное — ``standard``."""
    try:
        data = json.loads((root / ".harness" / "project.json").read_text("utf-8"))
        profile = data["hook_policy"]["profile"]
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_PROFILE
    return profile if profile in PROFILES else DEFAULT_PROFILE


def decide(
    hook_id: str, profile: str, code: Optional[int], stderr: str
) -> Dict[str, object]:
    """Вердикт по коду возврата исходного сценария (``None`` — сценарий не запустился)."""
    block_reason, critical = HOOKS[hook_id]
    remedy = stderr.strip()[:REMEDY_LIMIT]
    evidence = f".claude/hooks/{hook_id}.sh"
    if code == 0:
        return build_response(hook_id, profile, "allow", REASON_OK)
    if code == 2 and block_reason != REASON_OK:
        if not critical and profile == "minimal":
            note = "профиль minimal понижает блокировку до предупреждения"
            return build_response(
                hook_id, profile, "warn", block_reason, f"{note}: {remedy}", evidence
            )
        return build_response(hook_id, profile, "block", block_reason, remedy, evidence)
    reason = f"hook вернул код {code}" if code is not None else "hook не запустился"
    decision = "block" if critical else "warn"
    return build_response(
        hook_id, profile, decision, REASON_INTERNAL_ERROR, reason, evidence
    )


def run_inner(
    command: Sequence[str], payload: bytes
) -> Tuple[Optional[int], bytes, bytes]:
    """Запустить исходный сценарий с тем же stdin; вернуть код (None при сбое запуска), stdout, stderr."""
    env = dict(os.environ, **{ENVELOPE_MARKER: "1"})
    try:
        done = subprocess.run(
            list(command),
            input=payload,
            capture_output=True,
            env=env,
            check=False,
        )
    except OSError as exc:
        return None, b"", f"{exc}".encode("utf-8", "replace")
    return done.returncode, done.stdout, done.stderr


def emit(stdout: bytes, stderr: bytes, response: Dict[str, object]) -> int:
    """Передать вывод исходного сценария и вердикт; вернуть код возврата по decision."""
    if stdout:
        sys.stdout.buffer.write(stdout)
        sys.stdout.buffer.flush()
    line = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    if stderr and not stderr.endswith(b"\n"):
        stderr += b"\n"
    sys.stderr.buffer.write(stderr + line.encode("utf-8") + b"\n")
    sys.stderr.buffer.flush()
    decision = response["decision"]
    assert isinstance(decision, str)
    return EXIT_CODES[decision]


def main(argv: List[str]) -> int:
    """``run <hook_id> <команда...>``; любой сбой самого конверта подчиняется критичности hook."""
    if len(argv) < 3 or argv[0] != "run" or argv[1] not in HOOKS:
        print(
            "hook_response.py: ожидается 'run <hook_id> <команда...>' с зарегистрированным hook_id",
            file=sys.stderr,
        )
        return 2
    hook_id = argv[1]
    critical = HOOKS[hook_id][1]
    try:
        payload = sys.stdin.buffer.read()
        profile = read_profile(project_root())
        code, out, err = run_inner(argv[2:], payload)
        response = decide(hook_id, profile, code, err.decode("utf-8", "replace"))
        return emit(out, err, response)
    except Exception as exc:  # критичный hook не должен открывать проход при сбое
        print(f"hook_response.py: внутренняя ошибка: {exc}", file=sys.stderr)
        return 2 if critical else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
