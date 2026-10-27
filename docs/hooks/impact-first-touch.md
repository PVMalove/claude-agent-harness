# impact-first-touch.sh — первое касание рискованного пути

## 1. Название и локализация

- **Исходный хук:** [`harness/project/hooks/impact-first-touch.sh`](../../harness/project/hooks/impact-first-touch.sh)
  и общее тело [`change_impact_hooks.py`](../../harness/project/hooks/change_impact_hooks.py)
- **Русский перевод:** предупреждение о первом касании рискованного пути в операции.

## 2. Полное описание

Advisory-hook `PreToolUse(Write|Edit)` из матрицы ADR 0024, раздел 2a. Он читает
`tool_input.file_path` и предупреждает, когда агент впервые в операции правит путь, влияние которого
ChangeImpact не вычисляет по графу импортов: конфиг сборки и проверок (`pyproject.toml`, `uv.lock`,
`Makefile`, `conftest.py`, `.github/*`, `scripts/verify.py`, `scripts/verification/*`) или каталог
capabilities и skills (`harness/CAPABILITIES.json`, `skills/*`). Набор совпадает с правилами
`build-config-changed` и `capabilities-or-skills-changed` функции `path_unknown_code` в
`harness/repo_map/change_impact.py`. Правило `non-python-file` в набор не входит, иначе рискованной
стала бы каждая правка документа. Hook не видит пакет harness и держит копию правил;
`tests/core/test_change_impact_hooks.py` сверяет копию с источником.

Путь сравнивается после `realpath` относительно ближайшего вверх каталога с `.git` (каталога или
файла worktree), в posix-форме; на Windows сравнение не учитывает регистр. Путь вне git-checkout hook
пропускает без предупреждения.

Предупреждение получает модель: stdout содержит
`{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "<текст>"}}` без
`permissionDecision`, поэтому поток разрешений не меняется. Hook никогда не блокирует.

- **Профили.** `minimal` — hook не работает: конверт отвечает `allow`, тело не запускается и состояние
  не пишется. `standard` и `strict` — `warn` с кодом `risky-path-first-touch`.
- **Дедупликация.** Одно уведомление на путь в операции: это и есть первое касание, и повторная правка
  того же пути цикла предупреждений не создаёт. Операция — прогон агента: `operation_id` равен первым 32
  hex от sha256 массива `["hook-operation", session_id, agent_id или ""]`. Роль-субагент, внешний worker
  и coordinator получают разные операции, а новый dispatch, developer-retry и повтор роли — новую
  операцию и новое уведомление. Маркер `n-<key>.json` создаётся через `O_EXCL`, поэтому из параллельных
  вызовов предупреждает ровно один. Маркер одновременно служит evidence-записью: её путь стоит в
  `evidence_ref` вердикта. Хранилище и его границы описаны в
  [targeted-check-reminder.md](targeted-check-reminder.md).
- **Внутренняя ошибка.** Нечитаемый JSON, нет `session_id` или `tool_input.file_path`, сбой хранилища,
  любое исключение, ненулевой код и тайм-аут 10 с (`ADVISORY_TIMEOUT_SECONDS`) дают `warn`
  `internal-error` с кодом `0` и пустым stdout ([hook-response.md](hook-response.md)).
- **Второй runtime.** Правка файла приходит там как `apply_patch` без `file_path`, поэтому hook не
  подключён до адаптера payload (#830). `harness health` перечисляет его в строке `нет события или
  payload` с проверкой вне hook — ChangeImpact-gate по ADR 0023.

## 3. Контракт

- **Вход:** JSON события `PreToolUse` в stdin: `session_id`, `agent_id` (у субагента),
  `hook_event_name`, `tool_name`, `tool_input.file_path`, `cwd`; профиль — только `hook_policy` в
  `.harness/project.json`.
- **Выход:** код `0` всегда. Первое касание рискованного пути — `additionalContext` в stdout, первая
  строка stderr `[risky-path-first-touch] <текст>` и строка `evidence_ref=<путь>`; последняя строка
  stderr — вердикт конверта. Остальные вызовы — `allow` без stdout.
- **Состояние:** `.harness/.sandboxes/hook-events/<operation_id>/n-<key>.json`; hook не запускает
  тесты, `make` и `git`.

## Источник

[impact-first-touch.sh](../../harness/project/hooks/impact-first-touch.sh),
[change_impact_hooks.py](../../harness/project/hooks/change_impact_hooks.py),
[change_impact.py](../../harness/repo_map/change_impact.py)
