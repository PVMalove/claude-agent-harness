# Руководство по backend-оркестрации

`backend-orchestration` — opt-in capability для согласованной backend-разработки несколькими
ролями. Она расширяет `pvmalove-suite`, но не является автономным scheduler: coordinator (человек
или назначенная им управляющая сессия) планирует batch и ведёт переходы согласно настроенной
политике approval. Роли не расширяют свой scope, не выбирают модель и не мержат pull request.

Целостный действующий контракт capability, включая её место в системе, роли, clean-room QA и
локальное state-хранилище, приведён в [backend-orchestration.md](./backend-orchestration.md). Этот документ
содержит подробную процедуру настройки и запуска.

Используйте её, когда у задачи есть независимые backend-границы или обязательная независимая
проверка. Для обычной одной задачи достаточно стандартного pipeline `pvmalove-suite`.

## Владение правилами

`/implement` — короткий контракт coordinator-а: он сохраняет порядок handoff
`architect → developer → code-review → qa → publish`, approval согласно политике,
model self-report и watchdog. Он не является второй копией процедуры.

Полные правила принадлежат устанавливаемым модулям: `playbook.md` — lifecycle, authority,
immutable brief, evidence, параллелизм и метрики; `roles/` — границы и доказательство каждой роли;
`coordinator.py` — проверяемые переходы и audit; проектный adapter — только transport. При
противоречии приоритет у этих module-owned guidance и immutable records, а не у runtime adapter-а
или краткого skill.

Токены — только наблюдаемая provider- или runtime-telemetry с источником и missing-data note.
Role self-report, completion report и оценка coordinator-а не являются token telemetry и не могут
заполнять отсутствующее значение.

## Что устанавливается

При выборе capability в проект копируются:

- `.harness/orchestration/roles/` — переносимые manifest'ы ролей и общий контракт;
- `.harness/orchestration/playbook.md` — полный lifecycle, handoff и правила параллелизма;
- `.harness/orchestration/pilot.md` — форма наблюдения за первыми batch;
- `.harness/orchestration/coordinator.py` — runtime-neutral CLI для batch, approval, dispatch и report;
  сам файл — только фасад: разбор аргументов, роутинг и вывод JSON. Сам lifecycle лежит рядом в
  `core/` (константы, конфигурация, git, workspace), `ledger/` (persistence) и `workflow/`
  (по модулю на стадию batch'а: планирование, бриф, доставка, решение, отчёт);
- `.harness/orchestration.json` — project-owned конфигурация назначений, потолка записи и проверок.

Coordinator state, immutable briefs/reports и санитизированные QA-артефакты создаются локально в
`.harness/orchestration/state/`; содержимое этой директории gitignored и не является исходным
кодом проекта. Оно остаётся локальным evidence до явного решения coordinator-а о безопасной очистке:
роль и adapter не удаляют историю batch.

Внутренний протокол имеет фиксированные языки: agent-to-agent handoff, checkpoint, state evidence и
свободный текст в `.harness/orchestration/state/` пишутся на английском; completion report,
адресованный coordinator-у, — на русском и содержит `"report_language": "ru"`. Команды, пути, SHA,
имена тестов и цитаты исходных требований не переводятся. Это уменьшает двусмысленность между
разными runtime и оставляет отчёт человеку читаемым.

Перед первым английским handoff coordinator и workers обязаны прочитать общий контракт
[Technical English](./technical-english.md), доступный также через playbook и `roles/_common.md`.
Его область, языковые исключения и примеры review описаны в самом источнике; действующие правила
языка, authority, scope, привязки evidence к candidate SHA и human approval сохраняются.

Manifest определяет режим роли (`write` или `read-only`), capability и risk triggers. Проектный
конфиг выбирает agent/fallback на уровне provider profile, а `model` и `effort` — отдельно для
каждой роли в её assignment plan, вместе с потолком записи, бюджетом параллелизма и командами проверки; он не может
ослабить границы manifest'а. Значения секретов не хранятся ни в конфиге, ни в brief, ни в report.

## 1. Включение

Для нового git-репозитория выберите только `backend-orchestration`: зависимость от
`pvmalove-suite` будет разрешена автоматически.

```bash
python3 harness/bin/harness.py init /path/to/repository \
  --project-type software \
  --stack python \
  --capability backend-orchestration \
  --base-branch main \
  --language ru \
  --qa-gate-command "python -m pytest"
```

В PowerShell замените `python3` на `python`, а `\` на обратную кавычку. Не передавайте отдельно
`pvmalove-suite`: capability уже содержит её через `extends`.

Чтобы включить её в существующем проекте с харнессом, сначала посмотрите локальный drift, затем
обновите выбранный набор capability:

```bash
python3 harness/bin/harness.py diff /path/to/repository
python3 harness/bin/harness.py update /path/to/repository --capability backend-orchestration
```

`update` не перезаписывает изменённые managed files без явного флага. `--force-managed-files`
обновляет только managed snapshot и сохраняет seed-документы; `--force-seed-files` перезаписывает
только seed, а `--force` объединяет оба действия. После любого
включения или изменения конфигурации выполните:

```bash
python3 harness/bin/harness.py health /path/to/repository
```

## 2. Настройка `.harness/orchestration.json`

Конфиг **не обязателен**. Без него coordinator работает на дефолтах: потолок записи ролей —
весь репозиторий, границу даёт `--allowed-path` batch, `verification_commands` берутся из `qa_gate_commands` в
`.harness/project.json`, `concurrency_budget` равен 1, а `model`/`effort` роли приходят из вызывающей
сессии (`dispatch create --model <model> --effort <effort>`). Транспорт в этом режиме всегда
`in-process`: provider profile нет, значит и внешний worker запускать нечем. `harness health` такой
проект принимает. Конфиг нужен, когда проекту нужен более узкий потолок записи, разные модели по ролям,
внешний транспорт или бюджет параллелизма больше единицы. Справочник всех полей с дефолтами —
[`.harness/orchestration/README.md`](../orchestration/README.md).

Запускайте coordinator-сессию с `medium` effort по умолчанию. Для architect в assignment plan также
выбирайте `medium`; более высокий effort требует явного решения разработчика для названного
труднообратимого вопроса, а не является дефолтом каждого ticket.

`init` создаёт конфиг как копию управляемого примера `.harness/orchestration.example.json`:
provider profiles `claude-profile` и `codex-profile`, назначения architect/developer/code-review/qa на
двух runtime, потолок записи на весь репозиторий и `approval_policy: low_risk` с `low_risk_paths: ["**"]`. Модели и effort в
примере — ориентир, замените их на свои. Списки проверок в примере пустые: впишите реальные project
checks в `verification_commands` (и при желании в `developer_verification_commands` и
`review_verification_commands`). У ролей два runtime без `default_runtime`, поэтому
`dispatch create` требует `--runtime`, пока вы не зададите `default_runtime`. Пример обновляется
при каждом `update`/`adopt`, а ваш `.harness/orchestration.json` — никогда: новые поля и значения
переносите из примера вручную. Правьте provider profiles, `write_paths` ролей, назначение для **каждой**
используемой роли и project checks под свой проект. `code-review` следует
назначить всегда: validator требует его, когда в конфиге есть назначения, поскольку это
обязательный gate для high-risk работы.

Ниже минимальный полный пример. Имена model и effort принадлежат конкретному проекту.
Fallback задаётся в provider profile. Assignment plan каждой роли содержит именованные
runtime-наборы (`codex`, `claude` и т.п.); в каждом обязательны `profiles`, `model` и `effort`.
Выбранный runtime фиксируется в immutable brief и не меняется при failover profile.

Если у роли несколько runtime, задайте `default_runtime` в её assignment plan; иначе каждый
`dispatch create` обязан явно передать `--runtime`. Скрытого fallback на Codex нет. Для review,
который проект всегда хочет запускать через Claude, это выглядит как
`"default_runtime": "claude"` рядом с `runtimes`. Пример также включает
`"worker_attestation_required": true`: worker до любой работы подтверждает свой фактический Git
worktree, branch и SHA; legacy projects могут включить это поле постепенно.

```json
{
  "$schema": "./orchestration/orchestration.schema.json",
  "provider_profiles": {
    "backend-primary": {
      "capabilities": [
        "backend-development",
        "architecture-analysis",
        "independent-verification",
        "database-migrations",
        "messaging-integration",
        "conflict-resolution",
        "code-review"
      ],
      "fallback": ["backend-fallback"],
      "known_limitations": ["Проект сам фиксирует доступные runtime limits"]
    },
    "backend-fallback": {
      "capabilities": [
        "backend-development",
        "architecture-analysis",
        "independent-verification",
        "database-migrations",
        "messaging-integration",
        "conflict-resolution",
        "code-review"
      ],
      "fallback": [],
      "known_limitations": ["Использовать только после безопасного отказа primary"]
    }
  },
  "assignment_plans": {
    "architect": {"runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-architect-model", "effort": "medium"}, "claude": {"profiles": ["backend-claude"], "model": "project-architect-claude-model", "effort": "medium"}}},
    "developer": {"write_paths": ["services/payments/**"], "transport": "external", "runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-developer-model", "effort": "xhigh"}, "claude": {"profiles": ["backend-claude"], "model": "sonnet", "effort": "xhigh"}}},
    "database-migrations": {"runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-migration-model", "effort": "xhigh"}, "claude": {"profiles": ["backend-claude"], "model": "project-migration-claude-model", "effort": "xhigh"}}},
    "messaging-integration": {"runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-messaging-model", "effort": "high"}, "claude": {"profiles": ["backend-claude"], "model": "project-messaging-claude-model", "effort": "high"}}},
    "conflict-resolver": {"runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-resolver-model", "effort": "high"}, "claude": {"profiles": ["backend-claude"], "model": "project-resolver-claude-model", "effort": "high"}}},
    "qa": {"runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-qa-model", "effort": "medium"}, "claude": {"profiles": ["backend-claude"], "model": "project-qa-claude-model", "effort": "medium"}}},
    "code-review": {"runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-review-model", "effort": "high"}, "claude": {"profiles": ["backend-claude"], "model": "project-review-claude-model", "effort": "high"}}}
  },
  "concurrency_budget": 1,
  "developer_verification_commands": ["python -m pytest tests/unit"],
  "review_verification_commands": ["python -m pytest tests/unit"],
  "verification_commands": ["python -m pytest"]
}
```

`transport` — необязательное поле assignment plan и выбирается для каждой роли отдельно: `in-process`
(по умолчанию) исполняет роль как
субагента текущей coordinator-сессии в worktree того же batch. Оба варианта получают один и тот же
immutable brief, обязаны пройти model self-report и вернуть completion report по общим правилам,
поэтому логика coordinator-а от транспорта не зависит. `harness health` проверяет допустимость
значения. Для внешнего worker укажите `"transport": "external"` явно и передайте проектный adapter. Для `in-process` `dispatch send` только фиксирует handoff: следующим действием coordinator
немедленно запускает субагента по уже immutable brief, до любого поиска старых report/template или
конфигурации. Architect собирает лишь targeted evidence для решения: его brief не содержит команд
проверки, а отчёт сдаёт пустой `checks_run`. Полный набор
`verification_commands` выполняет clean-room QA, а developer получает
`developer_verification_commands`. Это необязательное поле: без него сохраняется совместимый
режим, в котором developer получает полный список. Задавайте в нём быстрые task-scoped проверки,
а в `verification_commands` — независимый полный gate. `review_verification_commands` так же
необязателен и управляет только code-review; без него review получает полный список. Если
`developer_verification_commands` не задан, `harness health` предупреждает, что developer будет
гонять полный gate на каждой итерации. Code-review
запускает каждую полученную команду через `test_summary.py`: в report остаются исходная команда и
bounded summary, а санитизированный полный лог доступен только для упавшей проверки.

Граница записи — не подсказка: write-роль изменяет только пути, явно закреплённые за batch
(`--allowed-path`, они попадают в `write_paths` brief), и только внутри потолка роли. Потолок задаёт
`write_paths` роли в assignment plan (по умолчанию весь репозиторий): batch шире потолка не
создаётся, а brief и completion report проверяются по scope batch. Model должен быть CLI-алиасом
или ID без пробелов (например, `sonnet`), а не отображаемым названием. Параллельные batch зон не
требуют: у каждого свои issue-ветка и worktree, а число одновременно активных ограничивает
`concurrency_budget`; увеличивайте его только после явного решения coordinator-а. Устаревшие
`backend_zones`, `zone` в плане роли и `low_risk_zones` остаются валидными и отображаются на пути, но
новому проекту они не нужны. Сначала прогоните `harness health`: он проверит JSON, существование
profile, совместимость capability, fallback и режим `code-review`.

### Бюджет контекста и preflight

Новый batch не создаётся, пока `batch preflight` (он же автоматически вызывается из `batch create`)
не подтвердит ограниченный scope. Укажите ожидаемые changed paths, один bounded context/service и
консервативный размер diff; значения выше project policy нужно сначала разделить через
`/to-tickets`, а не передавать в architect как discovery-задачу:

```bash
python .harness/orchestration/coordinator.py --repo . batch preflight \
  --ticket '#123' --allowed-path 'services/payments/**' \
  --definition-of-done 'Добавить валидацию платежа' \
  --expected-file services/payments/validation.py \
  --expected-service payments --expected-changed-lines 120
