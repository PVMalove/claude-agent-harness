# test-author-guard.py — scoped guard субагента test-author

## 1. Название и локализация

- **Исходный скрипт:** [`harness/project/hooks/test-author-guard.py`](../../harness/project/hooks/test-author-guard.py)
- **Русский перевод:** защитный hook субагента тест-автора.

## 2. Полное описание

`test-author-guard.py` — hook события `PreToolUse` для `Write`, `Edit` и `Bash`, но он не входит в
`settings.local.json`: установка копирует его в `.claude/hooks/`, а подключает его только
frontmatter агента `.claude/agents/test-author.md`. Поэтому guard действует на вызовы субагента
test-author, а project-wide hooks (`block-scratch-outside-docs-tasks.sh`, `require-bounded-check.sh`
и остальные) остаются в силе и срабатывают так же, как для любого агента.

Guard разрешает ровно два вида вызовов:

- `Write` и `Edit` — путь после нормализации лежит внутри `test_paths`; symlink, ведущий за
  пределы зоны, и пути под `.git`, `.harness`, `.claude`, `.agents`, `.codex` отклоняются;
- `Bash` — один вызов `.claude/hooks/run-test.sh <test-id>` (допускается эквивалентный вызов через
  доверенный системный `bash`); shell-метасимволы, `--probe`, дополнительные аргументы и другие
  команды отклоняются.

Остальные инструменты получают отказ `tool_not_allowed`. Если payload пришёл от другого агента
(`agent_type` отличается от `test-author`), guard молча пропускает вызов: маршрут developer и
остальных агентов не меняется.

**Мягкий отказ.** Hook печатает один JSON `PreToolUse` с `permissionDecision: deny` и причиной
`test-author: <причина>` и завершается кодом 0. Он не пишет файлов, не меняет dispatch и ledger,
не возвращает пользовательский ввод. До проверки вызова guard заново проверяет активацию:
установлен ли agent с включённым hook, корректны ли `test_paths`, `test_runner` и зоны записи; при
сбое отказ получает причину этой проверки или `guard_unavailable`.

**Внутренний режим.** `test-author-guard.py --validate-config` печатает `{"status":"ok"}` либо
`{"status":"disabled","reason":"<причина>"}`. Его вызывает только
[`test-author-preflight.sh`](./test-author-preflight.md).

## 3. Контракты

- **Вход:** JSON `PreToolUse` на stdin с `agent_type`, `tool_name`, `tool_input`; корень проекта
  берётся из `CLAUDE_PROJECT_DIR`, а не из пути в вызове.
- **Причины отказа (закрытый набор):** `malformed_payload`, `write_outside_test_paths`,
  `write_path_invalid`, `runner_only`, `test_id_invalid`, `tool_not_allowed`, `guard_unavailable`
  и причины проверки активации: `agent_missing`, `scoped_hook_unsupported`, `config_invalid`,
  `test_paths_missing`, `test_runner_missing`, `write_zone_rejected`.
- **Код выхода:** всегда `0`.

## Источник

[test-author-guard.py](../../harness/project/hooks/test-author-guard.py),
[test_guard.py](../../tests/test_author/test_guard.py),
[test_compatibility.py](../../tests/test_author/test_compatibility.py),
[docs/agents/test-author.md](../agents/test-author.md),
[ADR 0014](../adr/0014-test-author-isolation.md)
