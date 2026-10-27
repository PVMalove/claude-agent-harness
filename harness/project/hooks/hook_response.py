#!/usr/bin/env python3
"""Конверт ответа управляемых hooks: JSON-вердикт, код возврата и профиль политики (issue #794).

Каждый управляемый hook событий перезапускается через этот модуль: ``hook_response.py run
<hook_id> <команда...>`` читает stdin, запускает исходный сценарий с тем же stdin и печатает вердикт
``{hook_id, profile, decision, reason_code, remedy, evidence_ref}`` последней строкой stderr.
Код возврата: 0 для allow/warn, 2 для block. JSON не пишется в stdout: Claude Code разбирает stdout
при коде 0 как собственный протокол, а значения ``allow``/``warn`` в нём не определены.

Профиль берётся только из ключа ``hook_policy`` файла ``.harness/project.json`` проекта, где лежит
этот модуль (``.claude/hooks/hook_response.py``). Переменные окружения, включая ``HOOK_PROFILE``, и
входные данные инструмента не читаются. Проект без ключа (и без файла) получает ``standard``; ключ,
который нельзя разобрать, или файл, который нельзя прочитать, даёт ``strict`` (только в строгую
сторону). Hook не знает роль и пути команды, поэтому не может проверить условия эскалации и применяет
самую строгую эскалацию ко всем вызовам: профиль становится не слабее базового и не слабее любой
эскалации (``hook_policy.resolve_profile`` для вызова с подходящими ролью и путями не выше этого).

Критичный hook при внутренней ошибке блокирует (код 2); зависание тела дольше ``INNER_TIMEOUT_SECONDS``
тоже внутренняя ошибка: процесс прерывается, критичный hook блокирует; поведенческий (``require-bounded-check``) и
учётные (``count-skill-usage``, ``mark-qa-gate-passed``) разрешают с предупреждением (код 0). Профиль
``minimal`` понижает блокировку поведенческого hook до предупреждения; критичные hooks блокируют в любом
профиле (ADR 0024). Advisory-hooks ChangeImpact (``ADVISORY_HOOKS``) не блокируют никогда: тело
предупреждает префиксом причины при коде 0, а в профиле вне ``ACTIVE_PROFILES`` тело не запускается.
Только стандартная библиотека, Python 3.9+.
"""

from __future__ import annotations

import json
import os
import re
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
    "protect-hook-policy": ("hook-policy-protected", True),
    "require-bounded-check": ("unbounded-check", False),
    "count-skill-usage": ("ok", False),
    "mark-qa-gate-passed": ("ok", False),
    "impact-first-touch": ("risky-path-first-touch", False),
    "targeted-check-reminder": ("targeted-checks-unreported", False),
    "sensitive-path-recheck": ("sensitive-path-touched", False),
}
# Advisory-hooks ChangeImpact (ADR 0024, раздел 2a): тело всегда завершается кодом 0 и называет
# предупреждение префиксом ``[reason_code] `` первой строки stderr, строка ``evidence_ref=<путь>``
# называет evidence. Конверт никогда их не блокирует. Hook работает только в профилях
# ``ACTIVE_PROFILES``: в остальных конверт отвечает allow, не запуская тело.
ACTIVE_PROFILES: Dict[str, Tuple[str, ...]] = {
    "impact-first-touch": ("standard", "strict"),
    "targeted-check-reminder": ("standard", "strict"),
    "sensitive-path-recheck": ("strict",),
}
ADVISORY_HOOKS = tuple(ACTIVE_PROFILES)
EVIDENCE_PREFIX = "evidence_ref="
# Дополнительные причины блокировки: сценарий называет одну из них префиксом ``[reason_code] `` stderr.
EXTRA_REASONS: Dict[str, Tuple[str, ...]] = {
    "protect-hook-policy": (
        "hook-policy-changed",
        "hook-policy-unparseable",
        "project-json-shell-write",
        "agent-config-edit",
    ),
}
PROFILES = ("minimal", "standard", "strict")
DEFAULT_PROFILE = "standard"
STRICT_PROFILE = "strict"
DECISIONS = ("allow", "warn", "block")
REASON_OK = "ok"
REASON_INTERNAL_ERROR = "internal-error"
REASON_CODES = tuple(
    dict.fromkeys(
        [
            REASON_OK,
            REASON_INTERNAL_ERROR,
            *(reason for reason, _ in HOOKS.values()),
            *(reason for reasons in EXTRA_REASONS.values() for reason in reasons),
        ]
    )
)
REASON_PREFIX = re.compile(r"\[([a-z][a-z-]*)\] ")
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
# Меньше тайм-аута hook-а в runtime (60 с): при зависании тела вердикт выдаёт конверт, а не runtime.
INNER_TIMEOUT_SECONDS = 50


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