```

Без `preflight_policy` coordinator ограничивает DoD (5), dependencies (3), файлы (12), сервисы (1),
diff (800 строк) и ожидаемый context (80k tokens); пример `orchestration.example.json` задаёт
8/5/25/2/2000/150k. Проект настраивает эти значения через `preflight_policy`.
`context_package_policy` использует консервативную token estimate и резервирует место для системных
инструкций; байтовый предел остаётся только диагностической совместимостью. `symbol_graph_depth`
(по умолчанию 2) в этой политике управляет глубиной import-графа для *каждого* автоматического
Context Package — включая пакеты, которые coordinator строит сам при `dispatch create` для
architect/developer/code-review, а не только для ручного `context-package register`.
`--max-package-tokens` на `context-package register` — это только более строгий потолок для одного
пакета: значение выше сконфигурированного `context_package_policy.max_tokens` coordinator отклоняет
с ошибкой, а не применяет молча — лимит поднимается только правкой `context_package_policy.max_tokens`
в конфиге проекта, осознанно и с прохождением `harness health`. `max_related_tests` (по умолчанию 25)
— отдельный fail-loud предохранитель: если import-граф стартовых файлов задевает больше
related_tests, чем этот предел, сборка пакета завершается `ContextPackageError` вместо того, чтобы
молча утащить в оценку токенов половину test suite; `--max-related-tests` переопределяет его для
одного ручного `context-package register`. Один immutable shared
Context Package переиспользуется всеми role sessions на том же base/candidate; новый строится только
при новом candidate. `continuation_policy` (2 continuations, из них максимум один automatic 429
resume) и `retry_policy` (один developer retry) делают циклы конечными. Все поля проверяются
`harness health`; effort допускает только документированные уровни (`none`…`ultra`).

### Tool policy и context budget в brief

Кроме model/effort и `verification_commands`, каждый immutable brief явно записывает два значения,
выбранных из `.harness/orchestration.json`, а не из чата:

- `allowed_tools` — рабочий набор инструментов именно этой роли. Без `tool_policy` берётся дефолт по
  режиму manifest: `read-only` (architect, code-review, qa) — `Read`, `Grep`, `Glob`, `Bash`, без
  `Edit`/`Write`; `write` — те же плюс `Edit` и `Write`. Read-only роль работает по Context Package,
  не сканирует весь репозиторий и не подгружает нерелевантные инструменты. Список — рабочий набор
  роли, а не deny-list: brief не отключает глобальные инструменты runtime.
- `context_budget` — токены из `adaptive_continuation_policy.context_limit` (по умолчанию 150000):
  тот же порог, от которого считается `context_advisory`. `dispatch create` (и `--propose`)
  отклоняет автоматический Context Package, чья `estimated_tokens` больше этого бюджета, даже если
  она укладывается в `context_package_policy.max_tokens`: потолок пакета может быть выше бюджета
  роли, но роль должна получить пакет, который помещается в её контекст.

Проект переопределяет набор необязательным `tool_policy`; запись роли важнее записи режима, а она —
встроенного дефолта:

```json
"tool_policy": {
  "modes": {"read-only": ["Read", "Grep", "Glob"]},
  "roles": {"qa": ["Read", "Grep", "Glob", "Bash"]}
}
```

`harness health` принимает только ключи `modes` (`read-only`/`write`) и `roles` (имена из role
manifests), а значением — непустой список уникальных строк. Brief без этих двух полей (созданный до
их появления) остаётся валидным; brief с одним из двух или с некорректной формой значений
отклоняется. Brief — неизменяемая запись выбора на момент approval, поэтому позднейшая правка
`tool_policy` или `context_limit` не делает уже созданный dispatch невалидным: новые значения
попадут только в следующие brief.

### Политика операционных циклов: attention, approval TTL и extensions

Три необязательных раздела `.harness/orchestration.json` управляют тем, как coordinator останавливает
зацикленные retry и как проверяет approval. Пропущенное поле берёт дефолт; resolved-значения
записываются в каждый brief как `orchestration_policy`, поэтому правка файла посреди dispatch не
меняет то, под чем он был утверждён:

```json
"attention_policy": {"retry_queue_seconds": 3600, "max_infrastructure_retries": 2, "stale_dispatch_seconds": 3600},
"approval_ttl_seconds": 3600,
"extensions": {"transport_health": "none", "verification_environment_health": "none",
               "retry_reason_classifier": "none", "context_telemetry_provider": "none", "human_notifier": "none"}
```

- `attention_policy` — пороги флага `needs_attention` (см. ниже): сколько принятый retry может ждать
  своего dispatch, сколько operational retry (`verification-infrastructure`, `transport`,
  `context-pressure`) допустимо на один candidate (`0` — ни одного) и после какого молчания живой
  dispatch считается stale.
- `approval_ttl_seconds` — срок жизни явного `--approved-at`. Более старое (или датированное в
  будущем) approval отклоняется и не используется повторно. Без поля approval не истекает; пример
  `orchestration.example.json`, из которого `harness init` создаёт конфиг, задаёт `14400`.
- `extensions` — подключаемые интерфейсы вне ядра coordinator: `transport_health`,
  `verification_environment_health`, `retry_reason_classifier`, `context_telemetry_provider`,
  `human_notifier`. Значение — `none` (инертный дефолт), имя, зарегистрированное хост-процессом, или
  `module:factory` (вызываемая без аргументов фабрика в импортируемом модуле). Неизвестное имя —
  fail-closed. Ни один extension не добавляет model tool и не меняет system prompt.

`harness health` проверяет форму всех трёх разделов. Пока у проекта нет
`assignment_plans`, coordinator работает на встроенных дефолтах и эти значения не читает.

### Discovery Context и Context Package

Discovery Pipeline переносит проверенный контекст от проектирования к dispatch. `/grilling` ведёт
`Live Artifact` с кандидатными путями, но добавляет путь только после явного согласия пользователя.
`/to-spec` сохраняет утверждённый список в эпике под `## Relevant Files (Discovery Context)`, а
`/to-tickets` назначает каждый путь подходящему tracer-bullet тикету и строит Path
inventory. Один cheap advisory-вызов может добавить только точные зависимости из этого Path inventory; его
вывод не является evidence или authority.

Перед первым dispatch coordinator может зарегистрировать детерминированный Context Package в
ledger:

```bash
python .harness/orchestration/coordinator.py --repo . context-package register \
  --batch <batch-id> --candidate-commit <candidate-sha> \
  --symbol-graph-depth 1 --min-starting-files 5 --max-starting-files 10 \
  --max-package-tokens 60000
```

`context_builder.py` не вызывает LLM и работает по pinned base/candidate commits. Package содержит
точный diff, 5–10 стартовых файлов с причинами, bounded symbol/dependency graph, связанные тесты,
краткие карточки ADR/precedent, SHA-256 каждого включённого файла, byte size и консервативную token
estimate. Побайтно идентичные файлы (например, зеркало `docs/agents/*` ↔
`harness/project/docs-agents/*`) остаются стартовыми файлами, но их содержимое учитывается в оценке
один раз; причина второй копии называет оригинал и требует держать копии идентичными. Markdown-файл,
чья оценка не меньше `context_package_policy.section_index_min_tokens` (по умолчанию 20000), входит
в пакет как оглавление: `sections` перечисляет заголовки уровней 1–3 (вне fenced code) с
`start_line`/`end_line`, оценка учитывает только это оглавление, а роль читает лишь нужные ей
диапазоны строк. Файл без заголовков учитывается целиком. Для Python AST извлекает
сигнатуры прямых локальных зависимостей; текущий Discovery-контракт ограничивает разворачивание
одним уровнем. Неподдержанный формат получает первые 30 строк как deterministic fallback. При
превышении token limit сборка завершается ошибкой, а не молча обрезает пакет. Legacy
`--max-package-size-bytes` можно задать как дополнительную диагностику, но он не заменяет token limit.

Запись package immutable, versioned и hash-проверяема. Она shared внутри batch: один и тот же
base/candidate переиспользует один package ID между architect, developer и continuation sessions;
новый package создаётся только после нового candidate. Coordinator проверяет freshness до handoff и
не создаёт brief со stale package. Brief передаёт compact summary (starting files, related tests,
precedents и pinned commits), а полный diff остаётся в package один раз. Package не заменяет immutable brief и не отменяет обязательные
self-report, heartbeat, review или QA.

## 3. Выбрать роли и спланировать batch

В базовом наборе есть четыре write-роли и три read-only роли.

| Роль | Режим | Когда назначать |
| --- | --- | --- |
| `developer` | write | Обычное backend-изменение внутри service или bounded context. |
| `database-migrations` | write | Schema/data migration и её rollout/rollback. |
| `messaging-integration` | write | Outbox, routing, message schema, retry или DLQ. |
| `conflict-resolver` | write | Текстовый конфликт ветки тикета с сдвинувшимся integration SHA; назначается только маршрутом `integration resolve`. |
| `architect` | read-only | Труднообратимое граничное решение. |
| `qa` | read-only | Нужна независимая проверка через project-facing interface. |
| `code-review` | read-only | Обязателен для listed high-risk triggers; выдаёт отдельные Standards и Spec reports. |

Одна задача может пройти несколько ролей, но handoff внутри одного batch всегда последовательный и
в нём бывает только один active writer. Batch с пересекающимися файлами могут идти параллельно, каждый в своих issue-ветке и worktree,
пока хватает `concurrency_budget`; пересечения разбираются при интеграции, а не блокируют запуск.
Два batch на один и тот же незавершённый тикет, ветку или worktree отклоняются. Тяжёлые integration/quality checks идут в одной
serialized quality-gate lane.

Для API/public contract, schema/data migration, outbox/queues, transactions,
authorization/security и concurrency/retry completion невозможен, пока coordinator не получил оба
независимых отчёта `code-review`: Standards и Spec.

После developer dispatch candidate commit получает детерминированную оценку рисков из DoD, changed
files и developer-reported triggers. Оценка решает, когда composite read-only `code-review`
**обязателен**, но не когда он *разрешён*: review можно создать для любого кандидата, и конвейер
`/implement` делает это всегда. Оси Standards и Spec остаются отдельными evidence. Только после
принятого review (если он обязателен) создаётся отдельный QA dispatch: обязательный полный QA gate
в clean-room нельзя заменить локальной проверкой developer-а. Любой новый candidate commit после
finding или failed QA снова проходит оценку риска.

## 4. Coordinator CLI и lifecycle

