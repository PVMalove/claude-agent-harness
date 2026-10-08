# Backend-оркестрация

Модуль capability `backend-orchestration`: ведёт один тикет через роли
`architect → developer → code-review → qa → publish` с проверяемыми переходами, неизменяемыми
brief и report, независимым review и clean-room QA. Capability выбирается явно и расширяет
`pvmalove-suite`; проекты без неё не меняются.

Оркестрация — не автономный scheduler. Coordinator (человек или назначенная им управляющая сессия)
создаёт batch, утверждает каждый dispatch и принимает или отклоняет каждый report согласно
`approval_policy`. Роли не расширяют свой scope, не выбирают модель и не мержат PR.

Пошаговая процедура настройки и запуска — в
[руководстве по backend-оркестрации](../docs/backend-orchestration.md), полный контракт
lifecycle — в [playbook.md](./playbook.md), границы ролей — в [roles/](./roles/).

## Состав

| Путь | Назначение |
| --- | --- |
| `coordinator.py` | CLI coordinator: разбор аргументов, маршрутизация и JSON-вывод. Логика — в `core/`, `ledger/`, `workflow/`. |
| `core/` | Константы и значения по умолчанию, чтение конфига, git, workspace. |
| `ledger/` | Локальное хранилище state: поколения ledger, неизменяемые записи, audit, миграции. |
| `workflow/` | По модулю на стадию batch: планирование, preflight, brief, dispatch, решения, отчёты, доставка. |
| `contract.py` | Проверка конфигурации, role manifest'ов, brief и report; источник `harness health` для оркестрации. |
| `qa_lane.py` | Очередь clean-room QA: один QA за раз, аренда с истечением. |
| `runtime_access.py`, `operation_access.py` | План доступа (`access_policy`) и его проверка перед QA, Git и publish, которые выполняет сам coordinator. |
| `dispatch_preflight.py`, `runtime_attestation.py`, `operational_guards.py` | Проверка worktree, ветки и SHA воркера; защитные проверки перед dispatch. |
| `advisory.py`, `extensions.py` | Advisory-вызовы и подключаемые extensions (health, классификатор retry, уведомления). |
| `roles/` | Role manifest'ы: режим (`read-only`/`write`), требуемые capability, risk triggers. |
| `playbook.md`, `pilot.md` | Полный lifecycle и форма наблюдения за первыми batch. |
| `orchestration.schema.json` | JSON Schema проектного конфига для подсказок редактора. |

В целевом проекте модуль лежит в `.harness/orchestration/` и обновляется `harness update`. Рядом
находятся управляемый пример `.harness/orchestration.example.json` и собственный конфиг проекта
`.harness/orchestration.json`.

## Как это работает

1. **Batch.** `batch create` (сначала `batch preflight`) фиксирует тикет, явный scope записи
   (`--allowed-path`), issue-ветку, worktree, Definition of Done, запреты и проверки. Batch один на
   тикет, в своём worktree; batch с пересекающимися файлами идут параллельно, пока хватает
   `concurrency_budget`.
2. **Dispatch.** Для каждой роли coordinator создаёт dispatch с неизменяемым brief: роль, runtime,
   модель, effort, `write_paths` (scope batch), `allowed_tools`, `context_budget`, проверки, Context Package и политика.
   Правка конфига не меняет уже созданный brief.
3. **Работа роли.** Роль подтверждает свою фактическую модель (self-report) и, если включено,
   worktree/ветку/SHA (attestation), шлёт heartbeat и сдаёт completion report на русском.
4. **Решение.** Coordinator принимает report (`batch decide --decision accept`), отправляет на
   retry с маршрутизацией по структурным данным report, блокирует или завершает batch. При
   `approval_policy` `milestone`/`low_risk`/`auto` чистые report без рисков принимаются автоматически.
5. **QA и publish.** Clean-room QA выполняет `qa_preparation` (если задан), затем `verification_commands`
   на закреплённом candidate SHA в общей очереди; провал подготовки не считается провалом проверок кода. Publish выдаёт принятый SHA; PR открывает человек через `/to-pull-requests`.
6. **Integration accounting.** После accepted publish `integration prepare` записывает связь
   тикет, ветка, source batch, опубликованный candidate SHA и target SHA; `integration status`
   только наблюдает `stale` без dispatch, а `integration link-evidence` принимает будущие CI,
   local-QA и resolver результаты. Завершённый batch не переписывается.

