"""VerificationPlan: стадии проверки роли по ImpactSet (ADR 0023, разделы 1 и 2).

План строится при ``dispatch propose`` из замороженной политики ``verification_plan_policy`` и
ImpactSet кандидата. Стадия pytest получает выбор ``focused`` по затронутым тестам или ``full``,
если влияние неизвестно либо роль — QA. Модуль только строит данные и их digest; исполняет план
другой компонент.
"""

from __future__ import annotations

import hashlib
import json
import platform
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from harness.repo_map.backend import ParserBackend
from harness.repo_map.change_impact import ImpactSet, compute_change_impact
from harness.repo_map.git_source import blob_contents
from harness.repo_map.policy import RepoMapPolicy

from . import operational_guards
from .core.constants import VERIFICATION_PLAN_SELECTIONS, VERIFICATION_STAGE_KINDS
from .core.utils import CoordinatorError, JsonObject

# Роли, для которых строится план: первый developer без diff плана не получает.
PLAN_ROLES = ("developer", "code-review", "qa")
PYTEST_KIND = "pytest"
IMPACT_NOT_COVERED = "impact_not_covered"
# Таймаут одной git-команды при чтении манифестов окружения.
_GIT_TIMEOUT_SECONDS = 60


def environment_fingerprint(
    repo: Path, commit: str, runtime_images: Mapping[str, str | None] | None = None
) -> str:
    """sha256 окружения проверки: интерпретатор, ``uv.lock``, секции ``[tool.*]`` и образы.

    Манифесты читаются из коммита, а не из рабочего дерева. Отсутствующий файл даёт пустой вклад.
    ``runtime_images`` — digest образов docker-dispatch из brief; без образа (native-запуск) ключ
    не входит в хеш, и fingerprint совпадает с прежним (ADR 0023, раздел 2).
    """
    files = blob_contents(
        repo, commit, ["uv.lock", "pyproject.toml"], _GIT_TIMEOUT_SECONDS
    )
    lock = files["uv.lock"] or b""
    tool: object = {}
    pyproject = files["pyproject.toml"]
    if pyproject is not None:
        try:
            tool = tomllib.loads(pyproject.decode("utf-8")).get("tool", {})
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            tool = {"unparsed": hashlib.sha256(pyproject).hexdigest()}
    parts: dict[str, object] = {
        "interpreter": platform.python_version(),
        "uv_lock": hashlib.sha256(lock).hexdigest(),
        "pyproject_tool": tool,
    }
    if runtime_images is not None:
        parts["runtime_images"] = dict(runtime_images)
    return _digest(parts)