Runtime-neutral режим не имеет команды «запустить всех». Coordinator CLI ведёт записи по
`planned → awaiting-approval ↔ active → completed | blocked | failed`. При `manual_all` каждый
report оставляет dispatch в `reported` до решения человека. При `low_risk` чистый завершённый
report batch, чей scope целиком лежит в `low_risk_paths`, принимается автоматически с записью решения в ledger. Blockers, failed
checks, раскрытые risks, risk triggers и findings любой оси review сохраняют ручной gate; publish тоже требует
отдельного approval. При `milestone` чистый отчёт обычной роли также принимается автоматически,
но QA, publish и рискованные переходы остаются ручными вехами. При `auto` координатор сам принимает
чистые отчёты любого batch, включая чистый QA, и готовит следующий dispatch без `low_risk_paths`;
решение записывается как `policy:auto`. Ручными остаются publish, открытие PR и merge, batch с
совпавшими risk triggers, findings review, упавшие проверки, blockers и раскрытые risks.

1. Создать planned batch и затем отдельно утвердить его:

   ```bash
   python .harness/orchestration/coordinator.py --repo . batch create \
     --ticket '#123' --branch feature/issue-123-payment-validation \
     --worktree issue-123-payment-validation \
     --allowed-path 'services/payments/**' \
     --definition-of-done 'Добавить валидацию платежа' \
     --prohibited-change 'Не менять migration или публичный API' \
     --required-gate review --required-gate qa
   python .harness/orchestration/coordinator.py --repo . batch approve \
     --batch <batch-id> --approved-by 'имя утверждающего' \
     --approved-at 2026-09-09T12:00:00Z
   ```

   `batch create` сначала выполняет `git fetch origin <ref>` — `--integration-ref`, если он передан,
   иначе `base_branch` проекта (для epic-less задач) — и фиксирует полученную вершину как
   `base_commit`/`integration_base_commit`; необновлённый локальный HEAD никогда не используется как
   замена. Полный маршрут `/implement` требует явного указания `--required-gate review --required-gate qa`
   (независимые Standards/Spec review и full clean-room QA), и координатор сверяет их наличие в
   `required_gates` до отправки write-role (`developer`) worker-а. В допустимых прямых CLI-сценариях
   `--required-gate` может быть опущен (по умолчанию `["none"]`). Дальше review, QA и publish проверяют
   закреплённый candidate, даже если `origin/<ref>` ушёл вперёд: обязательной проверки свежести базы и
   принудительного developer-перезапуска нет, другие batch это не останавливает. Финальное обновление базы
   выполняет `integration refresh` при подготовке PR (раздел «Integration accounting после publish»).
2. Сверить активные batch, `concurrency_budget`, writer и quality-gate lane. Пересечение файлов
   другого batch не повод откладывать запуск; занятая serialized quality-gate lane не мешает
   параллельной реализации. Если batch упёрся в бюджет, дождитесь завершения активного batch или
   поднимите `concurrency_budget`.
3. Создать и отдельно утвердить architect dispatch, принять его отчёт, и только потом — developer
   dispatch. Порядок жёсткий: `dispatch create --role developer` отклоняется, пока для того же batch
   нет architect-отчёта, принятого через `batch decide --decision accept`. Правило живёт в
   `coordinator.py`, поэтому действует и для ручного CLI, и для `/implement`. CLI сохраняет immutable
   brief до передачи:

   ```bash
   # сначала dry run: показывает канонический переход и его digest, brief не пишет
   python .harness/orchestration/coordinator.py --repo . dispatch propose \
     --batch <batch-id> --role developer --runtime codex
   # approval действует только для показанного digest
   python .harness/orchestration/coordinator.py --repo . dispatch create \
    --batch <batch-id> --role developer --runtime codex --approved-by 'имя утверждающего' \
     --approved-at 2026-09-09T12:01:00Z --transition-digest <transition_digest>
   python .harness/orchestration/coordinator.py --repo . dispatch send \
     --dispatch <dispatch-id> --adapter <project-runtime-adapter>
   ```

   Для роли с `transport: "in-process"` adapter не передаётся вовсе: `dispatch send --dispatch <id>`
   возвращает путь к brief, после чего coordinator **немедленно** запускает роль как субагента текущей
   сессии — никаких чтений предыдущих dispatch/report/template между этими действиями. Без
   `.harness/orchestration.json` к `dispatch create` добавляются `--model` и `--effort` вызывающей
   сессии; для coordinator и architect выбирайте `medium`, если разработчик явно не одобрил иное.

   Accept отчёта architect может закрепить предложенный им commit plan вместо плана по умолчанию:

   ```bash
   python .harness/orchestration/coordinator.py --repo . batch decide \
     --batch <batch-id> --decision accept --approved-by 'имя утверждающего' \
     --approved-at 2026-09-09T12:00:30Z --commit-plan-file .harness/.sandboxes/scratch/commit-plan.json
   ```

   Файл лежит в репозитории или одном из его worktree и содержит ровно один ключ
   `{"commit_plan": [...]}`. Каждая entry содержит ровно четыре поля: уникальный `id`
   (`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`), непустой `summary`, непустой список `expected_paths`
   (относительные пути или glob без ведущего `/` и сегмента `..`) и `covers` — номера пунктов DoD
   `1..n`, которые реализует entry. Порядок entries — порядок коммитов. Accept отклоняется с
   remedy, если какой-то пункт DoD не покрыт ни одной entry, entry называет несуществующий пункт
   или флаг передан не с `--decision accept` на отчёте architect; отчёт тогда остаётся ожидающим
   решения, а batch не меняется. Проверенный план сохраняется в batch как `commit_plan`, решение
   architect получает `commit_plan_sha256`, и этот план становится `commit_plan` каждого developer
   brief batch-а, включая `developer-retry`. Если план в batch не совпадает с digest принятого
   решения, developer brief не создаётся. Без файла brief содержит по одной entry `step-N` на
   пункт DoD с `covers: [N]`. При `low_risk` и `milestone` чистый architect report принимается
   автоматически с планом по умолчанию, поэтому architect, предлагающий другой план, указывает
   это в `risks`, и report ждёт ручного решения.

   `dispatch send --role code-review` — единственный случай, когда нужен ещё один обязательный флаг:
   `--checkout <путь>`, указывающий на worktree, реально зачекаученный на `candidate_commit` dispatch-а
   (см. clean-room QA lane ниже — та же изоляция нужна и для review). Все остальные роли `--checkout` не
   передают. Без него `dispatch send` отклоняется с точным текстом ожидаемого флага.
4. Принять один schema-validated completion report с evidence. Он сохраняется как canonical JSON
   и детерминированная Markdown-проекция, после чего dispatch остаётся `reported`, а batch ждёт
   следующего решения:

   ```bash
   python .harness/orchestration/coordinator.py --repo . report submit \
     --file developer-report.json
   ```
   До следующего dispatch coordinator записывает отдельное решение. При `manual_all` или нечистом
   report человек принимает report, override-ит warning либо требует retry. При `low_risk` и чистом
   report coordinator сам записывает `accept` с rationale
   `Auto-accepted due to low_risk policy and clean report`, вычисляет `next_action` и готовит
   следующий допустимый dispatch. При `milestone` чистый отчёт вне вехи также получает `accept`;
   после developer оценивается риск, а QA-dispatch ждёт отдельного approval. Чистый QA-report при
   `milestone` ждёт решения человека.

   Policy-цепочка после записи report (`policy-decide`, `risk-assess`, `next-dispatch`) идёт
   отдельными командами уже после записи report, и каждая может остановиться: ledger занят,
   поднят `needs_attention`, изменилось состояние batch. Тогда `report submit` всё равно завершается
   с кодом 0: report записан, ответ называет его (`report`, `report_sha256`) и несёт объект
   `completion` — `route: "report-completion"`, `failed_step`, состояние каждого шага (`done`,
   `already-done`, `not-applicable`, `failed`, `not-run`), `error`, `remedy` и точную команду
   `command`, которую выполняет сам coordinator (`run_by: "coordinator"`), а не worker:

   ```bash
   python .harness/orchestration/coordinator.py --repo . report complete --dispatch <dispatch-id>
   ```

   `report complete` идемпотентна: каждый шаг выводится из ledger, поэтому уже записанное решение,
   risk assessment или следующий dispatch не повторяются, а повторный запуск ничего не пишет. Она
   повторяет только policy, записанную при `report submit` (`auto_accept_policy` в статусе
   dispatch), никогда не записывает report заново и не создаёт dispatch для роли, сдавшей report.
   Шаг `risk-assess` оценивает candidate report-а: у developer — его `commit_sha` и `changed_files`,
   у read-only verification — candidate, закреплённый в её dispatch, с файлами из diff от base batch,
   как их считает `risk assess`.
   Шаг, которому нужен человек, останавливается с remedy этого шага. Для report, оставленного
   человеку, все шаги — `not-applicable`. Повторно отправлять report нельзя: он уже записан.

   Ручное решение выглядит так:

   ```bash
   python .harness/orchestration/coordinator.py --repo . batch decide \
     --batch <batch-id> --decision accept --approved-by 'имя утверждающего' \
     --approved-at 2026-09-09T12:02:00Z
   ```

   Developer report против brief с `commit_plan` несёт `commit_map` — пары
   `{commit_sha, plan_entry_id}` для каждого коммита после `snapshot_commit` (у rebase — после
   `rebase_target`). В initial и rebase отчёте это отношение: коммит, закрывающий несколько entries,
   даёт по паре на каждую, entry, закрытая несколькими коммитами, — по паре на каждый коммит.
   Отображение не one-to-one (объединённый коммит, разделённая или незакрытая entry) — расхождение,
   и тогда отчёт обязан нести `dod_coverage` — ровно по записи на каждый пункт DoD:
   `{"dod_item": <n>, "commits": [<sha>, ...]}` из коммитов этого dispatch или
   `{"dod_item": <n>, "not_covered": "<причина>"}` — и непустой `divergence_justification`: что
   объединено, разделено или добавлено и почему. При one-to-one `dod_coverage` необязателен
   (coordinator выводит покрытие из `covers` плана), а `divergence_justification` отклоняется.
   `report submit` и `batch decide` отклоняют структурные ошибки, каждую с remedy:
   неотображённый созданный коммит, коммит не из этого dispatch, неизвестная entry, повтор пары,
   расхождение без `dod_coverage` или без обоснования, покрытие без пункта, с чужим пунктом или
   чужим коммитом, `not_covered` без причины. В `developer-retry` и в отчётах других ролей эти два
   поля отклоняются.

   `report submit` также сверяет `dod_coverage` с `commit_map` и `covers` плана: пункт DoD,
   заявленный покрытым набором коммитов, отклоняется, если `commit_map` не сопоставляет ни один из
   них с entry, у которой этот пункт есть в `covers`. Remedy называет пункт, заявленные коммиты и
   entries плана, покрывающие этот пункт. Обратное направление допустимо: `not_covered` с причиной
   для пункта, который сопоставление формально покрывает, валиден и, как любой `not_covered`,
   требует ручного решения. One-to-one отчёт без `dod_coverage` получает покрытие из `covers` и
   этой сверки не требует.

   Обоснованное расхождение с полным покрытием само по себе не делает отчёт нечистым: при
   `low_risk` и `milestone` чистый в остальном отчёт принимается автоматически, а запись решения
   получает `commit_plan_divergence` (`developer_dispatch_id`, `justification`, `merged_commits`,
   `split_entries`, `unclosed_entries`); ту же запись получает и ручное принятие. Любой пункт `not_covered` делает
   отчёт нечистым при любой policy: auto-accept не срабатывает, `--decision accept` отклоняется,
   принять отчёт можно только `--decision override-warning` с `--note`, отличным от `none`
   (решение получает `dod_not_covered` с причинами), либо вернуть его через `retry`.
   `batch decision-packet` показывает `dod_coverage`, `dod_coverage_source` (`report` или
   `derived`) и `commit_plan_divergence`. Brief code-review несёт `commit_plan_divergence`
   последнего принятого initial или rebase developer report (у остальных ролей поле `null`), чтобы
   reviewer проверил, что границы коммитов остались reviewable.

   Дефект, который coordinator нашёл в чистом developer report, не тратит developer-retry до review
   (маршрут `carry-over`). Retry developer report без accept — исключение только для невыполненного
   пункта DoD или изменения вне scope; в остальных случаях report принимается с находками:

   ```bash
   python .harness/orchestration/coordinator.py --repo . batch decide \
     --batch <batch-id> --decision accept --approved-by 'имя утверждающего' \
     --approved-at 2026-09-09T12:02:00Z --findings-file .harness/.sandboxes/scratch/findings.json
   ```

   Файл лежит в репозитории или одном из его worktree и содержит ровно один ключ
   `{"findings": [...]}`. Каждая находка содержит ровно три поля: непустой `summary`, `files` —
   непустой список уникальных путей от корня репозитория в POSIX-форме (без ведущего `/`, обратной
   косой черты и сегмента `..`) — и непустой `expected_evidence`: чем review подтвердит закрытие.
   Текст находки передаётся роли, поэтому он на английском. Флаг допустим только с `accept` или
   `override-warning` на completed developer work report; иначе решение отклоняется с remedy, и
   batch не меняется. Каждая находка записывается в batch append-only как запись `carried_items`:
   `item_id` `coordinator-finding-<n>` (порядковый номер в batch), `source` (`kind`, `dispatch_id` и
   `report_sha256` принятого report, `candidate_commit`), `summary`, `files`, `expected_evidence`,
   `attached_at`, `attached_by` и `record_sha256`, который сверяется при каждом чтении batch.

   После policy auto-accept, который файл находок не принимает, находки добавляет `batch carry-over`:

   ```bash
   python .harness/orchestration/coordinator.py --repo . batch carry-over \
     --batch <batch-id> --findings-file .harness/.sandboxes/scratch/findings.json
   ```

   Команда не создаёт dispatch и не меняет candidate, поэтому человек её не утверждает: она
   записывает решение coordinator `carry-over` с `approved_by: policy:carry-over` и прикрепляет
   находки к последнему принятому developer work report; batch с `next_action: qa` переходит в
   `code-review`. Команда отклоняется с remedy, пока какой-либо report ждёт решения, если в batch нет
   принятого developer work report и если после него уже создан dispatch, не отменённый и не
   переведённый `batch resume` в `abandoned`: для неотправленного code-review (или qa, созданного
   low_risk-цепочкой) remedy — `dispatch cancel` и повтор `batch carry-over`, для отправленного —
   сообщить дефект при решении его report, для уже решённого report — приложить находку через
   `batch decide --findings-file` при accept следующего developer report.

   Пока находка открыта, risk assessment ведёт candidate в `code-review`, даже если ни один триггер
   не совпал (`review_required` записи оценки не меняется). Находка закрыта, когда принят (`accept`
   или `override-warning`) code-review, чей brief её нёс; retry review оставляет её открытой.

   Каждый brief несёт секцию `carried_items` — общий канал переносимых пунктов: объект, ключ —
   вид источника (`coordinator-finding`, `review-finding`), значение — список
   `{item_id, source, summary, files, expected_evidence}`; пустой канал — `{}`. Brief code-review и
   developer work несёт все открытые `coordinator-finding`. Developer brief, отвечающий на `retry`
   code-review с маршрутом `developer-retry`, несёт ещё находки осей Standards и Spec этого review
   как `review-finding` (`item_id` `review-finding-<n>`; `source` — `dispatch_id`, `report_sha256`,
   `axis`, `severity`; `files: []`; `expected_evidence` — evidence находки). Поэтому единственный
   developer-retry закрывает обе группы и тратит `retry_policy.max_developer_retries` один раз;
   accept с находками и `batch carry-over` бюджет не тратят. Непустая секция входит в transition как
   `carried_items_sha256`, так что находка, добавленная после `dispatch propose`, требует нового
   approval.

   Отчёт code-review отчитывается по каждому пункту brief в необязательном `review.carried_items`:
   `[{"item_id": ..., "status": "closed" | "open" | "unverified", "evidence": ...}]`. `report submit`
   отклоняет пункт, которого brief не нёс, повтор пункта, неизвестный статус и пустое evidence.
   Пропуск пункта структурно допустим, но вместе с `unverified` и `open` это carried gap: отчёт не
   clean, policy его автоматически не принимает, `--decision accept` отклоняется, а
   `override-warning` требует `--note`, отличный от `none`, и записывает в решение
   `carried_items_gap`; отчёт можно и вернуть через `retry`. Правила blocker и warning сохраняют
   приоритет. Пункт `open` — структурное evidence категории `code`, поэтому такой retry ведёт в
   `developer-retry`. `batch decision-packet` показывает `carried_items` (каждый пункт с `source`,
   `summary`, `status` — у отчёта code-review `omitted` для пропущенного пункта — и `evidence`) и
   `carried_items_gap`.

