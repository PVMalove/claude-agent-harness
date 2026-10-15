# Test-author — тест-автор

## 1. Назначение

`test-author` — необязательный in-session субагент developer для следующего поведенческого
теста цикла Red → Green → Refactor. Он пишет ровно один тест и запускает его через
`.claude/hooks/run-test.sh`; production-код, скелет и orchestration остаются у developer.

Субагент доступен только в Claude Code и только после успешного
`.claude/hooks/test-author-preflight.sh`. Команда возвращает одну строку `{"status":"ok"}`,
если `test_paths`, `test_runner`, process isolation и scoped guard подтверждены. При любом
отказе она возвращает `{"status":"disabled","reason":"<санитизированная причина>"}` и
developer продолжает обычный маршрут.

## 2. Конфигурация

В `.harness/project.json` задаются обе необязательные настройки:

```json
"test_paths": ["tests/**"],
"test_runner": {"command": ["python3", "-m", "pytest", "{test_id}"]}
```

`test_paths` — непустой список уникальных относительных glob-шаблонов без сегментов `..`.
Схема и `harness health` принимают любой такой glob, но тест-автор активируется только с зонами
записи вида `<каталог>/**`: остальные сегменты литеральные (без `*?[]{}` и `\`), первый сегмент не
`.git`, `.harness`, `.claude`, `.agents` или `.codex`, каталог существует, на пути к нему нет
symlink, внутри нет файлов с несколькими жёсткими ссылками, а зоны не вложены друг в друга. Иначе
preflight отвечает `write_zone_rejected`, например для `tests/*.py`.
`test_runner.command` — argv без shell, с ровно одним аргументом `{test_id}`, который не является
первым. Схема, validator и `harness health` используют один контракт; отсутствие полей не
считается ошибкой, но выключает тест-автора.

## 3. Защита

Frontmatter установленного `.claude/agents/test-author.md` подключает
`.claude/hooks/test-author-guard.py` только к этому субагенту для `Write`, `Edit` и `Bash`.

- `Write` и `Edit` проходят только после нормализации пути и только внутри `test_paths`; symlink,
  ведущий за пределы тестовой зоны, отклоняется.
- `Bash` разрешает только один вызов `.claude/hooks/run-test.sh <test-id>` (допускается
  эквивалентный вызов через доверенный системный `bash`); shell-метасимволы, `--probe`,
  дополнительные аргументы и другие команды отклоняются.
- Отказ — мягкий: hook печатает JSON с причиной, не меняет dispatch/ledger и завершается кодом 0.
  Другие субагенты не получают эту защиту и не меняют свой маршрут.

Preflight проверяет условия и на первом нарушении отвечает `disabled` с одной
фиксированной причиной:

| Проверка | Причина отказа |
| --- | --- |
| `CLAUDE_PROJECT_DIR` совпадает с корнем Git checkout | `project_untrusted` |
| Claude Code не запущен с `CLAUDE_CODE_SIMPLE=1` или `CLAUDE_CODE_SAFE_MODE=1` | `claude_code_unsupported` |
| Пользователь принял диалог доверия для этого проекта | `project_untrusted` |
| Установлен agent `test-author.md` | `agent_missing` |
| В agent включён scoped `PreToolUse` hook | `scoped_hook_unsupported` |
| Заданы `test_paths` и `test_runner`, зоны записи допустимы | `test_paths_missing`, `test_runner_missing`, `config_invalid`, `write_zone_rejected` |
| Файл `test-author-guard.py` исполняемый и отвечает JSON | `guard_hook_unavailable` |
| `run-test.sh --probe` подтверждает изоляцию | `isolation_unavailable`, `runner_unavailable` |

Доверие проверяется по записи проекта в `~/.claude.json` (или `$CLAUDE_CONFIG_DIR/.claude.json`):
для точного пути проекта должно стоять `hasTrustDialogAccepted: true`. Нативный режим
`claude --print` диалог доверия пропускает, поэтому preflight проверяет решение сам. Если Claude
Code считает проект доверенным через родительский каталог или корень Git, но записи для самого
пути нет (например, в отдельном worktree), preflight отвечает `project_untrusted`: это
безопасный отказ, а не ошибка. Откройте проект по этому пути в Claude Code и примите диалог.

Проверки agent, hook и guard статические: preflight не запускает Claude Code и не доказывает,
что hook сработает в живой сессии. Поэтому `guard_hook_unavailable` означает только отсутствие
исполняемого guard или его неисправный ответ. Реальное срабатывание scoped hook подтверждает
проверка самого харнесса на настоящем Claude Code; в установленный проект она не входит. Если agent
или hook отсутствует, Claude Code работает в режиме, который отключает hooks, проект не является
доверенным, либо изоляция недоступна, test-author не запускается без защиты.

## 4. Контракт вызова

Developer передаёт требование, один сценарий и согласованный public seam. Test-author читает
минимальные относящиеся к делу файлы, пишет один тест только в `test_paths`, запускает выбранный
тест через runner и возвращает:

1. требование и сценарий;
2. входные данные и независимый источник ожидания;
3. идентификатор теста;
4. санитизированный JSON runner с доказательством поведенческого Red.

Если runner отказал или тест оказался Green, субагент сообщает факт и останавливается. Он не
создаёт manifest, dispatch, brief/report, approval или ledger record.