Состояния batch: `planned → awaiting-approval ↔ active → completed | blocked | failed | abandoned`.
`needs_attention` — не состояние, а флаг: он останавливает следующий dispatch при зависшем воркере,
исчерпанных retry или долгой очереди. Всё локальное state хранится в gitignored
`.harness/orchestration/state/`.

## Файлы конфигурации

| Файл | Кто владеет | Когда меняется |
| --- | --- | --- |
| `.harness/orchestration.example.json` | Харнесс (managed) | Ставится и обновляется при `init`, `adopt`, `update`; drift виден в `harness diff`. |
| `.harness/orchestration.json` | Проект | Создаётся копией примера при первой установке, если его нет; дальше только ваши правки (`--force-seed-files` перезаписывает). |

Новые поля и значения из обновлённого примера в свой конфиг переносите вручную. После любой правки
выполните `harness health` — он проверяет конфиг полностью: схему полей, ссылки на профили,
совместимость capability с role manifest'ами, fallback и обязательный `code-review`.

**Без конфига** coordinator тоже работает: потолок записи ролей — весь репозиторий, а границу
задаёт `--allowed-path` batch; проверки берутся из `qa_gate_commands` в `.harness/project.json`, бюджет параллелизма 1, `model` и
`effort` передаются в `dispatch create`, транспорт только `in-process`. Конфиг, в котором нет
ни назначений, ни зон, означает то же самое.

## Справочник `orchestration.json`

Обязательны `provider_profiles`, `assignment_plans`, `concurrency_budget` и
`verification_commands`; остальные поля необязательны и без значения берут default из таблиц.
Неизвестные поля отклоняются.

### Верхний уровень

| Поле | Тип | По умолчанию | Назначение |
| --- | --- | --- | --- |
| `$schema` | строка | — | Путь к схеме для редактора: `./orchestration/orchestration.schema.json`. |
| `provider_profiles` | объект | — | Профили агентов: имя → capability, fallback, ограничения. |
| `assignment_plans` | объект | — | Назначение каждой используемой роли: потолок записи, runtime, модель, effort. |
| `backend_zones` | объект | — | Устаревшее, необязательное: имя → `paths` (glob). Больше не блокирует параллельные batch; существующий конфиг с зонами остаётся валидным. |
| `concurrency_budget` | целое ≥ 1 | 1 | Сколько batch могут быть активны одновременно. Единственный предел параллелизма: пересечение файлов и совпадение зон его не заменяют. |
| `verification_commands` | список строк | `[]` | Полный gate clean-room QA. Пустой список — QA без проверок; впишите реальные команды проекта. |
| `qa_preparation` | список строк | `[]` | Команды подготовки окружения clean-room QA (установка зависимостей и т.п.): выполняются в том же checkout кандидата до `verification_commands`, остановка на первой упавшей. Без поля QA сразу гоняет gate, report не меняется. |
| `qa_environment_probes` | список строк | `[]` | Независимые пробы окружения QA (например, доступность реестра): выполняются только после упавшей `qa_preparation`. Упавшая проба при успешных `qa_project_file_checks` подтверждает инфраструктурную причину. |
| `qa_project_file_checks` | список строк | `[]` | Офлайн-проверки файлов проекта (например, согласованность lock-файла): выполняются только после упавшей `qa_preparation`. Упавшая проверка подтверждает дефект проекта. Без проб и проверок инфраструктурная причина не подтверждается. |
| `developer_verification_commands` | список строк | = `verification_commands` | Быстрые проверки developer. Без поля developer гоняет полный gate, `harness health` предупреждает. |
| `review_verification_commands` | список строк | = `verification_commands` | Проверки code-review. |
| `test_path_patterns` | список glob | `tests/**`, `**/tests/**`, `**/test_*.py`, `**/*_test.py` | Какие пути считаются тестами (delta-review при изменении только тестов). |
| `approval_policy` | `manual_all` \| `milestone` \| `low_risk` \| `auto` | `manual_all` | Какие report принимаются без человека (см. ниже). |
| `low_risk_paths` | список glob | — | Пути в форме `dir/**`, `**` или точного файла (без `./`, `//`, `..`; сравнение по сегментам), внутри которых при `low_risk` чистые report принимаются автоматически: весь `--allowed-path` batch должен лежать в них. Без списка ничто не считается низкорисковым. |
| `low_risk_zones` | список имён зон | — | Устаревшее: зоны из `backend_zones`, отображаются на свои пути как `low_risk_paths`. Batch, запланированный до явного scope, по-прежнему определяется своей зоной. |
| `human_approval_gate` | `trusted` \| `tty` | `trusted` | `trusted` — approval через `--approved-by/--approved-at`; `tty` — только интерактивное подтверждение в терминале. |
| `approval_ttl_seconds` | целое ≥ 1 | без срока | Срок жизни `--approved-at`: более старое или датированное будущим approval отклоняется. |
| `worker_attestation_required` | логическое | `false` | Воркер до работы подтверждает фактический worktree, ветку и SHA. Пример включает `true`. |
| `communication_policy` | объект | `en` / `ru` | `agent_to_agent_language` всегда `en`, `coordinator_report_language` всегда `ru`. |
| `tool_policy` | объект | по режиму роли | Рабочий набор инструментов роли в brief (см. ниже). |
| `extensions` | объект | все `none` | Подключаемые интерфейсы вне ядра (см. ниже). |