Минимальный ручной brief хранит ticket и dispatch ID, роль и её access, выбранный profile/model/effort,
allowed paths (scope записи), issue-ветку/worktree, DoD, запреты, команды, dependencies, approval. Для
write-роли completion report обязан включать commit SHA, exact changed files, результаты всех checks,
risks, blockers и следующее решение coordinator-а. Если write-роль остановилась до изменений (например,
из-за risk trigger при отсутствии необходимого gate), принимается честный отчёт с `outcome: blocked`,
пустым `changed_files` и `commit_sha`, привязанным к проверенному checkout (`snapshot_commit`). Отчёт
регистрируется и возвращает `decision_packet` с `recovery_route` (`retry`, `block`, `abandon`) без
регистрации незавершённого кандидата. Для read-only роли вместо SHA указывается
`not applicable — read-only role`.

Новые факты не меняют отправленный brief. Coordinator добавляет отдельное решение с evidence; если
изменились scope, DoD, assignment или proof, текущий dispatch заканчивается и создаётся новый.
Повтор после `blocked` или `failed` — тоже новый dispatch с новым ID и brief.

### Маршрутизация `retry` и решение `abandon`

`batch decide --decision retry` больше не означает «снова developer». Coordinator сохраняет на
решении routing record: `route`, `previous_role`, `reason_category`, `next_role`, `next_action`,
`rationale` и `candidate_commit` (пока он не изменился). Причина определяется только по
структурированным данным report: outcome, findings, severity осей Standards/Spec, failed checks и
тому, изменился ли candidate. Свободный текст `blockers`/`output` не классифицируется. Явную причину
можно передать через `--reason-category` (`code`, `requirements`, `candidate-change`,
`verification-infrastructure`, `transport`, `context-pressure`, `tooling`, `block-bypass`,
`unknown`), но она не отменяет найденный finding. Rate limit, недоступный Bash/WSL wrapper и
transport failure — это operational evidence (`verification-infrastructure` или `transport`), а не
code finding. Context limit — `context-pressure` только если для отчитавшегося dispatch записано
`critical`-наблюдение `context_pressure` (см. ниже); голое утверждение даёт `unknown`.

| Стадия отчёта | `accept` | `retry` | `block` / `fail` | `abandon` |
| --- | --- | --- | --- | --- |
| architect | developer | новый architect | terminal | `abandoned` |
| developer | risk assessment | `developer-retry` продолжает непринятый candidate этого report (`snapshot_commit` — его `commit_sha`); retry создаёт новый candidate, и тот получает новую risk assessment | terminal | `abandoned` |
| code-review | qa | новый code-review на том же `candidate_commit`, если report `blocked`, причина — `verification-infrastructure`/`transport`/`context-pressure`, findings пусты, обе оси без findings, нет failed check и candidate не менялся; иначе `developer-retry` | terminal | `abandoned` |
| qa | publish | новый qa на том же SHA при том же условии (QA остаётся read-only); defect или новый candidate — `developer-retry` | terminal | `abandoned` |
| publish | `completed` | новый publish на том же принятом SHA при `verification-infrastructure`/`transport`/`context-pressure`; `developer-retry`, если candidate должен измениться | terminal | `abandoned` |

`code`, `requirements`, `candidate-change` и `unknown` всегда ведут в `developer-retry`; только три
operational-категории могут повторить read-only стадию на том же SHA — и лишь при пустых findings,
неизменном candidate и отсутствии scope/requirement blocker. Противоречивая или неподтверждённая
причина всегда даёт безопасный маршрут `developer-retry`. Повтор на том же SHA — это новый immutable dispatch: новый dispatch ID, повторная
проверка свежести Context Package и собственное явное approval при `manual_all`.
Прежние brief, report и blocker остаются audit evidence. Фиктивные и пустые commit не
используются; новый candidate всегда требует новой risk assessment; `block` и `fail` сами retry не
запускают. `--retry-role developer` принудительно выбирает developer retry там, где coordinator
иначе повторил бы ту же роль на том же SHA.

`tooling` — отдельная операционная категория: hook, классификатор безопасности или ledger
заблокировал законное действие роли. Coordinator присваивает её только по структурному полю
`tooling_blocker` (`tool`, точная `command`, `message`) в report с `outcome: blocked`, если её не
перекрывают finding, failed check, сдвинутый candidate или developer-категория. Явная
`--reason-category tooling` без этого поля даёт `unknown`, а другая названная операционная категория
идёт своим прежним маршрутом. Маршрут `tooling` на любой стадии — `tooling-retry`: новый dispatch той
же стадии на том же SHA (architect, verification, code-review, qa или publish), а для developer —
`developer-retry`, который продолжает его последний коммит; этот коммит записывается в
`candidate_commit` routing record. В таблице выше «три operational-категории» по-прежнему означают
`verification-infrastructure`, `transport` и `context-pressure`.

`block-bypass` — read-only роль (code-review, qa или verification) обошла блокировку hook-а или
инструмента вместо остановки с `tooling_blocker`. Эту категорию называет только approver, и report с
нарушением не является evidence: его findings, failed checks и outcome не влияют на маршрут, и лишь
сдвинутый candidate по-прежнему ведёт в `developer-retry`. Маршрут — `bypass-rerun`: новый dispatch
той же стадии на том же SHA (verification — на её зарегистрированном candidate) без нового candidate
commit. `batch decide` требует `--note` с описанием нарушения; report не принимается и не
закрывается через override-warning, а новый dispatch всегда требует явного approval
(`--approved-by`) при любой `approval_policy`. `bypass-rerun` не расходует
`retry_policy.max_developer_retries`. Для architect, developer и publish `block-bypass` отклоняется:
такой report по-прежнему получает `retry` с developer-категорией (`code`, `requirements`,
`candidate-change`) или `block`.

Retry непринятого developer report продолжает его историю. Пока batch ждёт этот `developer-retry`,
coordinator берёт candidate из immutable report (`commit_sha`, сверенный по hash) и пинит на него
`snapshot_commit` нового developer dispatch. Worktree не откатывается ни к base, ни к более старому
принятому candidate, а `commit_map` retry считает только коммиты поверх `snapshot_commit`: каждый
новый коммит закрывает ровно одну distinct entry плана, а `dod_coverage` и
`divergence_justification` в retry-отчёте отклоняются. Путь по
умолчанию — `dispatch propose`/`create` без `--candidate-commit`: brief получает
`candidate_commit: null` и не требует risk assessment, как developer retry после code-review. Чтобы
привязать этот SHA к transition digest, передайте `--candidate-commit <commit_sha>`; тогда до
`dispatch propose`/`create`, пока batch в `awaiting-approval`, нужна `risk assess` для того же SHA.
Такая оценка — только evidence: `next_action` остаётся `developer-retry`, `required_next_role` и
требование нового candidate сохраняются, code-review и QA не открываются, а `risk assess` для
другого SHA отклоняется. Retry-report с тем же SHA не принимается. Retry после code-review, qa или
publish по-прежнему пинит `snapshot_commit` на последний принятый developer candidate.

Code-review `blocker` никогда не принимается. Пока `retry_policy.max_developer_retries` ещё допускает
developer retry, для него доступны `retry` или `abandon`; после исчерпания budget `retry`
отклоняется, а blocker закрывается через `block`, `fail` или `abandon`, после чего работа
разбивается или перепланируется в новом batch. `tooling-retry` этот budget не расходует и при его
исчерпании не отклоняется: каждый такой retry по-прежнему решает человек, а серию ограничивает
attention `tooling-retry-repeated`. `--retry-role developer` на read-only стадии по-прежнему даёт
`developer-retry` с расходом budget.

Решение `abandon` доступно после любого completion report. Оно требует явного approval и непустого
`--reason`, переводит batch в терминальный `abandoned` и помечает незакрытые dispatch как
`abandoned`:

```bash
python .harness/orchestration/coordinator.py --repo . batch decide \
  --batch <batch-id> --decision abandon --approved-by 'имя утверждающего' \
  --approved-at 2026-09-20T09:00:00Z --reason 'план заменён, начинаем новый batch'
```

`abandon` не удаляет worktree, candidate, brief, report, Context Package и audit evidence, не
закрывает issue и не создаёт PR. Убирается только то, что не является evidence: staged-копии report в
agent inbox и записи QA-очереди dispatch, которые уже не запустятся (живой QA lease по-прежнему
снимается только `qa clear-stale-lease`). Batch записывает `abandoned.last_accepted` — последний принятый
этап и его candidate, — от которого можно создать свежий batch на той же ветке и том же candidate.
`abandon` никогда не является автоматическим fallback для `block`, `fail` или `retry`. Команда
`batch abandon` для batch, у которого не будет ни одного report, остаётся прежней и завершает его
в `failed`.

#### Поле `route`: routing record, decision packet и audit

Каждое решение `retry` и `abandon` в `batch decide` записывает выбранный маршрут восстановления в
`routing.route` — одно значение закрытого набора `RECOVERY_ROUTES`: `developer-retry`,
`same-candidate-rerun` (новый code-review, qa или publish на том же SHA), `verification`,
`architect-retry` и `abandon`. Шестое значение, `report-completion`, `batch decide` не записывает:
его называет `completion` у `report submit`, когда policy-цепочка после записанного report
остановилась (см. шаг 4 выше и «Занятый ledger» ниже). Седьмое, `carry-over`, записывают `accept`
или `override-warning` с `--findings-file` и `batch carry-over` (см. шаг 4): routing record с
`previous_role: developer`, `next_role` и `next_action` `code-review`, `candidate_commit`,
`carried_item_ids` и `rationale`, без `reason_category` и `decided_at`. К `next_action` он не
применяется: тот идёт через risk assessment, как при любом accept developer. Восьмое,
`tooling-retry`, записывает `retry` с категорией `tooling` (см. выше). Девятое, `bypass-rerun`,
записывает `retry` с категорией `block-bypass` (см. выше). Маршрут ставится там же, где
`next_action`, по тем же структурированным данным и никогда по свободному тексту. У `abandon`
routing record той же формы, но `reason_category`, `next_role`, `next_action` и `candidate_commit`
равны `null`, а `rationale` содержит только структурные факты (`--reason` остаётся в `note`).
Нормативная таблица «ситуация → маршрут → кто утверждает → evidence» — раздел «Recovery route table»
в `.harness/orchestration/playbook.md`.

`batch decision-packet` показывает маршрут до записи решения: поле `route_preview` содержит
`retry` — routing record, вычисленный так же, как в `batch decide` (без `decided_at`), и `abandon` —
`{"route": "abandon"}`. Packet принимает те же `--reason-category` и `--retry-role developer`, что и
`batch decide`, и ничего не пишет; для уже решённого report и для пакета следующего dispatch
`route_preview` равен `null`:

```bash
python .harness/orchestration/coordinator.py --repo . batch decision-packet \
  --batch <batch-id> --reason-category verification-infrastructure
```

Preview не проверяет `retry_policy.max_developer_retries`: при исчерпанном бюджете он по-прежнему
показывает маршрут `developer-retry`, а `batch decide --decision retry` такое решение отклоняет.
Если маршрут retry вычислить нельзя (например, упало настроенное расширение retry reason
classifier), packet всё равно строится, а `route_preview.retry` равен
`{"route": null, "refused": ..., "remedy": ...}` с той ошибкой, которой откажет
`batch decide --decision retry`. С `--findings-file <path>` packet добавляет
`route_preview["carry-over"]` — запись, которую сделает `batch decide --findings-file` на ожидающем
developer report, а без такого report — `batch carry-over`, либо отказ той же формы
`{"route": null, "refused": ..., "remedy": ...}`; без флага `route_preview` не меняется.

Каждое решение `batch decide` хранит в transition audit record batch деталь `decision`:
`dispatch_id`, `decision`, `route` (`carry-over` у `accept` и `override-warning` с
`--findings-file`, `null` у решения без маршрута — остальных `accept`, `override-warning`,
`block`, `fail`), `evidence` (`dispatch_id`, путь `report` и `report_sha256` immutable report),
`approver` и `approved_at`. `approver` — `{"kind": "policy", "name": "low_risk" | "milestone" | "auto"}` для
policy auto-accept или `{"kind": "human", "name": <--approved-by>}` для явного решения; решение
`batch carry-over` пишет ту же деталь с `route: "carry-over"` и
`{"kind": "policy", "name": "carry-over"}`; вид
определяется путём, которым решение утверждено, а не строкой имени. Деталь входит в ту же audit-запись
и ту же контрольную сумму, что и переход batch. Маршрут вне `RECOVERY_ROUTES` отклоняется с remedy
при записи и при чтении batch.

### Approval, привязанный к digest перехода

`dispatch propose` принимает те же аргументы, что и `dispatch create`, но brief не пишет: он
регистрирует shared Context Package, который brief закрепит, и возвращает канонический переход и его
`transition_digest` — SHA-256 от batch ID, ID и роли предыдущего dispatch, reason category,
следующей роли/действия и purpose, candidate SHA, base SHA, review scope, verification commands,
Context Package ID и required gates. `dispatch create` с явным approval обязан получить этот digest в
`--transition-digest`: coordinator пересчитывает переход из ledger и отклоняет любое расхождение, так что
изменение scope, candidate, роли, verification command, reason category или Context Package требует
нового `propose` и нового approval. Digest сохраняется в approval и в immutable brief вместе с самим
переходом; ledger-валидация пересчитывает его. Policy approval (`milestone`, `low_risk`) выводится из
создаваемого перехода и привязан к его собственному digest. При `human_approval_gate: "tty"` digest
показывается в запросе подтверждения.

Если Repo Map у закрепляемого пакета имеет tier не `full`, результат `dispatch propose` дополнительно
содержит `context_package_quality_warning`: tier, причину деградации и parser provenance. Это
предупреждение для утверждающего человека, а не блокировка dispatch; для `full` поле отсутствует.

Без `repo_map_policy.min_tier`/`min_tier_by_role` деградация Repo Map никогда не блокирует dispatch —
только описанное выше необязывающее предупреждение. Если в `repo_map_policy` присутствует `min_tier`
(общий минимум для репозитория) и/или `min_tier_by_role` (переопределение по роли, ключи ровно
`architect`/`developer`/`code-review`), порядок разрешения — override роли, затем `min_tier`, затем
отсутствие гейта. Если для роли задан минимум и фактический tier закреплённого Context Package хуже
требуемого (порядок уровней по ADR 0008: `minimal` < `full`), и `dispatch propose`, и `dispatch create`
отклоняются с причиной, называющей роль, фактический и требуемый tier, ещё до какого-либо approval —
эта проверка выполняется в общем пути перед веткой `propose`/`create`, поэтому готовый
`--transition-digest` её не обходит. Роль, не перечисленная в `min_tier_by_role`, при отсутствии
общего `min_tier` сохраняет поведение по умолчанию (без блокировки).

Просроченное (`approval_ttl_seconds`) или отклонённое в терминале approval — fail-closed: coordinator
не повторяет вызов сам и не подставляет более старое approval.

### Idempotency read-only retry

Brief ролей `architect`, `code-review`, `qa` и publish хранит
`retry_idempotency_key = sha256(role + candidate SHA + base SHA + review scope + reason category +
digest verification commands)`. Пока в открытом batch есть активный (не решённый, не cancelled, не
abandoned) dispatch с тем же ключом, новый dispatch отклоняется. Завершённый retry не мешает новому
dispatch на том же candidate — он получает новый immutable ID; смена candidate всегда меняет ключ;
повторный review/QA никогда не правит прежние report и brief.

### Context pressure

`dispatch context-pressure --dispatch <id> --observed-tokens <N> --source probe|provider-usage|
runtime-adapter` пишет наблюдение `observed_tokens`, `context_limit`, `warning_threshold`, `level`
(`ok`/`warning`/`critical`) и `recorded_at`. Без `--observed-tokens` число берёт настроенный
`context_telemetry_provider`. Источник — только provider/runtime: self-report модели отклоняется.
Лимит и доля предупреждения — значения, зафиксированные в brief. Запись — чистое наблюдение: она не
меняет `next_action`, не создаёт retry и не снимает approval. При `critical` она сообщает обязанность
worker-а: write-роль создаёт checkpoint на ближайшей зелёной границе TDD либо возвращает structured
blocker, read-only роль возвращает blocker. Continuation создаётся только из checkpoint и только с
новым model self-report. Категория `context-pressure` у retry требует такой `critical`-записи для
отчитавшегося dispatch.

### Attention state

`needs_attention` — флаг batch, а не lifecycle-состояние: он не меняет `state`, `next_action`,
candidate и evidence, но запрещает создание следующего dispatch. Поля: `needs_attention`,
`attention_reason`, `attention_since`, `last_safe_action`, `recommended_human_action`. Причины:

| `attention_reason` | Когда |
| --- | --- |
| `unknown-reason` | retry с причиной `unknown` |
| `infrastructure-retry-repeated` | operational retry одного candidate больше `max_infrastructure_retries` |
| `tooling-retry-repeated` | третий подряд `tooling-retry` на одном candidate; снимается после исправления инструмента |
| `retry-queued-too-long` | принятый retry ждёт dispatch дольше `retry_queue_seconds` |
| `stale-evidence` | закреплённый в незавершённом dispatch Context Package расходится с base или текущим developer candidate (тем же, что выбирает `snapshot_commit`) |
| `stale-dispatch` | живой dispatch молчит дольше `stale_dispatch_seconds` |

Флаг ставят `batch decide --decision retry`, `dispatch wait` (событие `stale`) и
`batch attention check --batch <id>`; последняя команда просто оценивает batch сейчас. Снимает его
только человек: `batch attention resolve --batch <id> --note '…' --approved-by … --approved-at …`
подтверждает открытые findings (то же событие повторно не поднимается) и пишет событие в
`attention_events`. Если настроен `human_notifier`, он вызывается при постановке флага; сбой адаптера
записывается как `failed` и флаг не отменяет.

### Совместимость и миграция

Все новые поля batch и четыре поля brief (`transition`, `transition_digest`, `retry_idempotency_key`,
`orchestration_policy`; либо все, либо ни одного) необязательны: записи, созданные раньше, остаются
валидными, версия ledger не меняется и `ledger migrate` не нужен. Новые записи проходят ту же
целостностную проверку (`context_pressure` с hash, форма attention-полей, согласованность brief).
Изменилось поведение CLI: `dispatch create` с `--approved-by` теперь требует `--transition-digest`.
Brief без поля `commit_plan_divergence` и batch без `commit_plan` тоже остаются валидными; entry
плана без `covers` покрывает пункт DoD по своей позиции. Brief без `carried_items`, transition без
`carried_items_sha256`, batch без `carried_items` и отчёт code-review без `review.carried_items`
тоже валидны: пустой канал ничего не добавляет в transition, поэтому прежние digest не меняются.

Поле `route` в routing record и деталь `decision` в transition audit record batch тоже
необязательны и введены без смены версии ledger (остаётся 3). Решение, записанное до них, читается
как есть и остаётся валидным: `ledger migrate` не добавляет и не выводит для него route, route
пишет только новое решение `batch decide`. Записанный `route` вне закрытого набора маршрутов
отклоняется при чтении batch.

### Инвентарь и закрытие тупикового batch

Посмотреть, что вообще заведено и что не закрыто:

```bash
python .harness/orchestration/coordinator.py --repo . batch list --open
python .harness/orchestration/coordinator.py --repo . batch list --ticket '#123'
```

Обычный путь к терминальному состоянию — `batch decide`. Но он требует ровно один отчёт, ожидающий
решения, а отчёт требует живой dispatch с подтверждённой моделью. Воркер, умерший до self-report, не
отчитается никогда — и такой batch не закрыть ни `fail`, ни `block`. Для этого случая есть отдельная
команда:

```bash
python .harness/orchestration/coordinator.py --repo . batch abandon \
  --batch <batch-id> --approved-by 'имя утверждающего' --approved-at 2026-09-11T06:00:00Z \
  --reason 'воркер умер до model self-report, решение недостижимо'
```

Она требует явного approval и непустой причины, переводит batch в `failed`, помечает все незакрытые
dispatch как `abandoned` и записывает решение рядом с остальными. **Она ничего не удаляет**: immutable
brief, отчёты и QA-артефакты остаются на месте. Повторно применить её к уже терминальному batch
нельзя.

Если старт dispatch завершился блокировкой инфраструктуры или watchdog отметил отправленный dispatch
как `stale`, продолжайте тот же batch после устранения причины:

```bash
python .harness/orchestration/coordinator.py --repo . batch resume \
  --batch <batch-id> --reason 'причина устранена'
```

Команда сохраняет принятые отчёты и `next_action` в ledger, помечает только сорванный dispatch как
`abandoned` и переводит batch в `awaiting-approval`. Следующий `dispatch create` использует ту же
принятую архитектуру и создаёт новый brief для прерванной роли. Для решения `block` или закрытого
через `batch abandon` batch этот путь недоступен.

Batch закреплён за рантаймом, под которым его спланировали. `batch create` записывает хэш всего
пакета рантайма в `.harness/`: верхнеуровневых модулей и всех подпакетов (`orchestration/`,
`gate_runner/`, `context_builder/` и других). Кроме того, он сохраняет неизменяемый снимок этого пакета
в `.harness/orchestration/state/runtimes/`. Если после этого рантайм переустановили, например
`harness update` с ветки другой задачи, команды этого batch (`--batch`, `--dispatch` или
`dispatch_id` в файле `--file` у `report submit`, `dispatch checkpoint` и `dispatch telemetry`)
автоматически выполняются кодом снимка, и описания ролей тоже берутся из снимка. Новые batch
получают новый рантайм. Так задачи идут параллельно и не блокируют друг друга. Изменённый вручную
снимок отклоняется. У batch, созданного до появления снимков, хэш покрывает только
`orchestration/`, а снимка нет. Его восстанавливают из пакета той ревизии, на которой batch
спланирован: подойдёт `.harness` из временного worktree с выполненным `harness update` или каталог
`harness/` из `git archive <ревизия> harness`:

```bash
python .harness/orchestration/coordinator.py --repo . batch restore-runtime \
  --batch <batch-id> --from <путь к пакету закреплённой ревизии>
```

Команда принимает каталог, только если его хэш совпадает с закреплённым. Снимок и новый рантайм
работают с одним ledger. Поэтому `ledger migrate` на новую версию схемы отказывается выполняться,
пока есть незавершённые batch, закреплённые за другим рантаймом. Сначала их доводят до конца или
отменяют на их собственном рантайме.

Если pinned snapshot уже удовлетворяет всем пунктам DoD, не создавайте фиктивный commit ради
write-role отчёта и не используйте `abandon`. Зафиксируйте отдельное терминальное решение:

```bash
python .harness/orchestration/coordinator.py --repo . batch not-required \
  --batch <batch-id> --approved-by 'имя утверждающего' --approved-at 2026-09-11T06:00:00Z \
  --reason 'pinned snapshot already satisfies every definition-of-done item'
```

Команда оставляет audit evidence, отменяет незакрытые dispatch, переводит batch в `not-required` и
возвращает рекомендацию закрыть связанный issue с меткой `resolution::wontfix`. Обычный write-role
report по-прежнему обязан содержать реальный commit и exact changed files.

Если ошибка найдена **до** передачи brief runtime-у, не abandon batch. Отмените только этот
неотправленный dispatch: immutable brief останется в audit trail, а batch вернётся в
`awaiting-approval` и сможет получить исправленный dispatch.

```bash
python .harness/orchestration/coordinator.py --repo . dispatch cancel \
  --dispatch <dispatch-id> --approved-by 'имя утверждающего' \
  --approved-at 2026-09-11T06:00:00Z --reason 'исправить назначение до запуска worker'
```

Править файлы в `.harness/orchestration/state/` руками не следует ни при каких обстоятельствах: эти
записи и есть доказательство, ради которого существует весь маршрут. Если штатной команды для вашего
случая нет — это дефект инструмента, а не повод открыть редактор.

### Model self-report и dispatch watchdog

Отправленный dispatch не считается живым сам по себе. Первым действием после получения brief роль
подтверждает фактически активную модель:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch self-report \
  --dispatch <dispatch-id> --model <фактическая модель> \
  --worktree "$(git rev-parse --show-toplevel)"
```

Совпадение с `resolved_model` immutable brief переводит dispatch в `working`. Если включён
`worker_attestation_required`, coordinator также проверяет переданный Git top-level, issue branch
write-роли либо pinned SHA review-роли; расхождение немедленно переводит dispatch в `blocked`,
помечает batch `blocked` и завершает команду ошибкой; после этого
`report submit` для такого dispatch не принимается. Починка — новый dispatch с новым brief, а не
правка отправленного. Completion report вообще не принимается без успешного self-report, поэтому
подменённая или неверно настроенная модель видна сразу, а не после потраченного окна.

Для architect/developer `worker_attestation_required` также требует, чтобы Git-worktree HEAD в момент
`self-report` буквально совпадал с immutable `snapshot_commit` из brief. Для architect, developer,
verification и code-review `dispatch create` выбирает `snapshot_commit` в таком порядке:

1. явный `--candidate-commit`;
2. `commit_sha` непринятого developer report, пока batch ждёт его `developer-retry`;
3. последний принятый developer candidate — например, `developer-retry` после code-review blocker,
   qa или publish (повтор на том же SHA developer dispatch не создаёт);
4. иначе `base_commit`.

`dispatch preflight` и проверка свежести Context Package используют то же правило. Retry продолжает
историю этого candidate и добавляет отдельные логические коммиты по immutable commit plan;
coordinator не выполняет и не предлагает `git reset --soft`. Если HEAD worktree не совпадает с pinned
snapshot, исправляйте конфигурацию нового dispatch или выбирайте worktree на этом commit, не
переписывая существующую историю.

Для `developer-retry` `dispatch preflight` дополнительно возвращает `retry_start`: handoff, с
которого стартует новая developer-сессия, её стартовые файлы, `context_estimate` (порог, оценка до и
после компакта) и `warning`. Оценку и warning повторяет `decision_packet` как
`retry_context_estimate` и `retry_context_warning`. Состав handoff, порог smart zone и правила
компакта — раздел "Developer-retry handoff" в `playbook.md`.

`dispatch create` до записи brief выполняет над ним ту же проверку, что `dispatch send`. Brief,
который send отклонил бы, не создаётся: в ledger не появляются ни brief, ни его status. Пример —
`--candidate-commit` без связанной immutable risk assessment. Remedy называет
`risk assess --batch <batch-id> --candidate-commit <sha> --changed-file <path>...`; после неё нужны
новые `dispatch propose` и `create` (неотправленный brief сначала отменяется через
`dispatch cancel`). Проверка при `dispatch send` остаётся на месте.

Пока роль работает, она отбивает heartbeat, а coordinator-сессия опрашивает состояние. По умолчанию
dispatch допускает до часа тишины для долгой сборки или теста, но immutable brief требует heartbeat
сразу после self-report и затем не реже раза в пять минут. Это сохраняет быстрый сигнал о живом
worker, не объявляя работающего developer stale из-за одного долгого tool call:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch heartbeat --dispatch <dispatch-id>
python .harness/orchestration/coordinator.py --repo . dispatch status \
  --batch <batch-id> --stale-after 3600
```

`dispatch status` показывает для каждого dispatch роль, транспорт, `resolved_model`, результат
self-report, время последнего heartbeat, `silent_seconds` и признак `stale`. Это обобщение
QA-lease-expiry на любой dispatch, а не только на clean-room QA lane. Stale — блокер, который
coordinator выносит человеку: сам он состояние по таймауту не меняет.

`dispatch heartbeat` принимает необязательную пару `--context-tokens <N> --context-source probe` —
координатор-измеренное число токенов из live-пробы контекста (см. «Отчётность и мониторинг токенов»
ниже), а не self-report роли: это сохраняет правило из начала документа («Токены — только
наблюдаемая provider- или runtime-telemetry», строки 27-29) — сама роль это значение не поставляет.
Значение попадает в открытое поле `extra` статуса dispatch-а, схема ledger не меняется, и новое
событие в `dispatch wait` не вводится.

`dispatch status` дополнительно отдаёт для каждого dispatch последнюю запись `telemetry`
(`dispatch telemetry`, см. ниже) и `context_advisory` — чистое чтение: отсутствие телеметрии не
считается ошибкой, `telemetry` и `context_advisory.observed` в этом случае — `null`
(`context_advisory.level` при этом остаётся `"ok"`).

### Занятый ledger, `report complete` и `ledger release-lock`

Каждая команда coordinator-а берёт эксклюзивный lock ledger-а (`.coordinator.lock` в каталоге
state). Lock записывает владельца — `pid`, `host` и `acquired_at`. Держит lock тот, кто первым
эксклюзивно создал эту запись: процесс, чей ещё пустой каталог lock успели снять и занять снова,
получает отказ и в чужой lock не пишет. Пока lock держит другая операция, команда записи
завершается ошибкой `ledger is locked by another operation`; её remedy предлагает повторить
команду и называет `ledger release-lock`, а не ручное удаление.

Опрос coordinator-а занятый lock переживает:

- `dispatch wait` считает занятый lock пропущенным опросом и опрашивает дальше до своего
  `--timeout`: report возвращается, как только lock освобождён, а если lock занят до конца ожидания,
  результат — обычный `{"dispatch_id": ..., "event": "timeout"}`;
- `dispatch status` при занятом lock завершается с кодом 0 и отвечает структурно:
  `{"ledger_busy": true, "retry_after_seconds": ..., "lock": {...}, "remedy": ...}`, где `lock`
  содержит владельца и `held_seconds`. Это не недоступность coordinator-а: повторите
  `dispatch status` через `retry_after_seconds`.

Терпимость касается только этих двух команд опроса и `report complete`: ни одна запись не идёт без
lock, а занятый lock у остальных команд остаётся ошибкой. `report complete` при занятом lock
останавливается на шаге, который не смог прочитать состояние, ничего не пишет и в remedy называет
себя для повтора.

Lock, который остаётся занятым, снимается только командой с проверкой владельца:

```bash
python .harness/orchestration/coordinator.py --repo . ledger release-lock
```

Вердикт зависит от владельца:

| Владелец lock | Вердикт |
| --- | --- |
| Процесс владельца жив (на этом host) | отказ `owner-alive` — при любом возрасте lock |
| Владелец на другом host | отказ `owner-on-another-host`: запустите команду на том host |
| Процесс владельца завершился (на этом host) | снимается, `owner-dead` |
| Нет читаемой записи владельца, lock младше `LEDGER_LOCK_STALE_SECONDS` (3600 с) | отказ `owner-unknown-recent`; remedy называет, через сколько секунд lock можно снять |
| Нет читаемой записи владельца, lock старше `LEDGER_LOCK_STALE_SECONDS` | снимается, `owner-unknown-stale` |

Нет читаемой записи владельца, если lock взят старым runtime и записи нет вовсе или если владелец
умер, создав `owner.json`, но не записав его: запись пустая или нечитаемая. Возраст нечитаемой
записи считается от mtime `owner.json`, а без записи — от mtime каталога lock. Пока lock моложе
порога, владелец может ещё записывать себя, поэтому команда отказывает. Владелец, который за
`LEDGER_LOCK_STALE_SECONDS` так и не записал созданную запись, считается завершившимся.

Успех — `{"released": true, "reason": ..., "lock": {...}}`; если lock нет —
`{"released": false, "lock": null}`; отказ — ошибка с причиной, владельцем и `held_seconds`, lock
не трогается. Объект `lock` описывает снятый lock:

| Поле | Значение |
| --- | --- |
| `path` | каталог lock |
| `owner` | запись владельца (`pid`, `host`, `acquired_at`) или `null`, если читаемой записи нет |
| `owner_record` | `readable` — запись прочитана; `unreadable` — `owner.json` есть, но пустой или нечитаемый; `absent` — записи нет |
| `acquired_at` | `acquired_at` из записи, иначе mtime `owner.json` для нечитаемой записи или каталога lock |
| `held_seconds` | сколько секунд lock держится к моменту проверки |

Команда снимает только тот lock, который проверила: если владелец сменился во время снятия, она
отказывает и просит повторить. Запуски `ledger release-lock` не пересекаются: их
сериализует файловая блокировка ОС (`.coordinator.lock.release` в каталоге state), которую система
снимает сама при выходе процесса, а второй параллельный запуск получает отказ
`another ledger release-lock is releasing the ledger lock` и повторяется после первого. Удалять lock
или другие файлы state вручную нельзя.

### Отчётность и мониторинг токенов: `dispatch telemetry`

```bash
python .harness/orchestration/coordinator.py --repo . dispatch telemetry --file telemetry.json
```

Записывает source-observed метрики (worker- или coordinator-сессии) в audit trail batch-а:
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `max_context_tokens`,
`tool_calls`, `tool_output_bytes`, `poll_turns`, `restart_reason`, `recorded_at` — недостающее
provider-поле остаётся `null`, а не оценочным нулём. Команда **intentionally data-only**: не может
изменить состояние роли, планирование, approvals или model routing — только пишет запись
телеметрии.

В **возвращаемом значении** (не в сохранённой ledger-записи) команда добавляет
`context_advisory: {"level": "ok"|"warn"|"over", "limit", "warn_at", "observed"}` — advisory-оценка
`max_context_tokens` против порога `adaptive_continuation_policy.context_limit` в проектной
`.harness/orchestration.json`, доля которого задаётся `adaptive_continuation_policy.context_warn_ratio`
(доля от `context_limit`, по умолчанию `0.8`; `warn_at = round(context_limit * context_warn_ratio)`).
`level` — `"ok"` пока `observed` (или его отсутствие) ниже `warn_at`, `"warn"` — в диапазоне
`[warn_at, context_limit)`, `"over"` — на `context_limit` и выше. Это чистая оценка: она не
триггерит checkpoint автоматически — решение о checkpoint остаётся за coordinator-ом, как и для
любого другого сигнала, кроме auto-resume по 429 (см. the continuation section above). Baseline из
`playbook.md` («Baseline metrics») тем же образом остаётся ориентиром, а не скрытым лимитом.

### Checkpoint и новая worker session

Write-роль (developer, database-migrations, messaging-integration, conflict-resolver) может растянуть один dispatch на
несколько worker session, если весь TDD-цикл в одну сессию раздувает её контекст. Read-only роль
(architect, qa, code-review) — не может: попытка checkpoint для неё отклоняется сразу.

Вместо completion report текущая worker session фиксирует неитоговый checkpoint — отдельную,
hashed ledger-запись, которую нельзя перепутать с отчётом:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch checkpoint \
  --file checkpoint.json
```

`checkpoint.json` обязан содержать ровно: `dispatch_id`, `commit_sha`, `changed_files`,
`remaining_definition_of_done` (подмножество DoD approved dispatch), `passing_checks` (в формате
`checks_run` completion report, команды — из approved `verification_commands`), `risks`, `blockers`
и `context_package_id` — ссылку на последний зарегистрированный для batch Context Package, либо
литеральный `not applicable — no context package registered`, если для batch его пока нет. Никаких
чужих полей: ни сырой истории чата, ни логов прежних неудачных попыток. `checkpoint` требует уже
подтверждённого self-report, переводит dispatch-status в `checkpointed` и не трогает outcome enum
(`completed`/`blocked`/`failed`) — этот enum остаётся только у completion report.

Новая worker session для того же dispatch ID стартует командой `dispatch resume`. Авторизация
зависит от того, почему закончилась прежняя сессия:

```bash
# runtime adapter сообщил rate-limit termination — авторизация автоматическая
python .harness/orchestration/coordinator.py --repo . dispatch resume --dispatch <dispatch-id> \
  --termination-reason rate_limit

# планируемый trigger (context limit / N TDD-циклов / большой failure log / законченный vertical
# slice) — требуется явное coordinator decision
python .harness/orchestration/coordinator.py --repo . dispatch resume --dispatch <dispatch-id> \
  --trigger context-limit --measured-value 162000 --file continuation-facts.json \
  --approved-by "project coordinator" --approved-at 2026-09-14T18:00:00Z
```

`--termination-reason`, распознанный как rate limit (`rate_limit`/`rate-limit`/`429`), авторизует
новую сессию автоматически — новое решение человека/coordinator-а не требуется. Любая другая
причина, включая отсутствующую или нераспознанную, трактуется как planned trigger — safe default
в сторону approval, а не от него:

- `--trigger` обязателен и должен быть одним из `context-limit`, `tdd-cycles`, `failure-log`,
  `vertical-slice`.
- для `context-limit`/`tdd-cycles`/`failure-log` `--measured-value` обязан быть не меньше
  соответствующего порога `adaptive_continuation_policy` (`context_limit`/
  `tdd_cycle_count`/`failure_log_bytes`) из `.harness/orchestration.json` — без явной конфигурации
  используются задокументированные значения по умолчанию (150000 / 3 / 20000), а не зашитые
  внутри порознь для каждого места.
- `--file` обязан содержать JSON с `dispatch_id`, `remaining_definition_of_done`, `risks` и
  `dependencies`, буквально совпадающими с последним checkpoint (первые два поля) и с dispatch
  (`dependencies`); `blockers` в сравнение не входит — их формулировка может измениться между
  сессиями без реального дрейфа scope/DoD/risks/dependencies. Расхождение — сигнал, что они реально
  изменились: coordinator обязан закрыть текущий dispatch и открыть новый через обычный approval,
  а не резюмировать этот.
- авторизация записывается тем же `coordinator_decisions`, что accept/retry/block/fail/abandon/
  cancel — новый тип записи не вводится.

`resume` принимает только `checkpointed` dispatch, возвращает его в `dispatched` и отбрасывает
предыдущий model self-report. Это значит, что новая сессия обязана заново пройти `dispatch
self-report` и `dispatch heartbeat` — ровно так же, как при первом contact, — прежде чем следующий
checkpoint или completion report будет принят. Круг замыкается тем же dispatch ID: checkpoint →
`dispatch resume` → новая self-report/heartbeat → в итоге один completion report.

### Clean-room QA lane

Одобренный `qa` dispatch выполняется самим coordinator в отдельном temporary Git worktree,
отсоединённом ровно на `candidate_commit`. Команды берутся буквально из
`verification_commands` immutable brief; несовпадение HEAD или грязный worktree останавливает
проверку. Запуск не передают runtime adapter:

```bash
python .harness/orchestration/coordinator.py --repo . qa run \
  --dispatch <qa-dispatch-id> --lease-seconds 1800
```

В репозитории существует одна FIFO-полоса тяжёлых проверок. Если она занята, команда сохраняет
запрос и возвращает `state: queued` с позицией; повторный вызов для того же dispatch запустит его
только когда он станет первым. Состояние и текущий owner видны без запуска gate:

```bash
python .harness/orchestration/coordinator.py --repo . qa status
```

Lease содержит dispatch ID, host, PID, время взятия и expiry. Истёкшая аренда **не** снимается
автоматически: coordinator сперва сверяет owner, затем записывает собственное решение с теми же
host/PID/expiry и только после этого удаляет stale request:

```bash
python .harness/orchestration/coordinator.py --repo . qa clear-stale-lease \
  --expected-host <host> --expected-pid <pid> --expected-expiry <ISO-8601> \
  --approved-by 'имя coordinator-а' --approved-at 2026-09-10T12:00:00Z \
  --reason 'проверено, что владелец больше не выполняется'
```

После выполнения создаётся immutable completion report с командами, exit codes и кратким
санитизированным evidence. Полный санитизированный stdout/stderr сохраняется вне Git в
`.harness/orchestration/state/qa-artifacts/<sha256>.log`; report ссылается на этот путь и checksum.
Провал gate остаётся QA finding и требует нового одобренного developer dispatch — runner не правит
код и не перезапускает проверку самостоятельно.

После accepted QA evidence coordinator создаёт, но не запускает, publish dispatch для того же
candidate SHA. Только developer publish отправляет этот SHA; ни QA, ни review, ни adapter не создают
и не мержат PR. После publish человек вручную запускает `/to-pull-requests <ticket>`: этот шаг
проверяет accepted QA evidence текущего SHA и ведёт обычный ручной PR workflow без повторного
тяжёлого gate.

Обычная точка входа — `/implement <ticket>`: эта сессия сама становится coordinator-ом и ведёт
описанный цикл, останавливаясь на пяти approval-гейтах (architect, developer, code-review, qa,
publish) и наблюдая за heartbeat каждого dispatch. Один тикет доводится до терминального состояния
batch до старта следующего. Ручной запуск по этому руководству остаётся полностью валидным — для
него готовый запрос управляющей сессии можно сформулировать так:

```text
Выступи coordinator-ом backend batch для issue #123. Прочитай .harness/orchestration/roles/
и .harness/orchestration/playbook.md. Не запускай роль до моего явного approval. Предложи
explicit allowed paths, последовательность ролей, immutable brief и требуемые risk gates;
не меняй protected или integration branch.
```

### Integration accounting после publish

Завершённый batch — история: его не переоткрывают и не переписывают. Связь, нужная следующему
шагу интеграции (тикет, issue-ветка, source batch, опубликованный candidate SHA и target SHA
integration ref), фиксирует отдельная immutable запись — Integration record. Её создаёт и читает
группа `integration`; ни одна из команд не пишет batch, plan, dispatch и reports, а Git меняет
только `refresh` (ниже):

```bash
python .harness/orchestration/coordinator.py --repo . integration prepare \
  --ticket '#123' --branch feature/issue-123-short-name
python .harness/orchestration/coordinator.py --repo . integration status \
  --ticket '#123' --branch feature/issue-123-short-name
python .harness/orchestration/coordinator.py --repo . integration link-evidence \
  --record <integration_record_id> --kind ci --result passed \
  --candidate-commit <sha> --target-commit <sha> --reference <run-url> [--artifact-sha256 <hex>]
```

`prepare` запускают после accepted publish и до merge или удаления ветки. Он выбирает единственный
завершённый batch с accepted publish для пары ticket+branch и сверяет опубликованный SHA двумя
независимыми способами: по принятому отчёту publish (digest отчёта и brief) и по `ls-remote`
(ветка на remote — ровно candidate, integration ref — ровно закреплённый target). Evidence
берётся из accepted green QA того же SHA. Ответ содержит `integration_record_id`, пару
`candidate_sha`/`target_sha` и ссылки на исходное QA и publish по digest. Повторный `prepare` не
создаёт вторую запись и не теряет первую (`created: false`). Отказ всегда содержит remedy:
не найден batch для тикета и ветки, `--batch` чужой пары, batch не завершён или не опубликован,
несколько опубликованных batch без `--batch` (подмены одного batch другим нет), `--candidate-commit`
не равен опубликованному SHA, remote-ветка отсутствует или другая, нет accepted green QA, integration
ref уже ушёл вперёд, а записи ещё нет, remote недоступен, история batch изменена после записи.

Запись лежит в `reports/integration/`, а evidence — в `reports/integration-evidence/` внутри
уже проверяемого каталога `reports`: версия схемы ledger и `ledger migrate` не меняются.

`status` — только наблюдение, под тем же read-only контрактом: он не создаёт dispatch, ничего не
записывает и при повторе даёт тот же ответ. Состояния: `current` (integration ref всё ещё на
записанном target), `stale` (ref ушёл вперёд; `refresh_required: true`) и `unavailable` (remote не
ответил; подтвердить пару нельзя). Исходное QA относится только к записанной паре:
`source_evidence.applies_to_current_pair` равен `false` при `stale`, и старое QA не принимается для
новой пары кандидат/target. `status` сам ничего не запускает; обновить пару при `stale` позволяет
`integration refresh` (ниже). После refresh `status` показывает текущую пару (`candidate_sha`,
`target_sha`, исходные значения в `original_candidate_sha`/`original_target_sha`), список `refreshes`
и блок `verification`: `required: true`, пока нет passed CI или local-QA именно этой пары
(`resolver` не считается), и всегда `re_review_required: false`.

`refresh` — маршрут подготовки PR вместо обязательного developer-перезапуска из-за сдвига базы:

```bash
python .harness/orchestration/coordinator.py --repo . integration refresh \
  --ticket '#123' --branch feature/issue-123-short-name
