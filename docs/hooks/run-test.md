# run-test.sh — изолированный запуск одного теста

## 1. Название и локализация

- **Исходный скрипт:** [`harness/project/hooks/run-test.sh`](../../harness/project/hooks/run-test.sh),
  ядро — [`harness/health/isolated_runner.py`](../../harness/health/isolated_runner.py)
- **Русский перевод:** изолированный runner одного выбранного теста.

## 2. Полное описание

`run-test.sh` — не hook события: установка `pvmalove-suite` копирует его в
`.claude/hooks/run-test.sh`, но не подключает в `settings.local.json`. Скрипт запускает один тест,
выбранный по идентификатору, внутри bubblewrap, поэтому тест и все его дочерние процессы не могут
изменить production-код, `.git` и конфигурацию runner. Сам скрипт только находит Python и корень
репозитория (на два уровня выше `.claude/hooks/`) и загружает ядро из `.harness/health/` через
`python3 -I -B`: переменные `PYTHON*`, пользовательский site, текущий каталог и запись байткода
отключены, а из зон записи runner ничего не импортирует и не исполняет. Режима запуска без
изоляции нет: любой провал до запуска теста даёт отказ.

**Настройки.** Runner читает только `.harness/project.json` checkout и принимает из CLI лишь
идентификатор теста, поэтому вызывающий не может расширить зону записи или подменить команду.
Оба поля необязательны и проверяются теми же функциями, что и `harness health`
(`test_paths_problems`, `test_runner_problems` в `harness/health/project_files.py`):

- `test_paths` — непустой список уникальных относительных glob без сегментов `..`, тестовые пути
  и единственная зона записи тест-автора;
- `test_runner` — объект `{"command": [...]}`: argv без шелла из непустых строк, в котором ровно
  один элемент равен `{test_id}`, и этот элемент не первый.

```json
"test_paths": ["tests/**"],
"test_runner": {"command": ["python3", "-m", "unittest", "{test_id}"]}
```

Шаблон `project.json.tmpl` этих полей не содержит: без них runner отказывает с
`test_paths_missing` или `test_runner_missing`.

**Зоны записи.** Схема допускает любой относительный glob, а runner монтирует на запись только
записи вида `<каталог>/**` и отказывает с `write_zone_rejected` для остальных. Каждая запись
должна пройти все проверки:

- остальные сегменты литеральные: без `*?[]{}` и `\`, непустые, не `.` и не `..`;
- первый сегмент не `.git`, `.harness`, `.claude`, `.agents` или `.codex` (без учёта регистра), и
  зона не совпадает с корнем;
- каталог существует, ни один компонент пути от корня checkout не является symlink, а `realpath`
  совпадает с самим путём;
- зоны не вложены друг в друга: иначе тест одного запуска мог бы подменить каталог внутренней
  зоны symlink на production до монтирования в параллельном запуске;
- в зоне нет обычного файла с несколькими жёсткими ссылками: заранее созданный hardlink на
  production-файл дал бы к нему альтернативный доступ.

Symlink внутри зоны безопасен: запись через него попадает в монтирование только для чтения и
получает `EROFS`. Кроме зон тест пишет в `/tmp` (tmpfs, не сохраняется) и в свежие для каждого
запуска `cache/` и `reports/`. Общего кеша между запусками нет: через него тест подложил бы
`.pyc` production-модуля следующему запуску.

**Песочница.** Набор флагов из [ADR 0014](../adr/0014-test-author-isolation.md) не ослабляется:

```text
bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp
      --bind <зона> <зона> ... --bind <run>/cache <run>/cache --bind <run>/reports <run>/reports
      --chdir <repo> --unshare-all --die-with-parent --new-session --cap-drop ALL
      --json-status-fd <fd> -- <test_runner.command с id вместо {test_id}>