def _policy_profile(policy: object) -> str:
    """Самый строгий профиль политики: базовый и все эскалации; неразбираемая политика — ``strict``."""
    if not isinstance(policy, dict):
        return STRICT_PROFILE
    escalations = policy.get("escalations", [])
    if policy.get("profile") not in PROFILES or not isinstance(escalations, list):
        return STRICT_PROFILE
    profiles: List[str] = [policy["profile"]]
    for item in escalations:
        if not isinstance(item, dict) or item.get("profile") not in PROFILES:
            return STRICT_PROFILE
        profiles.append(item["profile"])
    return max(profiles, key=PROFILES.index)


def read_profile(root: Path) -> str:
    """Действующий профиль из ``hook_policy``: нет файла или ключа — ``standard``; иначе самый строгий
    из базового и эскалаций; файл, который нельзя прочитать или разобрать, и неразбираемый ключ —
    ``strict``."""
    try:
        raw = (root / ".harness" / "project.json").read_text("utf-8")
    except FileNotFoundError:
        return DEFAULT_PROFILE
    except (OSError, ValueError):
        return STRICT_PROFILE
    try:
        data = json.loads(raw)
    except ValueError:
        return STRICT_PROFILE
    if not isinstance(data, dict):
        return STRICT_PROFILE
    if "hook_policy" not in data:
        return DEFAULT_PROFILE
    return _policy_profile(data["hook_policy"])


def decide_advisory(
    hook_id: str, profile: str, code: Optional[int], stderr: str
) -> Dict[str, object]:
    """Вердикт advisory-hook: код 0 с его префиксом — warn, без префикса — allow, иначе warn
    ``internal-error``. Advisory-hook не блокирует ни в одном профиле."""
    reason = HOOKS[hook_id][0]
    evidence = f".claude/hooks/{hook_id}.sh"
    lines = stderr.strip().splitlines()
    if code == 0:
        named = REASON_PREFIX.match(lines[0]) if lines else None
        if named is None or named.group(1) != reason:
            return build_response(hook_id, profile, "allow", REASON_OK)
        refs = [
            line[len(EVIDENCE_PREFIX) :].strip()
            for line in lines[1:]
            if line.startswith(EVIDENCE_PREFIX)
        ]
        remedy = lines[0][named.end() :][:REMEDY_LIMIT]
        return build_response(
            hook_id, profile, "warn", reason, remedy, refs[0] if refs else evidence
        )
    detail = f"hook вернул код {code}" if code is not None else "hook не отработал"
    if lines:
        detail = f"{detail}: {stderr.strip()}"
    return build_response(
        hook_id, profile, "warn", REASON_INTERNAL_ERROR, detail[:REMEDY_LIMIT], evidence
    )


def decide(
    hook_id: str, profile: str, code: Optional[int], stderr: str
) -> Dict[str, object]:
    """Вердикт по коду возврата исходного сценария (``None`` — сценарий не запустился)."""
    if hook_id in ADVISORY_HOOKS:
        return decide_advisory(hook_id, profile, code, stderr)
    block_reason, critical = HOOKS[hook_id]
    named = REASON_PREFIX.match(stderr.lstrip())
    if named and named.group(1) in EXTRA_REASONS.get(hook_id, ()):
        block_reason = named.group(1)
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
    reason = f"hook вернул код {code}" if code is not None else "hook не отработал"
    if remedy:
        reason = f"{reason}: {remedy}"
    decision = "block" if critical else "warn"
    return build_response(
        hook_id, profile, decision, REASON_INTERNAL_ERROR, reason, evidence
    )


def run_inner(
    command: Sequence[str], payload: bytes
) -> Tuple[Optional[int], bytes, bytes]:
    """Запустить исходный сценарий с тем же stdin; код — ``None`` при сбое запуска или тайм-ауте."""
    env = dict(os.environ, **{ENVELOPE_MARKER: "1"})
    try:
        done = subprocess.run(
            list(command),
            input=payload,
            capture_output=True,
            env=env,
            check=False,
            timeout=INNER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        message = f"тайм-аут {INNER_TIMEOUT_SECONDS} с: hook прерван"
        return None, b"", message.encode("utf-8")
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
        if profile not in ACTIVE_PROFILES.get(hook_id, PROFILES):
            return emit(b"", b"", build_response(hook_id, profile, "allow", REASON_OK))
        code, out, err = run_inner(argv[2:], payload)
        response = decide(hook_id, profile, code, err.decode("utf-8", "replace"))
        return emit(out, err, response)
    except Exception as exc:  # критичный hook не должен открывать проход при сбое
        print(f"hook_response.py: внутренняя ошибка: {exc}", file=sys.stderr)
        return 2 if critical else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