```

Он читает текущий SHA integration ref. Если он равен target пары, rebase не запускается
(`state: unchanged`, ничего не пишется). Иначе в worktree своего batch issue-ветка перебазируется
на этот точный SHA и публикуется через `--force-with-lease` с ожидаемым старым SHA: чужой коммит на
remote не теряется. Команда отказывает с remedy, если worktree не на issue-ветке на записанном
candidate, имеет незакоммиченные изменения или операцию в процессе, либо remote-ветка уже не равна
записанному candidate; stash, reset и обход не применяются. Protected и `integration/*` ветки целью
записи не бывают, чужие worktree не затрагиваются. Чистый rebase возвращает `state: rebased`,
`new_candidate_sha` и `verification_required: true`, не вызывает resolver и не тратит его два цикла.
Текстовый конфликт возвращает `state: conflict` и `resolver` (`conflicting_files`, `candidate_sha`,
`target_sha`, `worktree`, `cycles_spent: 0`); rebase отменяется, ветка и worktree остаются как были.
Rebase пишет immutable `IntegrationRefreshRecord` в `reports/integration-refresh/` (прежний и новый
candidate, target, коммиты до и после). Старое QA остаётся историческим evidence; новый candidate
подтверждают CI или local-QA пары через `link-evidence`, повторный review из-за refresh не нужен.
Конфликт и работу, которой нужен developer, ведёт маршрут rebase из ADR 0012; `refresh` от него не
зависит.

`resolve` — маршрут текстового конфликта (роль `conflict-resolver`, ADR 0015). Он ничего не пишет в
Git: чистый rebase отклоняется (это работа `refresh`), а конфликт превращается в новый batch вида
`resolver` рядом с завершённым batch тикета (его brief, отчёты и история не меняются):

```bash
python .harness/orchestration/coordinator.py --repo . integration resolve \
  --ticket '#123' --branch feature/issue-123-short-name
```

Ответ содержит `batch_id`, конфликтные файлы, candidate и target SHA, scope и остаток бюджета;
`next_action` batch — `resolve-conflict`. Дальше идёт обычный путь: `batch approve`, затем
`dispatch create --role conflict-resolver --purpose work` (сначала `--propose`). Brief несёт
неизменяемую секцию `resolver`: тикет, требования обеих сторон (`sides.candidate` — DoD исходного
batch; `sides.target` — plan-записи тикетов `(#N)` из subject коммитов цели, иначе subject и тело
коммита), SHA candidate и target, scope, запреты, план коммита, проверки, остаток бюджета и
`report_staging_path`. Роль открывает skill `resolving-merge-conflicts`, сохраняет требования обеих
сторон и не добавляет функциональность вне них.

Бюджет — два автоматических target SHA; третий требует решения человека. Цикл тратит только
зафиксированный отчёт resolver-а по новому target SHA; чистый rebase, ответ человека и правка на том
же target цикл не тратят, а правки на одном target ограничены `retry_policy.max_developer_retries`.
Бюджет выводится из append-only событий `reports/resolver-events/` (`cycle-spent`,
`same-target-fix`, `human-decision`, `scope-change`, `exhausted`), поэтому потеря сессии или resume
его не сбрасывают.

Несовместимые требования resolver не угадывает: он пишет checkpoint (`blockers` — конкретное описание
и варианты) и завершает сессию. Ответ человека фиксируется отдельным событием до resume:

```bash
python .harness/orchestration/coordinator.py --repo . integration resolver-event \
  --record <id> --kind human-decision --dispatch <dispatch-id> \
  --decided-by <имя> --option <вариант> --note '<решение>' [--extends-budget]
python .harness/orchestration/coordinator.py --repo . dispatch resume \
  --dispatch <dispatch-id> --trigger human-decision --file <facts.json> \
  --approved-by <имя> --approved-at <время>
```

Та же сессия продолжает тот же dispatch: новый developer не создаётся, соседние batch не
останавливаются. `--extends-budget` даёт ещё один автоматический target после двух потраченных.
Изменение scope — обычное approval нового dispatch (`--kind scope-change` лишь фиксирует его):
исходный brief не переписывается, а resume с изменившимися фактами отклоняется существующей
проверкой. Потерянная runtime-сессия возобновляется через существующие checkpoint/resume или
`batch resume`; счётчики берутся из событий.

После исчерпания бюджета (`exhausted`) останавливается только эта задача: ветка и evidence
сохраняются. Несовместимость интеграции продолжает тот же resolver после решения человека; собственный
дефект тикета (`resolver.cause: task-defect`) возвращается обычному developer-у.

Отчёт resolver-а несёт верхнеуровневый блок `resolver`: `preserved_requirements` (каждое требование
обеих сторон дословно из brief), `human_decisions` (id только тех событий human-decision, что отвечают на checkpoint этого dispatch; автономный `human-decision --extends-budget` без checkpoint фиксируется лишь событием ledger, в отчёт не попадает и, кроме ещё одного автоматического target-цикла, даёт ещё `max_developer_retries` попыток исправления того же target), `target_sha`,
`resolved_candidate_sha`, `cause`, `changed_files` и `commits` с записью плана для каждого коммита.
Принятая резолюция идёт узким маршрутом: повторный code-review пропускается, но QA и CI либо local-QA
новой пары candidate/target обязательны (`integration status` держит `verification.required`).

`collect-ci` собирает CI-доказательство для комбинированного результата PR (ADR 0016):

```bash
python .harness/orchestration/coordinator.py --repo . integration collect-ci \
  --ticket '#123' --branch feature/issue-123-short-name --pull-request 45
```

Команда работает только с трекером `github` и списком `ci_required_checks` из `.harness/project.json`.
CI принимается, если проверки шли на merge commit PR, родители которого — ровно текущие candidate и
target, а все обязательные проверки прошли; тогда пишется immutable запись с
`verification: collector-accepted`, а `integration status` показывает блок `qa_replacement` (источник,
репозиторий, PR, SHA пары, merge commit, id check run). Если integration ref ушёл вперёд,
`qa_replacement.applies` становится `false`, `re_refresh_required` — `true`: нужен новый refresh и
новый сбор. Запасной путь — полный локальный QA: он нужен при `fallback` с причиной
`unsupported_tracker`, `not_configured`, `unavailable`, `unknown_checkout`, `head_only`,
`stale_candidate`, `stale_target`, `missing_check`, `pending_check` или `inconclusive_check`;
при этом ничего не записывается, а недоступность CI не считается ни находкой в коде, ни успехом.
Завершённый `failure` на подтверждённой паре записывается как `collector-failed`. Вручную привязанный
через `link-evidence` CI остаётся `unverified` и проверку пары не закрывает. Исходные QA-отчёты CI
не заменяет.

`local-qa` — запасной путь, когда CI комбинированного результата отсутствует, недоступен или непригоден
(`--ci-condition absent|unavailable|unusable`, обязательные `--reason` и `--record`):

```bash
python .harness/orchestration/coordinator.py --repo . integration local-qa \
  --record <integration-record-id> --ci-condition absent --reason 'CI не настроен для ветки'
```

Команда закрепляет immutable запрос (пара candidate/target, причина, полный список
`verification_commands`) и запускает существующий gate runner в изолированном clean-room checkout
точного candidate; ветка задачи, terminal source batch и принятые отчёты не меняются, код не
чинится. Запуск идёт через общую FIFO-очередь QA (`qa status`): два тяжёлых QA не работают
одновременно, остальные batch продолжают реализацию. Результат пишется отдельной immutable записью
с командами, санитизированным артефактом и checksum, затем привязывается как evidence
`kind: local-qa` с `verification: verified` — только если пара и remote-ветка не сдвинулись.
Другой SHA или движение target прежним результатом не подтверждаются. Провал проверки — finding с
`state: failed`, он остаётся для маршрутизации PR-сессией. Недоступность инфраструктуры — отдельный
`state: unavailable` без findings: повтор только явным `--retry` и не более
`max_infrastructure_retries`, затем `state: exhausted`. Вручную привязанный `local-qa` остаётся
`unverified` и проверку пары не закрывает.

`link-evidence` — единственный публичный способ привязать к записи будущие результаты CI, local-QA
или resolver (`--kind ci|local-qa|resolver`). Каждая привязка — отдельная immutable запись со своей
парой `candidate_sha`/`target_sha` и `verification: unverified`: исходное evidence записи никогда не
получает новую пару, а проверка новой пары не подменяет старую. Повтор той же привязки идемпотентен
(`linked: false`). Сами CI, local-QA и resolver эта операция не запускает; в `status` каждая привязка
показывает, относится ли её пара к текущему tip (`pair_checks[].applies_to_current_pair`).

### Advisory tool call

`.harness/orchestration/advisory.py` — дешёвый non-role CLI для чисто утилитарных подзадач:
ранжирование файлов по keyword, сводка лога и грубая риск-подсказка. Он выполняется вне
brief/report/self-report/heartbeat контракта: не является dispatch, не пишет ledger-запись и не
импортирует `ledger/`/`contract.py`/`coordinator.py`. Вывод эфемерен — печатается в stdout и
пересчитывается заново при каждом вызове, нигде не сохраняется как ground truth для другого
dispatch:

```bash
python .harness/orchestration/advisory.py rank-files --keyword payments -- services/payments/handler.py README.md
python .harness/orchestration/advisory.py summarize-log --file qa-output.log
python .harness/orchestration/advisory.py classify-risk --text "data migration for payments" --known-trigger data-migration
```

Его вывод — не авторизация. Coordinator/contract validation path не принимает advisory-вывод как
основание создать dispatch, понизить риск, принять QA или изменить scope: единственный авторитетный
источник риска остаётся `coordinator.py risk assess`.

## 5. Необязательный внешний adapter

Для `transport: "external"` проект передаёт `--adapter <path>` в `dispatch send`. Это transport-only
граница: coordinator
передаёт этому исполняемому файлу одобренный immutable brief через `--repo` и `--brief`.
Adapter отвечает за запуск worker в выбранной среде. Он не выбирает scope, не принимает report,
не планирует следующий dispatch и не мержит PR. После запуска worker возвращает completion report
по общим правилам раздела 4. Проверки доступа и среды запуска принадлежат проектному adapter.

## 6. Первый pilot и источник правил

Не задавайте лимиты токенов или «правильный» уровень параллелизма на глаз. В первом периоде pilot
записывайте по каждому закрытому ticket agent starts, tokens per batch, quality-gate wall time и
post-integration defects, включая источник и отсутствующие данные. Форма и единые правила подсчёта
лежат в `.harness/orchestration/pilot.md`.

Полный нормативный источник — `.harness/orchestration/playbook.md`; role-specific границы — в
`.harness/orchestration/roles/`. При противоречии между удобством конкретного runtime и этим
контрактом приоритет у manifest'а, immutable brief и явного approval.

Архитектурный контракт маршрута целиком зафиксирован в
[ADR 0003](https://github.com/PVMalove/claude-agent-harness/blob/master/docs/adr/0003-orchestration-core.md): opt-in capability, отдельное
approval для dispatch, review и QA для одного SHA, локальное санитизированное evidence и adapter
только для транспорта. Превращение `/implement` в coordinator-driven конвейер по умолчанию, model
self-report, dispatch watchdog, per-role transport и zero-config дефолты зафиксированы в
[ADR 0005](https://github.com/PVMalove/claude-agent-harness/blob/master/docs/adr/0005-implement-pipeline.md).