Политики `context_package_policy`, `adaptive_continuation_policy`, `repo_map_policy`,
`continuation_policy`, `retry_policy`, `execution_policy`, `attention_policy` и `preflight_policy`
описаны отдельно. В каждой можно задать только часть полей: остальные берут default.

### `provider_profiles.<имя>`

| Поле | Назначение |
| --- | --- |
| `capabilities` | Непустой список возможностей: `architecture-analysis`, `backend-development`, `code-review`, `independent-verification`, `database-migrations`, `messaging-integration`, `conflict-resolution`. Профиль роли должен покрывать `required_capabilities` её manifest'а. |
| `fallback` | Упорядоченные имена резервных профилей при безопасном отказе основного. Runtime brief при failover не меняется. |
| `known_limitations` | Известные ограничения профиля (текст). |

### `assignment_plans.<роль>`

Роли — имена manifest'ов: `architect`, `developer`, `code-review`, `qa`, `verification`,
`database-migrations`, `messaging-integration`, `conflict-resolver`. Если назначения заданы, `code-review` обязателен.

| Поле | Назначение |
| --- | --- |
| `write_paths` | Потолок записи роли (по умолчанию весь репозиторий). `--allowed-path` batch не может быть шире; brief и report проверяются по scope batch, а `changed_files` developer вне scope — предупреждение, принимаемое только через `override-warning`. |
| `zone` | Устаревшее, необязательное имя зоны из `backend_zones`: без `write_paths` потолок — пути этой зоны. |
| `transport` | `in-process` (по умолчанию: субагент coordinator-сессии) или `external` (проектный runtime adapter). |
| `runtimes` | Именованные наборы (`claude`, `codex`, …): у каждого `profiles`, `model`, `effort`. |
| `default_runtime` | Runtime по умолчанию, если их несколько. Без него `dispatch create` требует `--runtime`. |

