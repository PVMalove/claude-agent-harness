# Backend-оркестрация

Модуль capability `backend-orchestration` ведёт один тикет через роли
`architect → developer → code-review → qa → publish`. Он даёт проверяемые переходы, неизменяемые
brief и report, независимый review и clean-room QA. Capability выбирают явно: она расширяет
`pvmalove-suite`, а проекты без неё не меняются.

Оркестрация — не автономный scheduler. Coordinator — это человек или назначенная им управляющая
сессия: он создаёт batch, утверждает каждый dispatch и принимает или отклоняет каждый report согласно
`approval_policy`. Роли не расширяют свой scope, не выбирают модель и не мержат PR.

Пошаговая процедура настройки и запуска описана в
[руководстве по backend-оркестрации](https://github.com/PVMalove/claude-agent-harness/blob/master/docs/backend-orchestration.md),
полный контракт lifecycle — в [playbook.md](./playbook.md), а границы ролей — в [roles/](./roles/).

## Состав

| Путь | Назначение |
| --- | --- |
| `coordinator.py` | CLI coordinator: разбор аргументов, маршрутизация и JSON-вывод. Логика — в `core/`, `ledger/`, `workflow/`. |
| `core/` | Константы и значения по умолчанию, чтение конфига, git, workspace. |
| `ledger/` | Локальное хранилище state: поколения ledger, неизменяемые записи, audit, миграции. |
| `workflow/` | По модулю на стадию batch: планирование, preflight, brief, dispatch, решения, отчёты, доставка. |
| `contract.py` | Проверка конфига, role manifest'ов, brief и report; источник `harness health` для оркестрации. |
| `qa_lane.py` | Очередь clean-room QA и аренда с истечением. Batch QA получает проверки и сохранение отчёта через адаптер `workflow/qa_integration.py`. |
| `workflow/fix_forward.py` | История Fix-forward: retry-цепочка, основания диапазонов и происхождение rebased-копий для проверки report и delta-review. |
| `operational_guards.py` | Чистая привязка transition к brief, дайджест approval и retry key; lifecycle и policy проверяет coordinator. |
| `runtime_access.py`, `operation_access.py` | План доступа (`access_policy`) и его проверка перед QA, Git и publish. Эти операции выполняет сам coordinator. |
| `dispatch_preflight.py`, `runtime_attestation.py` | Проверка worktree, ветки и SHA воркера; защитные проверки перед dispatch. |
| `advisory.py`, `extensions.py` | Advisory-вызовы и подключаемые extensions (health, классификатор retry, уведомления). |
| `roles/` | Role manifest'ы: режим (`read-only`/`write`), требуемые capability, risk triggers. |
| `playbook.md`, `pilot.md` | Полный lifecycle и форма наблюдения за первыми batch. |
| `orchestration.schema.json` | JSON Schema проектного конфига для подсказок редактора. |

В целевом проекте модуль лежит в `.harness/orchestration/`, и `harness update` его обновляет. Рядом
находятся управляемый пример `.harness/orchestration.example.json` и собственный конфиг проекта
`.harness/orchestration.json`.

## Как это работает

1. **Batch.** `batch create` фиксирует тикет, явный scope записи (`--allowed-path`), issue-ветку,
   worktree, Definition of Done, запреты и проверки; перед ним запускают `batch preflight`. Каждый
   тикет получает один batch в своём worktree, а batch с пересекающимися файлами идут параллельно,
   пока хватает `concurrency_budget`.
2. **Dispatch.** Для каждой роли coordinator создаёт dispatch с неизменяемым brief. Brief содержит
   роль, runtime, модель, effort, `write_paths` (scope batch), `allowed_tools`, `context_budget`,
   проверки, Context Package и политику; правка конфига уже созданный brief не меняет.
3. **Работа роли.** Роль подтверждает свою фактическую модель (self-report) и, если это включено,
   worktree, ветку и SHA (attestation). Она шлёт heartbeat и сдаёт completion report на русском.
4. **Решение.** Coordinator принимает report (`batch decide --decision accept`), отправляет на
   retry, блокирует или завершает batch; маршрут retry зависит от структурных данных report. При
   `approval_policy` `milestone`/`low_risk`/`auto` политика сама принимает чистые report без
   рисков, а при `auto` остальные решения принимает `batch auto-decide` (раздел «Automatic path» в
   `playbook.md`).
5. **QA и publish.** Clean-room QA выполняет `qa_preparation` (если он задан), затем
   `verification_commands` на закреплённом candidate SHA в общей очереди. Провал подготовки QA не
   считает провалом проверок кода. Publish выдаёт принятый SHA, а PR человек открывает через
   `/to-pull-requests`.
6. **Integration accounting.** После accepted publish `integration prepare` записывает связь:
   тикет, ветка, source batch, опубликованный candidate SHA и target SHA. `integration status`
   только наблюдает `stale` и dispatch не создаёт, а `integration link-evidence` принимает будущие
   результаты CI, local-QA и resolver. Завершённый batch не переписывается.

Состояния batch: `planned → awaiting-approval ↔ active → completed | blocked | failed | abandoned`.
`needs_attention` — не состояние, а флаг, который останавливает следующий dispatch при зависшем
воркере, исчерпанных retry или долгой очереди. Всё локальное state лежит в gitignored
`.harness/orchestration/state/`.

## Файлы конфигурации

| Файл | Кто владеет | Когда меняется |
| --- | --- | --- |
| `.harness/orchestration.example.json` | Харнесс (managed) | Харнесс ставит и обновляет файл при `init`, `adopt`, `update`; `harness diff` показывает drift. |
| `.harness/orchestration.json` | Проект | Если файла нет, первая установка создаёт его копией примера. Дальше файл меняют только ваши правки; `--force-seed-files` перезаписывает его. |

Новые поля и значения из обновлённого примера переносите в свой конфиг вручную. После любой правки
выполните `harness health`. Команда проверяет конфиг полностью: схему полей, ссылки на профили,
совместимость capability с role manifest'ами, fallback и обязательный `code-review`.

**Без конфига** coordinator тоже работает: потолок записи ролей — весь репозиторий, а границу
задаёт `--allowed-path` batch. Проверки coordinator берёт из `qa_gate_commands` в
`.harness/project.json`, бюджет параллелизма равен 1, `model` и `effort` передаются в
`dispatch create`, а транспорт возможен только `in-process`. Конфиг без назначений означает то же
самое.

## Справочник `orchestration.json`

Обязательны `provider_profiles`, `assignment_plans`, `concurrency_budget` и
`verification_commands`. Остальные поля необязательны и без значения берут default из таблиц, а
проверка конфига отклоняет неизвестные поля.

### Верхний уровень

| Поле | Тип | По умолчанию | Назначение |
| --- | --- | --- | --- |
| `$schema` | строка | — | Путь к схеме для редактора: `./orchestration/orchestration.schema.json`. |
| `provider_profiles` | объект | — | Профили агентов: имя → capability, fallback, ограничения. |
| `assignment_plans` | объект | — | Назначение каждой используемой роли: потолок записи, runtime, модель, effort. |
| `concurrency_budget` | целое ≥ 1 | 1 | Сколько batch могут быть активны одновременно. Единственный предел параллелизма: пересечение файлов его не заменяет. |
| `verification_commands` | список строк | `[]` | Полный gate clean-room QA. Пустой список — QA без проверок; впишите реальные команды проекта. |
| `qa_preparation` | список строк | `[]` | Команды, которые готовят окружение clean-room QA (установка зависимостей и т.п.). QA выполняет их в том же checkout candidate SHA до `verification_commands` и останавливается на первой упавшей. Без поля QA сразу гоняет gate; report не меняется. |
| `qa_environment_probes` | список строк | `[]` | Независимые пробы окружения QA (например, доступность реестра). QA запускает их только после упавшей `qa_preparation`. Упавшая проба при успешных `qa_project_file_checks` подтверждает инфраструктурную причину. |
| `qa_project_file_checks` | список строк | `[]` | Офлайн-проверки файлов проекта (например, согласованность lock-файла). QA запускает их только после упавшей `qa_preparation`. Упавшая проверка подтверждает дефект проекта. Без проб и проверок QA не подтверждает инфраструктурную причину. |
| `developer_verification_commands` | список строк | = `verification_commands` | Быстрые проверки developer. Без поля developer гоняет полный gate, а `harness health` предупреждает. |
| `review_verification_commands` | список строк | = `verification_commands` | Проверки code-review. |
| `test_path_patterns` | список glob | `tests/**`, `**/tests/**`, `**/test_*.py`, `**/*_test.py` | Какие пути считаются тестами (delta-review при изменении только тестов). |
| `approval_policy` | `manual_all` \| `milestone` \| `low_risk` \| `auto` | `manual_all` | Какие report политика принимает без человека (см. ниже). |
| `low_risk_paths` | список glob | — | Пути в форме `dir/**`, `**` или точного файла (без `./`, `//`, `..`; сравнение по сегментам). При `low_risk` политика автоматически принимает чистые report внутри этих путей, если весь `--allowed-path` batch лежит в них. Без списка ничто не считается низкорисковым. |
| `human_approval_gate` | `trusted` \| `tty` | `trusted` | `trusted` — approval через `--approved-by/--approved-at`; `tty` — только интерактивное подтверждение в терминале. |
| `approval_ttl_seconds` | целое ≥ 1 | без срока | Срок жизни `--approved-at`. Coordinator отклоняет более старое approval и approval, датированное будущим. |
| `worker_attestation_required` | логическое | `false` | Воркер до работы подтверждает фактический worktree, ветку и SHA. Пример включает `true`. |
| `communication_policy` | объект | `en` / `ru` | `agent_to_agent_language` всегда `en`, `coordinator_report_language` всегда `ru`. |
| `tool_policy` | объект | по режиму роли | Рабочий набор инструментов роли в brief (см. ниже). |
| `access_policy` | объект | — | Доступ ролей и операций `qa`/`git`/`publish` к сети и файловой системе (см. «Доступ QA, Git и publish»). Opt-in: любой `access_policy` блокирует `dispatch send`, пока проект не подключил реализацию `extensions.runtime_access`. Поэтому пример его не содержит. |
| `extensions` | объект | все `none` | Подключаемые интерфейсы вне ядра (см. ниже). |

Отдельные разделы ниже описывают политики `context_package_policy`,
`adaptive_continuation_policy`, `repo_map_policy`, `continuation_policy`, `retry_policy`,
`execution_policy`, `attention_policy` и `preflight_policy`. В каждой можно задать только часть
полей: остальные берут default.

### `provider_profiles.<имя>`

| Поле | Назначение |
| --- | --- |
| `capabilities` | Непустой список возможностей: `architecture-analysis`, `backend-development`, `code-review`, `independent-verification`, `database-migrations`, `messaging-integration`, `conflict-resolution`. Профиль роли должен покрывать `required_capabilities` её manifest'а. |
| `fallback` | Упорядоченные имена резервных профилей при безопасном отказе основного. Failover не меняет runtime brief. |
| `known_limitations` | Известные ограничения профиля (текст). |

### `assignment_plans.<роль>`

Роли — имена manifest'ов: `architect`, `developer`, `code-review`, `qa`, `verification`,
`database-migrations`, `messaging-integration`, `conflict-resolver`. Если назначения заданы,
`code-review` обязателен.

| Поле | Назначение |
| --- | --- |
| `write_paths` | Потолок записи роли (по умолчанию весь репозиторий; пример задаёт `["**"]`). `--allowed-path` batch не может быть шире. Coordinator проверяет brief и report по scope batch. `changed_files` developer вне scope дают предупреждение; его принимают только через `override-warning`. |
| `transport` | `in-process` (по умолчанию: субагент coordinator-сессии) или `external` (проектный runtime adapter). |
| `runtimes` | Именованные наборы (`claude`, `codex`, …): у каждого `profiles`, `model`, `effort`. |
| `default_runtime` | Runtime по умолчанию, если их несколько. Без него `dispatch create` требует `--runtime`. |

`model` — CLI-алиас или ID без пробелов. `effort`: `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, `max`, `ultra`. Coordinator записывает выбранные runtime, модель и effort в brief. Роль
сверяет с ними свою фактическую модель.

### `approval_policy`

| Значение | Поведение |
| --- | --- |
| `manual_all` | Каждый report принимает человек. |
| `milestone` | Политика автоматически принимает чистый report (`completed`, без рисков, блокеров, risk triggers и упавших проверок). Исключения — QA, publish и batch с совпавшими risk triggers. |
| `low_risk` | То же, но только для batch, чей `--allowed-path` целиком лежит в `low_risk_paths`. |
| `auto` | Автоматический путь от `batch approve` до принятого publish. Политика согласует batch, каждый dispatch (включая publish, risk triggers, `bypass-rerun` и rebase target) и плановое продолжение. Если цепочка не приняла report, сессия запускает `batch auto-decide`. Политика принимает report или выбирает retry по `route_preview`. Политика записывает каждое решение как `policy:auto` с evidence в `batch.auto_decisions`. Остановку из закрытого списка она записывает в `batch.auto_stop`; после остановки каждый шаг согласует человек. `batch auto-report` выводит итоговый отчёт. Только человек открывает PR; auto-merge запрещён. Требует `human_approval_gate: trusted` и `worker_attestation_required: true`. |

При создании batch coordinator фиксирует в нём политику, и её смена на уже созданные batch не
влияет. Исключение — `auto`: эта политика согласует шаги, только пока конфиг проекта и план batch
выбирают `auto`, в batch нет `auto_stop` и не было ручного восстановления. В остальных случаях каждый шаг такого batch согласует
человек.

### `tool_policy`

Без поля роль получает набор по режиму manifest'а: `read-only` — `Read`, `Grep`, `Glob`, `Bash`;
`write` — те же плюс `Edit`, `Write`. Переопределяют набор `modes` (ключи `read-only`/`write`) и
`roles` (имена ролей), причём запись роли важнее записи режима. Это рабочий набор роли, а не запрет
глобальных инструментов runtime.

```json
"tool_policy": {"modes": {"read-only": ["Read", "Grep", "Glob"]}, "roles": {"qa": ["Read", "Grep", "Glob", "Bash"]}}
```

### `context_package_policy`

| Поле | По умолчанию | Назначение |
| --- | --- | --- |
| `max_tokens` | 200000 | Потолок оценки Context Package в токенах. |
| `context_window_tokens` | 250000 | Окно контекста целевой модели. |
| `reserved_prompt_tokens` | 20000 | Резерв под системные инструкции. |
| `symbol_graph_depth` | 2 | Глубина import-графа для каждого автоматического пакета. |
| `max_related_tests` | 25 | Предохранитель: больше связанных тестов — ошибка сборки пакета. |
| `min_starting_files` / `max_starting_files` | 5 / 10 | Число стартовых файлов при ручной регистрации пакета. |
| `section_index_min_tokens` | 20000 | С какого размера Markdown попадает в пакет оглавлением, а не целиком. |

### `adaptive_continuation_policy`

| Поле | По умолчанию | Назначение |
| --- | --- | --- |
| `context_limit` | 150000 | Бюджет контекста роли (`context_budget` в brief); dispatch отклоняет больший пакет. |
| `context_warn_ratio` | 0.8 | Доля `context_limit`, после которой фиксируется давление контекста. |
| `tdd_cycle_count` | 3 | Число TDD-циклов до checkpoint и новой worker session. |
| `failure_log_bytes` | 20000 | Размер лога ошибок, после которого предлагается продолжение. |

### `continuation_policy`, `retry_policy`, `execution_policy`

| Поле | По умолчанию | Назначение |
| --- | --- | --- |
| `continuation_policy.max_continuations` | 2 | Продолжений worker session на один dispatch. |
| `continuation_policy.max_rate_limit_resumes` | 1 | Автоматических возобновлений после rate limit. |
| `retry_policy.max_developer_retries` | 1 | Retry developer после отклонённого review или QA (`0` — ни одного). |
| `execution_policy.dispatch_wait_timeout_seconds` | 60 | Одно ожидание `dispatch wait`. |
| `execution_policy.dispatch_poll_interval_seconds` | 5 | Интервал опроса dispatch. |
| `execution_policy.qa_lease_seconds` | 1800 | Аренда QA lane. |
| `execution_policy.rate_limit_retry_seconds` | 60 | Пауза при rate limit без срока от провайдера. |

### `attention_policy`

| Поле | По умолчанию | Назначение |
| --- | --- | --- |
| `retry_queue_seconds` | 3600 | Сколько принятый retry может ждать своего dispatch. |
| `max_infrastructure_retries` | 2 | Операционных retry (инфраструктура, transport, контекст) на один candidate; `0` — ни одного. |
| `stale_dispatch_seconds` | 3600 | Молчание живого dispatch, после которого он считается зависшим (и `orchestration.stale_dispatches` в health). |
| `heartbeat_interval_seconds` | 300 | Желаемый интервал heartbeat. Реальный интервал не больше трети `stale_dispatch_seconds`. |

### `preflight_policy`

Ограничивает объём тикета до создания batch. Превышение — повод разделить тикет через
`/to-tickets`.

| Поле | По умолчанию |
| --- | --- |
| `require_estimates` | `true` |
| `max_definition_of_done_items` | 5 |
| `max_dependencies` | 3 |
| `max_expected_files` | 12 |
| `max_expected_services` | 1 |
| `max_expected_changed_lines` | 800 |
| `max_expected_context_tokens` | 80000 |
| `estimated_tokens_per_changed_line` | 20 |
| `estimated_tokens_per_file` | 2000 |

### `repo_map_policy`

Фильтрация и лимиты Repo Map для Context Package: `allow_paths`, `deny_paths`,
`redact_paths`, `redact_symbols`, `max_files` (10000), `max_file_bytes` (2000000),
`max_path_length` (4096), `max_symbol_length` (256), `max_signature_length` (2048),
`timeout_seconds` (10), `max_tokens` (4000), `tier` (`full`/`minimal`), `min_tier` и
`min_tier_by_role` (гейт dispatch по tier), `parser_bundle_registry_paths`,
`parser_bundle_timeout_seconds` (30), `parser_bundle_max_output_bytes` (10000000). Подробно — в
[README Repo Map](../repo_map/README.md).

### `extensions`

Ключи: `transport_health`, `verification_environment_health`, `retry_reason_classifier`,
`context_telemetry_provider`, `human_notifier`. Значение — `none` (по умолчанию), имя,
зарегистрированное хост-процессом, или `module:factory`; неизвестное имя считается ошибкой.
Extensions не добавляют модели инструментов и не меняют системный промпт.

### Доступ QA, Git и publish

Операции `qa`, `git` и `publish` выполняет сам coordinator, а не воркер и не runtime adapter. Если
в проекте есть `access_policy`, coordinator перед действием выбирает план операции. Для `qa run` и
`dispatch publish` план закреплён в approved brief. Для `integration local-qa`,
`integration refresh` и `integration resolve` coordinator берёт план из живого конфига. Затем
coordinator проверяет план на своём процессе: режим, разрешение плана, запись в Git metadata, общее
хранилище, каталог clean-room checkout или checkout batch, пути `cache` и remote. Coordinator не
применяет к такой операции override роли (`roles`). Отказ, неподдерживаемый режим и непроверенное
требование останавливают действие до любых изменений. Они также называют ресурс, путь и средство
исправления. Подробности и диагностика — в
[руководстве](https://github.com/PVMalove/claude-agent-harness/blob/master/docs/backend-orchestration.md#доступ-qa-git-и-publish).

## Пример

Управляемый пример [`orchestration.example.json`](../orchestration.example.json) включает все
возможности модуля, которые не требуют проектных команд или внешней реализации:

| Что включено | Поля |
| --- | --- |
| Полностью автоматический путь до PR с итоговым отчётом для человека | `approval_policy: auto`, `human_approval_gate: trusted`, `worker_attestation_required: true`, `approval_ttl_seconds: 14400` |
| Параллельные batch в отдельных worktree | `concurrency_budget: 5` |
| Все восемь ролей на двух runtime (`claude`, `codex`), включая `conflict-resolver` для `integration resolve`, `verification`, `database-migrations` и `messaging-integration` | `provider_profiles`, `assignment_plans` с `transport: in-process`, `default_runtime: claude`, `write_paths: ["**"]` |
| Рабочие наборы инструментов по режимам и определение тестовых путей для delta-review | `tool_policy`, `test_path_patterns` |
| Контекст ролей и продолжение worker session | `context_package_policy`, `adaptive_continuation_policy`, `continuation_policy` |
| Repo Map с гейтом dispatch по tier | `repo_map_policy` (`tier: full`, `min_tier: minimal`, лимиты parser bundle) |
| Ограниченные retry разработчика и инфраструктуры, остановка зависших dispatch | `retry_policy`, `attention_policy`, `execution_policy` |
| Preflight объёма тикета | `preflight_policy` |
| Язык координации: `en` между агентами, `ru` в отчётах coordinator | `communication_policy` |

Расширения в примере выключены (`extensions.*: none`), а `access_policy` отсутствует: их включение
требует реализации вашего проекта. Списки проверок пусты, потому что команды зависят от стека.

Перед первым batch:

1. Впишите реальные проверки проекта:
   - `verification_commands` — полный gate clean-room QA;
   - `developer_verification_commands` и `review_verification_commands` — быстрые task-scoped
     проверки для developer и code-review;
   - `qa_preparation` — установка зависимостей перед gate;
   - `qa_environment_probes` и `qa_project_file_checks` — независимые проба окружения и офлайн-проверка
     файлов проекта. Только вместе они позволяют coordinator подтвердить инфраструктурную причину
     сбоя подготовки и выполнить ограниченный повтор (`attention_policy.max_infrastructure_retries`).
2. Замените модели и effort на доступные вам.
3. Если `auto` вам не подходит, выберите `manual_all`, `milestone` или `low_risk`: для `low_risk`
   сузьте `low_risk_paths`.
4. При необходимости сузьте `write_paths` ролей: потолок записи ограничивает `--allowed-path` batch.
5. Выполните `harness health`.

## Частые команды

```bash
python .harness/orchestration/coordinator.py --repo . ledger status
python .harness/orchestration/coordinator.py --repo . batch preflight --ticket '#123' --allowed-path 'src/**' ...
python .harness/orchestration/coordinator.py --repo . batch create --ticket '#123' --branch feature/issue-123-x --worktree <путь> --allowed-path 'src/**' ...
python .harness/orchestration/coordinator.py --repo . dispatch create --batch <batch_id> --role developer
python .harness/orchestration/coordinator.py --repo . batch decide --batch <batch_id> --decision accept --approved-by <кто> --approved-at <ISO-время>
python .harness/orchestration/coordinator.py --repo . batch list --open
```

Полный список подкоманд — `python .harness/orchestration/coordinator.py --help`. Те же команды
для оператора доступны в разделе Orchestration `harness console`.

## Восстановление остановленного batch

`batch auto-report` читает evidence и показывает `observed_stop`; команда ничего не записывает.
`batch auto-decide` явно записывает остановку как `paused`, в том числе без report. Повтор команды
не создаёт новую остановку. `blocked` и `failed` сохраняют работу и допускают ручное восстановление.
Терминальные состояния — `completed`, `abandoned` и старое `not-required`.

- `batch resume-stop --batch <id> --approved-by <оператор> --approved-at <UTC> --note <причина>`
  готовит повтор того же этапа после доказанного сбоя запуска. Каждый следующий шаг требует
  человеческого approval; исходный `auto_stop` остаётся историей.
- `batch rewind --batch <id> --to code-review --approved-by <оператор> --approved-at <UTC> --note <причина>`
  возвращает к достигнутому этапу. Git history не меняется. Старые зависимые risk/review/QA
  перестают разрешать продолжение. Новая работа developer расходует оставшийся retry budget.
- `dispatch cancel --dispatch <id> --runtime-stopped --approved-by <оператор> --approved-at <UTC> --reason <причина>`
  записывает подтверждение, что оператор остановил runtime. Timeout такого подтверждения не даёт.
- `ledger validate` проверяет поколение, audit и evidence без исправления или миграции ledger.

Новые recovery-команды работают на установленном runtime. Ручное событие связывает старый pin с
новым control runtime; plan и прежние brief/report не переписываются. Ранний 429 writer-а сохраняет
известный чистый startup SHA отдельным evidence без фиктивного checkpoint. После recovery даже
429 требует человеческого approval. Подробный контракт: [Operator recovery](playbook.md#operator-recovery).
