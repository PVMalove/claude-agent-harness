# targeted-check-reminder.sh — напоминание о targeted-проверках

## 1. Название и локализация

- **Исходный хук:** [`harness/project/hooks/targeted-check-reminder.sh`](../../harness/project/hooks/targeted-check-reminder.sh)
  и общее тело [`change_impact_hooks.py`](../../harness/project/hooks/change_impact_hooks.py)
- **Русский перевод:** напоминание в конце хода роли отчитать targeted-проверки brief.

## 2. Полное описание

Advisory-hook из матрицы ADR 0024, раздел 2a, на двух событиях:

- **`PreToolUse(Write|Edit)`** — ведёт журнал касаний операции и отвечает `allow` без вывода. Запись
  `t-<sha256(realpath)[:32]>.json` содержит путь, корень checkout и вид: `report` для записи в
  `.harness/.sandboxes/scratch/inbox/*.json` (`report_staging_path`), `touch` для остального. Запись
  создаётся через `O_EXCL` один раз и не меняется.
- **`Stop` и `SubagentStop`** — конец хода роли. Главный агент (coordinator, внешний worker)
  заканчивает ход событием `Stop`, роль-субагент in-process — событием `SubagentStop`. Изменения — это
  существующие файлы журнала, кроме путей под `.harness/` и `docs/tasks/` и самих отчётов. Если
  изменений нет или последний отчёт операции новее всех изменений по `mtime`, hook молчит. Иначе он
  выдаёт `warn` с кодом `targeted-checks-unreported` и stdout `{"systemMessage": "<текст>"}`: число
  файлов, worktree и требование отчитать targeted-проверки brief в `checks_run`.

Hook никогда не продолжает ход: у `Stop` и `SubagentStop` нет `decision: block`, кода `2` и
`additionalContext`, а при `stop_hook_active: true` он отвечает `allow` без вывода. Поэтому цикла
нет. Hook не запускает тесты, `make` и `git`: он только делает `stat` файлов журнала.

- **Профили.** `minimal` — hook не работает: тело не запускается, журнал не пишется. `standard` и
  `strict` — журнал и напоминание.
- **Дедупликация.** Предмет уведомления — отсортированное состояние `(путь, mtime_ns, размер)`
  изменённых файлов и `mtime_ns` последнего отчёта. Повторный `Stop` без новых изменений молчит;
  новая правка или новый отчёт меняют digest и дают новое напоминание.

### Хранилище операции

Состояние трёх hooks ChangeImpact лежит в `<проект>/.harness/.sandboxes/hook-events/<operation_id>/`.
Корень проекта hook берёт по расположению модуля (`.claude/hooks/` проекта); каталог gitignored и
одноразовый. Каталог операции содержит маркеры уведомлений `n-<key>.json` и журнал касаний
`t-<key>.json`. Блокировок и read-modify-write нет: каждый файл создаётся через `O_EXCL`, поэтому
поведение одинаково на Linux и Windows. Хранилище ограничено: не больше 64 каталогов операций (при
создании нового самые старые по `mtime` удаляются) и не больше 2048 файлов в операции (дальше записи не
создаются, а hook отвечает `allow` с диагностикой в stderr). Имена файлов состоят только из hex, а
временных каталогов `/tmp` и `fcntl` hook не использует.

### Второй runtime

Шаблон `.codex/hooks.json` подключает этот hook только на `Stop` и `SubagentStop`: второй runtime
доставляет оба события с `session_id`, `stop_hook_active` и, у `SubagentStop`, `agent_id`. Правки
файлов там приходят как `apply_patch` без `file_path`, поэтому журнал касаний на `Write` и `Edit` ждёт
адаптера payload (#830). До него журнал второго runtime пуст, и напоминание молчит; `harness health`
перечисляет строку `PreToolUse` в `нет события или payload` с проверкой вне hook — сверкой
`checks_run` coordinator-ом.

## 3. Контракт

- **Вход:** JSON события в stdin: `session_id`, `agent_id` (у субагента), `hook_event_name`;
  у `PreToolUse` — `tool_input.file_path` и `cwd`, у `Stop` и `SubagentStop` — `stop_hook_active`.
- **Выход:** код `0` всегда. Напоминание — `systemMessage` в stdout, первая строка stderr
  `[targeted-checks-unreported] <текст>`, строка `evidence_ref=<путь>` и вердикт `warn`; остальные
  вызовы — `allow` без stdout. Внутренняя ошибка — `warn` `internal-error` с пустым stdout, чтобы
  частичный вывод не сломал протокол `Stop` второго runtime.

## Источник

[targeted-check-reminder.sh](../../harness/project/hooks/targeted-check-reminder.sh),
[change_impact_hooks.py](../../harness/project/hooks/change_impact_hooks.py)
