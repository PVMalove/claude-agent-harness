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
`test_runner.command` — argv без shell, с ровно одним аргументом `{test_id}`, который не является
первым. Схема, validator и `harness health` используют один контракт; отсутствие полей не
считается ошибкой, но выключает тест-автора.

## 3. Защита

Frontmatter установленного `.claude/agents/test-author.md` подключает
`.claude/hooks/test-author-guard.py` только к этому субагенту для `Write`, `Edit` и `Bash`.

- `Write` и `Edit` проходят только после нормализации пути и только внутри `test_paths`; symlink,
  ведущий за пределы тестовой зоны, отклоняется.
- `Bash` разрешает только один вызов `.claude/hooks/run-test.sh <test-id>` (допускается
  эквивалентный вызов через `bash`); shell-метасимволы, `--probe`, дополнительные аргументы и
  другие команды отклоняются.
- Отказ — мягкий: hook печатает JSON с причиной, не меняет dispatch/ledger и завершается кодом 0.
  Другие субагенты не получают эту защиту и не меняют свой маршрут.

Preflight отдельно вызывает `run-test.sh --probe` и выполняет canary-запрос к guard. Если hook
не срабатывает, проект не является доверенным checkout, либо изоляция недоступна, test-author
не запускается без защиты.

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