```

Идентификатор подставляется одним элементом argv, шелл не используется. `bwrap` ищется в `PATH`;
работу изоляции доказывает probe, а не наличие бинарника. Тест получает только `PATH`, `LANG`,
`LC_ALL`, `LC_CTYPE` и `TZ` из окружения вызывающего, а также `HOME=/tmp`, `TMPDIR=/tmp`,
`XDG_CACHE_HOME`, `PYTHONPYCACHEPREFIX`, `HARNESS_TEST_CACHE_DIR` (каталог `cache/`) и
`HARNESS_TEST_REPORT_DIR` (каталог `reports/`). Остальные переменные, включая токены, тест не
видит. Таймаут теста — 300 секунд; по истечении runner завершает группу процессов, а
`--die-with-parent` завершает всё дерево внутри песочницы.

**Порядок проверок.** Runner проверяет идентификатор, затем настройки и зоны, затем хост: Linux,
`bwrap` в `PATH`, checkout и каталог запуска вне `/tmp`, `/dev` и `/proc`, которые песочница
подменяет своими. Затем он создаёт каталог запуска и выполняет probe с тем же набором
монтирований: создание файла в корне checkout обязано получить именно `EROFS`, запись в `cache/`
обязана пройти, а оба артефакта удаляются. Только после этого запускается тест. Если bwrap не
сообщил `child-pid`, песочница не поднялась, и результатом будет отказ `sandbox_failed`, а не
провал теста. Diff-проверка файлов защитой не служит и runner её не выполняет.

**Локальные артефакты.** Каталог `.harness/.sandboxes/runs/isolated-test-<run_id>/` содержит
`log.txt` (stdout и stderr probe и теста, потолок 4 MiB с маркером обрезки; его пишет runner вне
песочницы), `cache/` и `reports/`. В песочницу монтируются только `cache/` и `reports/`. Старые
запуски удаляет `harness cleanup` по возрасту.

**Граница поддержки.** Поддержаны Linux и bubblewrap 0.9.0+ с непривилегированными
user-namespaces. На Windows и macOS runner отвечает `isolation_unavailable` с `platform`. Checkout
внутри `/tmp` не поддерживается: тест его не увидит, и runner откажет с `checkout_masked`.
Поддержаны только зоны вида `<каталог>/**`: проект с тестами рядом с исходниками (например,
`**/*_test.go`) получит `write_zone_rejected`.

**Граница с другими задачами.** Runner защищает checkout от процесса теста и его потомков, но не
от вызывающего агента вне песочницы. Субагент тест-автора, его scoped guard, preflight с
canary-проверкой guard и opt-in проекта относятся к #554; `--probe` даёт для preflight только
статус изоляции. Подготовка сборочных артефактов и тестовой БД относится к #556.

## 3. Контракты

- **Вход:** `run-test.sh <test-id>` или `run-test.sh --probe`; любые другие аргументы дают отказ
  `test_id_invalid`. Идентификатор соответствует `^[A-Za-z0-9_][A-Za-z0-9_.:/\[\]-]{0,199}$`
  (node id pytest, dotted id unittest): ведущий `-`, пробел, `;`, `$`, обратная кавычка и перевод
  строки отклоняются. Настройки — `test_paths` и `test_runner` из `.harness/project.json`.
- **Выход:** одна строка JSON не длиннее 512 байт с фиксированными ключами:
  - `test_id` — идентификатор или `null` для недопустимого;
  - `result` — `passed`, `failed`, `timeout` или `refused`;
  - `reason` — `test_passed`, `test_failed`, `timeout` или причина отказа;
  - `detail` — только для `isolation_unavailable`, иначе `null`;
  - `exit_code` — код теста или `null`;
  - `run_id` — hex-идентификатор каталога запуска или `null`.

  Вывод теста, абсолютные пути и переменные окружения в строку не попадают, stderr пуст.
- **Причины отказа (закрытый набор):**
  - `runner_unavailable` — нет Python, не загружается `.harness/health` или runner упал;
  - `test_id_invalid` — недопустимый идентификатор или лишние аргументы;
  - `config_invalid` — `project.json` нечитаем или поля не проходят проверку;
  - `test_paths_missing`, `test_runner_missing` — поле не задано;
  - `write_zone_rejected` — зона записи не прошла проверки;
  - `isolation_unavailable` с `detail`: `platform`, `bwrap_missing`, `checkout_masked`,
    `sandbox_failed` или `probe_failed`.
- **Коды выхода:** `0` — тест прошёл; `1` — тест упал или превысил таймаут; `3` — отказ, тест
  не запускался.
- **`--probe`:** не требует `test_paths`, выводит `{"status":"ok"}` с кодом `0` либо
  `{"status":"unavailable","reason":"isolation_unavailable","detail":"<код>"}` с кодом `3`; при
  недоступном runner — `reason: runner_unavailable`. Каталог запуска probe удаляется.

## Источник

[run-test.sh](../../harness/project/hooks/run-test.sh),
[isolated_runner.py](../../harness/health/isolated_runner.py),
[project_files.py](../../harness/health/project_files.py),
[ADR 0014](../adr/0014-test-author-isolation.md)