def _digest(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_sha256(plan: Mapping[str, object]) -> str:
    """Канонический digest плана: порядок ключей не важен, а любой изменённый байт меняет digest.

    Это тот же digest, который approval связывает с переходом как ``verification_plan_sha256``.
    """
    return operational_guards.verification_plan_digest(plan)


STAGE_FIELDS = frozenset(
    {
        "stage_id",
        "kind",
        "argv",
        "timeout_seconds",
        "selection",
        "nodeids",
        "environment_fingerprint",
    }
)


def plan_problems(plan: object) -> list[str]:
    """Нарушения формы плана в brief; пусто, если план корректен."""
    if not isinstance(plan, dict):
        return ["verification_plan must be an object"]
    problems: list[str] = []
    if plan.get("role") not in PLAN_ROLES:
        problems.append(
            f"verification_plan.role must be one of: {', '.join(PLAN_ROLES)}"
        )
    stages = plan.get("stages")
    if not isinstance(stages, list) or not stages:
        return [*problems, "verification_plan.stages must be a non-empty list"]
    for index, stage in enumerate(stages):
        where = f"verification_plan.stages[{index}]"
        if not isinstance(stage, dict) or set(stage) != STAGE_FIELDS:
            problems.append(
                f"{where} must have exactly: {', '.join(sorted(STAGE_FIELDS))}"
            )
            continue
        if stage["kind"] not in VERIFICATION_STAGE_KINDS:
            problems.append(f"{where}.kind is not a registered stage kind")
        if stage["selection"] not in VERIFICATION_PLAN_SELECTIONS:
            problems.append(f"{where}.selection is not a registered selection")
        if not isinstance(stage["nodeids"], list) or not all(
            isinstance(item, str) for item in stage["nodeids"]
        ):
            problems.append(f"{where}.nodeids must be a list of strings")
    return problems


def _stage(
    template: Mapping[str, object],
    impact: ImpactSet,
    *,
    widen: bool,
    fingerprint: str,
) -> JsonObject:
    """Стадия плана по шаблону политики: выбор тестов применяется только к стадии pytest."""
    argv = list(cast(Sequence[str], template["argv"]))
    selection, nodeids = "full", []
    if template["kind"] == PYTEST_KIND and not widen:
        selection, nodeids = "focused", list(impact.related_tests)
        argv += nodeids
    return {
        "stage_id": template["stage_id"],
        "kind": template["kind"],
        "argv": argv,
        "timeout_seconds": template["timeout_seconds"],
        "selection": selection,
        "nodeids": nodeids,
        "environment_fingerprint": fingerprint,
    }


def uncovered_tests(plan: Mapping[str, object]) -> list[str]:
    """Связанные тесты ImpactSet, которые план не выбирает (ADR 0023, раздел 6).

    Тесты покрыты, если любая стадия pytest имеет выбор ``full`` либо ``nodeids`` перечисляют сам
    тестовый файл целиком: nodeid внутри файла (``file::test``) файл не покрывает.
    """
    impact = cast(Mapping[str, Sequence[str]], plan["impact"])
    related = list(impact["related_tests"])
    stages = [
        stage
        for stage in cast(Sequence[Mapping[str, object]], plan["stages"])
        if stage["kind"] == PYTEST_KIND
    ]
    if any(stage["selection"] == "full" for stage in stages):
        return []
    selected = {
        nodeid for stage in stages for nodeid in cast(Sequence[str], stage["nodeids"])
    }
    return [path for path in related if path not in selected]


def require_covered(plan: Mapping[str, object]) -> None:
    """Отказать ``impact_not_covered``, если план не выбирает связанный тест ImpactSet.

    Это машинная проверка dispatch propose и create для code-review и qa; текст отчёта роли её
    не заменяет.
    """
    missing = uncovered_tests(plan)
    if missing:
        raise CoordinatorError(
            f"{IMPACT_NOT_COVERED}: the verification plan does not select "
            f"{len(missing)} related test(s): {', '.join(missing)}",
            remedy="widen the pytest stage selection to full or select every related test "
            "of the impact set, then propose the dispatch again",
        )


def build_plan(
    repo: Path,
    policy: Mapping[str, object],
    *,
    base: str,
    candidate: str,
    role: str,
    backend: ParserBackend | None = None,
    repo_map_policy: RepoMapPolicy | None = None,
    runtime_images: Mapping[str, str | None] | None = None,
) -> JsonObject:
    """План проверки роли для diff ``base..candidate`` по замороженной ``policy``.

    Стадия pytest получает ``full`` при неизвестном влиянии (конфиг сборки, ``conftest.py``,
    удалённый или переименованный модуль, порог ``max_related_tests``, недоступный парсер) и для
    QA, иначе ``focused`` по ``related_tests`` без отсечения. ``focused`` без тестов не выбирает
    ничего: исполнитель отчитывается ``not_run``, а не запускает весь набор.
    """
    if role not in PLAN_ROLES:
        raise CoordinatorError(
            f"a verification plan is not built for role {role!r}",
            remedy=f"build a plan only for: {', '.join(PLAN_ROLES)}",
        )
    threshold = cast(int, policy["max_related_tests"])
    impact = compute_change_impact(
        repo,
        base,
        candidate,
        backend=backend,
        policy=repo_map_policy,
        max_related_tests=threshold,
    )
    fingerprint = environment_fingerprint(repo, candidate, runtime_images)
    widen = role == "qa" or bool(impact.unknown)
    return {
        "base_sha": base,
        "candidate_sha": candidate,
        "role": role,
        "max_related_tests": threshold,
        "impact": impact.to_json(),
        "stages": [
            _stage(template, impact, widen=widen, fingerprint=fingerprint)
            for template in cast(Sequence[Mapping[str, object]], policy["stages"])
        ],
    }