`model` — CLI-алиас или ID без пробелов. `effort`: `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, `max`, `ultra`. Выбранные runtime, модель и effort записываются в brief; роль сверяет
с ними свою фактическую модель.

### `approval_policy`

| Значение | Поведение |
| --- | --- |
| `manual_all` | Каждый report принимает человек. |
| `milestone` | Чистый report (`completed`, без рисков, блокеров, risk triggers и упавших проверок) принимается автоматически, кроме QA, publish и batch с совпавшими risk triggers. |
| `low_risk` | То же, но только для batch, чей `--allowed-path` целиком лежит в `low_risk_paths`. |
| `auto` | Координатор сам принимает чистый report любого batch, включая чистый QA, и готовит следующий dispatch; `low_risk_zones` не применяются. Решение пишется как `policy:auto`. Ручными остаются publish, PR, batch с совпавшими risk triggers, findings, упавшие проверки и report с блокерами или рисками. |

Политика фиксируется в batch при создании; её смена не влияет на уже созданные batch.

### `tool_policy`

Без поля роль получает набор по режиму manifest'а: `read-only` — `Read`, `Grep`, `Glob`, `Bash`;
`write` — те же плюс `Edit`, `Write`. Переопределение: `modes` (ключи `read-only`/`write`) и `roles`
(имена ролей); запись роли важнее записи режима. Это рабочий набор роли, а не запрет глобальных
инструментов runtime.

```json
"tool_policy": {"modes": {"read-only": ["Read", "Grep", "Glob"]}, "roles": {"qa": ["Read", "Grep", "Glob", "Bash"]}}
```

### `context_package_policy`

| Поле | По умолчанию | Назначение |
| --- | --- | --- |
| `max_tokens` | 200000 | Потолок оценки токенов Context Package. |
| `context_window_tokens` | 250000 | Окно контекста целевой модели. |
| `reserved_prompt_tokens` | 20000 | Резерв под системные инструкции. |
| `symbol_graph_depth` | 2 | Глубина import-графа для каждого автоматического пакета. |
| `max_related_tests` | 25 | Предохранитель: больше связанных тестов — ошибка сборки пакета. |
| `min_starting_files` / `max_starting_files` | 5 / 10 | Число стартовых файлов при ручной регистрации пакета. |
| `section_index_min_tokens` | 20000 | С какого размера Markdown попадает в пакет оглавлением, а не целиком. |

### `adaptive_continuation_policy`

| Поле | По умолчанию | Назначение |
| --- | --- | --- |
| `context_limit` | 150000 | Бюджет контекста роли (`context_budget` в brief); больший пакет dispatch отклоняет. |
| `context_warn_ratio` | 0.8 | Доля `context_limit`, после которой фиксируется давление контекста. |
| `tdd_cycle_count` | 3 | Число TDD-циклов до checkpoint и новой worker session. |
| `failure_log_bytes` | 20000 | Размер лога ошибок, после которого предлагается продолжение. |

### `continuation_policy`, `retry_policy`, `execution_policy`

| Поле | По умолчанию | Назначение |
| --- | --- | --- |
| `continuation_policy.max_continuations` | 2 | Продолжений worker session на один dispatch. |
| `continuation_policy.max_rate_limit_resumes` | 1 | Автоматических возобновлений после rate limit. |
| `retry_policy.max_developer_retries` | 1 | Повторов developer после отклонённого review или QA (`0` — ни одного). |
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
| `heartbeat_interval_seconds` | 300 | Желаемый интервал heartbeat; фактически не больше трети `stale_dispatch_seconds`. |

### `preflight_policy`

Ограничивает объём тикета до создания batch; превышение — повод разделить тикет через
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

Фильтрация и лимиты карты репозитория для Context Package: `allow_paths`, `deny_paths`,
`redact_paths`, `redact_symbols`, `max_files` (10000), `max_file_bytes` (2000000),
`max_path_length` (4096), `max_symbol_length` (256), `max_signature_length` (2048),
`timeout_seconds` (10), `max_tokens` (4000), `tier` (`full`/`minimal`), `min_tier` и
`min_tier_by_role` (гейт dispatch по tier), `parser_bundle_registry_paths`,
`parser_bundle_timeout_seconds` (30), `parser_bundle_max_output_bytes` (10000000). Подробно — в
[README Repo Map](../repo_map/README.md).

### `extensions`

Ключи: `transport_health`, `verification_environment_health`, `retry_reason_classifier`,
`context_telemetry_provider`, `human_notifier`. Значение — `none` (по умолчанию), имя,
зарегистрированное хост-процессом, или `module:factory`. Неизвестное имя — ошибка. Extensions не
добавляют модели инструментов и не меняют системный промпт.

### Доступ QA, Git и publish

Операции `qa`, `git` и `publish` выполняет сам coordinator, а не worker и не runtime adapter. Если в
проекте есть `access_policy`, перед действием coordinator выбирает план операции (закреплённый в
approved brief для `qa run` и `dispatch publish`, из живого конфига — для `integration local-qa`,
`integration refresh` и `integration resolve`) и проверяет его на своём процессе: режим, разрешение
плана, запись в Git metadata, общее хранилище, каталог clean-room checkout или checkout batch, пути
`cache` и remote. Override роли (`roles`) такой операции не применяется. Отказ, неподдерживаемый
режим и непроверенное требование останавливают действие до любых изменений и называют ресурс, путь
и средство исправления. Подробности и диагностика — в
[руководстве](../docs/backend-orchestration.md#доступ-qa-git-и-publish).

## Пример

Управляемый пример [`orchestration.example.json`](../orchestration.example.json):

- профили `claude-profile` и `codex-profile`;
- назначения architect, developer, code-review и qa на двух runtime;
- потолок записи ролей — весь репозиторий (зон нет);
- `approval_policy: low_risk` с `low_risk_paths: ["**"]`;
- расширенные лимиты контекста и preflight, `approval_ttl_seconds: 14400`;
- пустые списки проверок.

Перед первым batch:

1. Впишите реальные проверки в `verification_commands` (и быстрые — в
   `developer_verification_commands`).
2. Замените модели и effort на доступные вам.
3. Задайте `default_runtime` ролям, которые должны идти через один runtime без `--runtime`.
4. Сузьте `low_risk_paths` и `write_paths`, если автопринятие или запись должны быть уже; независимые
   batch зон не требуют, их ограничивает `concurrency_budget`.

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
