# test-author-preflight.sh — проверка активации test-author

## 1. Название и локализация

- **Исходный скрипт:** [`harness/project/hooks/test-author-preflight.sh`](../../harness/project/hooks/test-author-preflight.sh)
- **Русский перевод:** проверка, что тест-автора можно включить без потери защиты.

## 2. Полное описание

`test-author-preflight.sh` — не hook события: установка копирует его в
`.claude/hooks/test-author-preflight.sh`, но не подключает в `settings.local.json`. Developer
запускает его перед вызовом субагента test-author. Скрипт печатает ровно одну строку JSON и всегда
завершается кодом 0: `{"status":"ok"}` либо `{"status":"disabled","reason":"<причина>"}`. Отказ не
создаёт состояния оркестрации, а developer продолжает обычный маршрут.

**Аргумент `--worktree <путь>`.** Скрипт проверяет только тот checkout, в котором лежит сам, то есть
checkout, откуда он запущен. Developer in-process worker работает в другом worktree, поэтому
вызывает `test-author-preflight.sh --worktree <worktree из brief>`. Ответ `{"status":"ok"}`
возможен, только если канонический путь (после `cd` и `pwd -P`) совпадает с checkout скрипта;
иначе скрипт отвечает `checkout_mismatch`. Отсутствующий, пустой или неразрешимый путь, а также
любые другие аргументы (неизвестный флаг, лишние аргументы, `--worktree` без значения) дают тот же
`checkout_mismatch`. Проверка идёт сразу после определения корня скрипта и раньше остальных, а путь
из аргумента в ответ не попадает. Вызов без аргументов работает как прежде.

Скрипт проверяет условия и на первом нарушении отвечает фиксированной причиной (таблица условий и
причин есть в [руководстве](../agents/test-author.md), раздел 3). Поддерживаемые причины:
`checkout_mismatch`, `runner_unavailable`, `project_untrusted`, `claude_code_unsupported`, `agent_missing`,
`scoped_hook_unsupported`, `test_paths_missing`, `test_runner_missing`, `config_invalid`,
`write_zone_rejected`, `guard_hook_unavailable`, `isolation_unavailable`. Ответы guard и runner
пропускаются через список допустимых причин: любое другое значение заменяется на
`guard_hook_unavailable` или `isolation_unavailable`, поэтому в ответ не попадают пути и текст
чужих ошибок.

**Доверие проекту.** Нативный режим `claude --print` не показывает диалог доверия, поэтому скрипт
читает решение сам: в `~/.claude.json` (или `$CLAUDE_CONFIG_DIR/.claude.json`) для точного пути
`CLAUDE_PROJECT_DIR` должно стоять `hasTrustDialogAccepted: true`. Отсутствие файла, битый JSON,
чужие проекты и любое значение, кроме булева `true`, дают `project_untrusted`. Claude Code может
считать доверенным и каталог, чей родитель или корень Git принят; для такого пути записи нет, и
скрипт отвечает `project_untrusted`. Это безопасное направление ошибки.

**Режимы Claude Code.** Переменные `CLAUDE_CODE_SIMPLE` и `CLAUDE_CODE_SAFE_MODE` отключают
hooks; скрипт при значении `1` отвечает `claude_code_unsupported`. Обе переменные присутствуют в
бинарнике Claude Code 2.1.300; при смене версии эту проверку стоит повторить.

**Статический характер.** Скрипт не запускает Claude Code. Проверки agent, frontmatter и guard
статические, поэтому `guard_hook_unavailable` означает неисправный или неисполняемый guard, а не
доказательство срабатывания hook в живой сессии.

## 3. Проверка реальным Claude Code в clean-room

Срабатывание scoped hook подтверждает не preflight, а сценарий
[`scripts/clean_room/isolated_test_runner.py`](../../scripts/clean_room/isolated_test_runner.py) с
модулем [`claude_probe.py`](../../scripts/clean_room/claude_probe.py). Он управляется переменными
окружения:

| Переменная | Эффект |
| --- | --- |
| не заданы | проба не запрашивается, в логе строка `confirmation not requested` |
| `HARNESS_VERIFY_CLAUDE_TEST_AUTHOR=1` | проба запрошена; без `claude` в `PATH` пропускается с сообщением, любой иной сбой проваливает сценарий |
| `HARNESS_REQUIRE_CLAUDE_TEST_AUTHOR=1` | проба обязательна; отсутствие `claude` тоже провал |

`make verify` и CI эти переменные не задают: у CI нет учётных данных Claude Code, а проба тратит
квоту, поэтому её запускают вручную перед PR:

```bash
make test-author-probe
```

Цель выставляет `HARNESS_REQUIRE_TEST_SANDBOX=1`, `HARNESS_VERIFY_CLAUDE_TEST_AUTHOR=1` и
`HARNESS_REQUIRE_CLAUDE_TEST_AUTHOR=1` и запускает весь clean-room (`scripts/test_clean_room.py`).
Нужны Linux с bubblewrap, `claude` в `PATH` и выполненный вход; результат пробы приводят в теле PR.

Проба запускает `claude --agent test-author` в доверенном clean-room проекте дважды. Первый запуск
просит три вызова: запись `src/canary.txt`, команду `touch bash-probe.txt` и запись
`tests/allowed-probe.txt`. Hook обязан отказать в первых двух с причинами
`write_outside_test_paths` и `runner_only` (они есть только у guard, поэтому служат признаком
срабатывания), а третий пропустить. Второй запуск отдаёт субагенту требование и сценарий: он сам
пишет один тест и запускает его через runner. Результат принимается, только если runner сообщил
`failed`, лог запуска содержит `AssertionError` без `TypeError`, `ImportError` и `SyntaxError`, а
production-код не изменён. Изолированный `HOME` с решением о доверии лежит рядом с проектом, а
ссылка на учётные данные пользователя создаётся только на время пробы и удаляется после неё.

Кроме пробы, сценарий проверяет без Claude Code, что preflight активируется в доверенном проекте и
остаётся `project_untrusted` при трёх состояниях доверия: решение не принято, запись только для
другого проекта и отсутствие `.claude.json`.

## Источник

[test-author-preflight.sh](../../harness/project/hooks/test-author-preflight.sh),
[test_preflight.py](../../tests/test_author/test_preflight.py),
[claude_probe.py](../../scripts/clean_room/claude_probe.py),
[docs/agents/test-author.md](../agents/test-author.md),
[ADR 0014](../adr/0014-test-author-isolation.md)
