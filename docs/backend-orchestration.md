# Руководство по backend-оркестрации

`backend-orchestration` — opt-in capability для согласованной backend-разработки несколькими
ролями. Она расширяет `pvmalove-suite`, но это не автономный scheduler. Coordinator (человек или
назначенная им управляющая сессия) планирует batch и ведёт переходы по настроенной политике
approval. Роли не расширяют свой scope, не выбирают модель и не мержат pull request.

Место capability в системе, её роли, clean-room QA и локальное state-хранилище описывают
[архитектура](./ARCHITECTURE.md) и [ADR 0003](./adr/0003-orchestration-core.md). Этот документ
содержит подробную процедуру настройки и запуска.

Используйте её, когда у задачи есть независимые backend-границы или обязательная независимая
проверка. Для обычной одной задачи достаточно стандартного pipeline `pvmalove-suite`.

## Владение правилами

`/implement` — короткий контракт coordinator-а, а не вторая копия процедуры. Он сохраняет порядок
handoff `architect → developer → code-review → qa → publish`, approval по политике, model self-report
и watchdog.

Полные правила принадлежат устанавливаемым модулям: `playbook.md` — lifecycle, authority,
immutable brief, evidence, параллелизм и метрики; `roles/` — границы и доказательство каждой роли;
`coordinator.py` — проверяемые переходы и audit; проектный adapter — только transport. При
противоречии приоритет имеют эти module-owned guidance и immutable records, а не runtime adapter
или краткий skill.

Токены — только наблюдаемая provider- или runtime-telemetry с источником и missing-data note.
Role self-report, completion report и оценка coordinator-а — это не token telemetry. Они не могут
заполнять отсутствующее значение.

## Что устанавливается

При выборе capability CLI копирует в проект:

- `.harness/orchestration/roles/` — переносимые manifest'ы ролей и общий контракт;
- `.harness/orchestration/playbook.md` — полный lifecycle, handoff и правила параллелизма;
- `.harness/orchestration/pilot.md` — форма наблюдения за первыми batch;
- `.harness/orchestration/coordinator.py` — runtime-neutral CLI для batch, approval, dispatch и report;
  сам файл — только фасад: разбор аргументов, роутинг и вывод JSON. Lifecycle лежит рядом в
  `core/` (константы, конфигурация, git, workspace), `ledger/` (persistence) и `workflow/`
  (по модулю на стадию batch'а: планирование, бриф, доставка, решение, отчёт);
- `.harness/orchestration.json` — project-owned конфигурация назначений, потолка записи и проверок.

Coordinator state, immutable briefs/reports и санитизированные QA-артефакты создаются локально в
`.harness/orchestration/state/`. Содержимое этого каталога gitignored, это не исходный код
проекта. Оно остаётся локальным evidence, пока coordinator явно не решит, что очистка безопасна.
Роль и adapter не удаляют историю batch.

Внутренний протокол имеет фиксированные языки. Agent-to-agent handoff, checkpoint, state evidence и
свободный текст в `.harness/orchestration/state/` пишутся на английском. Completion report для
coordinator-а пишется на русском и содержит `"report_language": "ru"`. Команды, пути, SHA,
имена тестов и цитаты исходных требований не переводятся. Это уменьшает двусмысленность между
разными runtime и сохраняет отчёт читаемым для человека.

Перед первым английским handoff coordinator и workers обязаны прочитать общий контракт
[Technical English](../harness/docs/technical-english.md). Он также доступен через playbook и `roles/_common.md`.
Источник сам описывает свою область, языковые исключения и примеры review. Действующие правила
языка, authority, scope, привязки evidence к candidate SHA и human approval сохраняются.

Manifest определяет режим роли (`write` или `read-only`), capability и risk triggers. Проектный
конфиг выбирает agent/fallback на уровне provider profile. `model` и `effort` он выбирает отдельно
для каждой роли в её assignment plan, вместе с потолком записи, бюджетом параллелизма и командами
проверки. Конфиг не может ослабить границы manifest'а. Значения секретов не хранятся ни в конфиге,
ни в brief, ни в report.

## 1. Включение

Для нового git-репозитория выберите только `backend-orchestration`. CLI разрешит зависимость от
`pvmalove-suite` автоматически.

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
обновляет только managed snapshot и сохраняет seed-документы, `--force-seed-files` перезаписывает
только seed, а `--force` объединяет оба действия. После любого включения или изменения конфигурации
выполните:

```bash
python3 harness/bin/harness.py health /path/to/repository
```

## 2. Настройка `.harness/orchestration.json`

Конфиг **не обязателен**. Без него coordinator работает на дефолтах: потолок записи ролей — весь
репозиторий (границу даёт `--allowed-path` batch), `verification_commands` берутся из
`qa_gate_commands` в `.harness/project.json`, `concurrency_budget` равен 1, а `model`/`effort` роли
приходят из вызывающей сессии (`dispatch create --model <model> --effort <effort>`). Транспорт в
этом режиме всегда `in-process`; provider profile нет, поэтому внешний worker запускать нечем.
`harness health` принимает такой проект. Конфиг нужен, когда проекту требуется более узкий потолок
записи, разные модели по ролям, внешний транспорт или бюджет параллелизма больше единицы. Справочник
всех полей с дефолтами — [`.harness/orchestration/README.md`](../harness/orchestration/README.md).

По умолчанию запускайте coordinator-сессию с `medium` effort, и для architect в assignment plan
тоже выбирайте `medium`. Более высокий effort — не дефолт каждого ticket: он требует явного решения
разработчика для названного труднообратимого вопроса.

`init` создаёт конфиг как копию управляемого примера `.harness/orchestration.example.json`. Пример
содержит provider profiles `claude-profile` и `codex-profile`, назначения всех восьми ролей на двух
runtime (`default_runtime: claude`, `transport: in-process`), потолок записи на весь репозиторий,
`concurrency_budget: 5` и `approval_policy: auto` с `human_approval_gate: trusted` и
`worker_attestation_required: true`. Модели и effort в примере — ориентир, замените их на свои.
Списки проверок в примере пустые, потому что команды зависят от стека. Впишите реальные project
checks в `verification_commands`, `developer_verification_commands`, `review_verification_commands`
и, чтобы работали подготовка QA и ограниченный инфраструктурный повтор, в `qa_preparation`,
`qa_environment_probes` и `qa_project_file_checks`. Если вам не нужен полностью автоматический
путь, выберите другую `approval_policy`. Чтобы выбрать другой runtime для роли, передайте
`--runtime` в `dispatch create` или смените `default_runtime`. CLI обновляет пример
при каждом `update`/`adopt`, но никогда не обновляет ваш `.harness/orchestration.json`. Новые поля и
значения переносите из примера вручную. Правьте provider profiles, `write_paths` ролей, назначение
для **каждой** используемой роли и project checks под свой проект. Всегда назначайте `code-review`.
Validator требует его, когда в конфиге есть назначения: это обязательный gate для high-risk работы.

Ниже приведён минимальный полный пример; имена model и effort принадлежат конкретному проекту.
Fallback задаётся в provider profile. Assignment plan каждой роли содержит именованные
runtime-наборы (`codex`, `claude` и т.п.), в каждом из которых обязательны `profiles`, `model` и
`effort`. Выбранный runtime фиксируется в immutable brief и не меняется при failover profile.

Если у роли несколько runtime, задайте `default_runtime` в её assignment plan, иначе каждый
`dispatch create` обязан явно передать `--runtime`: скрытого fallback на Codex нет. Например, если
проект всегда хочет запускать review через Claude, рядом с `runtimes` указывается
`"default_runtime": "claude"`. Пример также включает `"worker_attestation_required": true`: до любой
работы worker подтверждает свой фактический Git worktree, branch и SHA. Legacy projects могут
включать это поле постепенно. `approval_policy: auto` требует `worker_attestation_required: true` и
`human_approval_gate: trusted`; проверка конфигурации и `harness health` отклоняют другое сочетание.

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

`transport` — необязательное поле assignment plan, которое выбирают для каждой роли отдельно.
`in-process` (по умолчанию) исполняет роль как субагента текущей coordinator-сессии в worktree того
же batch. Оба варианта получают один и тот же immutable brief, обязаны пройти model self-report и
вернуть completion report по общим правилам, поэтому логика coordinator-а от транспорта не зависит.
`harness health` проверяет допустимость значения. Для внешнего worker явно укажите
`"transport": "external"` и передайте проектный adapter.

Для `in-process` `dispatch send` только фиксирует handoff, а следующим действием coordinator
немедленно запускает субагента по уже immutable brief — до любого поиска старых report/template или
конфигурации. Architect собирает только targeted evidence для решения: его brief не содержит команд
проверки, а отчёт содержит пустой `checks_run`. Полный набор `verification_commands` выполняет
clean-room QA, а developer получает `developer_verification_commands`. Это необязательное поле; без
него сохраняется совместимый режим, и developer получает полный список. Задавайте в нём быстрые
task-scoped проверки, а в `verification_commands` — независимый полный gate. Если
`developer_verification_commands` не задан, `harness health` предупреждает, что developer будет
гонять полный gate на каждой итерации. Необязательный `review_verification_commands` управляет
только code-review, а без него review получает полный список. Code-review запускает каждую
полученную команду через `test_summary.py`; в report остаются исходная команда и bounded summary, а
санитизированный полный лог доступен только для упавшей проверки.

Граница записи — не подсказка. Write-роль изменяет только пути, явно закреплённые за batch
(`--allowed-path`, они попадают в `write_paths` brief), и только внутри потолка роли, который задаёт
`write_paths` роли в assignment plan (по умолчанию — весь репозиторий). Coordinator не создаёт batch
шире потолка и проверяет brief и completion report по scope batch. Model должен быть CLI-алиасом
или ID без пробелов (например, `sonnet`), а не отображаемым названием.

У каждого batch свои issue-ветка и worktree, а число
одновременно активных batch ограничивает `concurrency_budget`. Увеличивайте его только после
явного решения coordinator-а. Сначала прогоните
`harness health`: он проверит JSON, существование profile, совместимость capability, fallback и режим
`code-review`.

### Бюджет контекста и preflight

Новый batch не создаётся, пока `batch preflight` не подтвердит ограниченный scope. `batch create`
вызывает его автоматически. Укажите ожидаемые changed paths, один bounded context/service и
консервативный размер diff. Значения выше project policy сначала разделите через `/to-tickets`, а
не передавайте в architect как discovery-задачу:

```bash
python .harness/orchestration/coordinator.py --repo . batch preflight \
  --ticket '#123' --allowed-path 'services/payments/**' \
  --definition-of-done 'Добавить валидацию платежа' \
  --expected-file services/payments/validation.py \
  --expected-service payments --expected-changed-lines 120
```

Без `preflight_policy` coordinator ограничивает DoD (5), dependencies (3), файлы (12), сервисы (1),
diff (800 строк) и ожидаемый context (80k tokens). Пример `orchestration.example.json` задаёт
8/5/25/2/2000/150k. Проект настраивает эти значения через `preflight_policy`.
`context_package_policy` использует консервативную token estimate и резервирует место для системных
инструкций. Байтовый предел остаётся только для диагностической совместимости. `symbol_graph_depth`
(по умолчанию 2) в этой политике управляет глубиной import-графа для *каждого* автоматического
Context Package. Это касается и пакетов, которые coordinator строит сам при `dispatch create` для
architect/developer/code-review, а не только ручного `context-package register`.
`--max-package-tokens` на `context-package register` — только более строгий потолок для одного
пакета. Значение выше сконфигурированного `context_package_policy.max_tokens` coordinator отклоняет
с ошибкой, а не применяет молча. Лимит поднимает только правка `context_package_policy.max_tokens`
в конфиге проекта, осознанно и с прохождением `harness health`. `max_related_tests` (по умолчанию 25)
— отдельный fail-loud предохранитель. Если import-граф стартовых файлов задевает больше
related_tests, чем этот предел, сборка пакета завершается `ContextPackageError`. Она не утаскивает
молча в оценку токенов половину test suite. `--max-related-tests` переопределяет предел для
одного ручного `context-package register`. Все role sessions на том же base/candidate
переиспользуют один immutable shared Context Package. Новый пакет строится только при новом
candidate. `continuation_policy` (2 continuations, из них максимум один automatic 429 resume) и
`retry_policy` (один developer retry) делают циклы конечными. `harness health` проверяет все поля.
Effort допускает только документированные уровни (`none`…`ultra`).

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
  отклоняет автоматический Context Package, если его `estimated_tokens` больше этого бюджета. Это
  верно, даже если оценка укладывается в `context_package_policy.max_tokens`. Потолок пакета может
  быть выше бюджета роли, но роль должна получить пакет, который помещается в её контекст.

Проект переопределяет набор необязательным `tool_policy`. Запись роли важнее записи режима, а запись
режима важнее встроенного дефолта:

```json
"tool_policy": {
  "modes": {"read-only": ["Read", "Grep", "Glob"]},
  "roles": {"qa": ["Read", "Grep", "Glob", "Bash"]}
}
```

`harness health` принимает только ключи `modes` (`read-only`/`write`) и `roles` (имена из role
manifests), а значением — непустой список уникальных строк. Brief без этих двух полей (созданный до
их появления) остаётся валидным. Brief с одним из двух полей или с некорректной формой значений
отклоняется. Brief — неизменяемая запись выбора на момент approval. Поэтому поздняя правка
`tool_policy` или `context_limit` не делает уже созданный dispatch невалидным. Новые значения
попадут только в следующие brief.

### Политика операционных циклов: attention, approval TTL и extensions

Три необязательных раздела `.harness/orchestration.json` управляют тем, как coordinator останавливает
зацикленные retry и как проверяет approval. Пропущенное поле берёт дефолт, а resolved-значения
coordinator записывает в каждый brief как `orchestration_policy`, поэтому правка файла посреди
dispatch не меняет условия, под которыми dispatch был утверждён:

```json
"attention_policy": {"retry_queue_seconds": 3600, "max_infrastructure_retries": 2, "stale_dispatch_seconds": 3600},
"approval_ttl_seconds": 3600,
"extensions": {"transport_health": "none", "verification_environment_health": "none",
               "retry_reason_classifier": "none", "context_telemetry_provider": "none", "human_notifier": "none",
               "runtime_access": "none"}
```

- `attention_policy` — пороги флага `needs_attention` (см. ниже): сколько принятый retry может ждать
  своего dispatch, сколько operational retry (`verification-infrastructure`, `transport`,
  `context-pressure`) допустимо на один candidate (`0` — ни одного) и после какого молчания живой
  dispatch считается stale.
- `approval_ttl_seconds` — срок жизни явного `--approved-at`. Coordinator отклоняет более старое
  (или датированное в будущем) approval и не использует его повторно. Без поля approval не истекает.
  Пример `orchestration.example.json` задаёт `14400`; из него `harness init` создаёт конфиг.
- `extensions` — подключаемые интерфейсы вне ядра coordinator: `transport_health`,
  `verification_environment_health`, `retry_reason_classifier`, `context_telemetry_provider`,
  `human_notifier`, `runtime_access`. Значение — `none` (инертный дефолт), имя, зарегистрированное
  хост-процессом, или `module:factory` (вызываемая без аргументов фабрика в импортируемом модуле).
  Неизвестное имя — fail-closed. Ни один extension не добавляет model tool и не меняет system prompt.

`harness health` проверяет форму всех трёх разделов. Пока у проекта нет
`assignment_plans`, coordinator работает на встроенных дефолтах и эти значения не читает.

### Доступ ролей: `access_policy` и `runtime_access`

Необязательный раздел `access_policy` задаёт режим sandbox и границы сети и файловой системы для
worker. Без него brief сохраняет `inherit`, а `dispatch send` ведёт себя как раньше. Раздел не
требует `assignment_plans`: coordinator читает `access_policy` и в конфиге, где есть только он.

```json
"access_policy": {
  "defaults": {"mode": "sandbox",
               "network": {"hosts": ["github.com"]},
               "filesystem": [{"resource": "checkout", "access": "write"},
                              {"resource": "git_common", "access": "write"},
                              {"resource": "cache", "access": "write", "path": "~/.cache/uv"}]},
  "roles": {"code-review": {"filesystem": [{"resource": "checkout", "access": "read"}]}},
  "operations": {"qa": {"network": {"hosts": []}}}
},
"extensions": {"runtime_access": "my_runtime:factory"}
```

- Компоненты `mode` (`inherit`, `sandbox`, `unsandboxed`), `network.hosts` и `filesystem`
  выбираются независимо от транспорта роли: `in-process` или `external`.
- Override роли (`roles`) или операции (`operations`: `qa`, `git`, `publish`) заменяет только те
  компоненты, которые в нём указаны, а остальные берутся из `defaults`. Операции `qa`, `git` и
  `publish` выполняет сам coordinator, а не worker, поэтому к ним применяются только `defaults` и
  собственный override операции, а `roles` — никогда. Операцию выбирает сама команда: `publish` для
  `--purpose publish`, `qa` для роли `qa` и `integration local-qa`, `git` для `integration refresh`
  и `integration resolve`. Что именно проверяется, описывает раздел «Доступ QA, Git и publish» ниже.
- `filesystem` называет ресурс: `checkout`, `git_common`, `shared_storage` или `cache` (только `cache`
  требует `path`). Coordinator превращает их в реальные пути worktree, общего Git-каталога и
  хранилища. Корень диска и домашний каталог целиком отклоняются.
- Права на запись не расширяют `write_paths` brief, tool policy и нативные подтверждения, а
  read-only роль не получает запись в `checkout`, `git_common` и `shared_storage`.
- Coordinator записывает разрешённый план (`runtime_access`) в brief, и его sha256 входит в
  transition digest утверждения. Правка конфига после `dispatch propose` не расширяет
  утверждённый dispatch: новый доступ требует нового `propose` и нового approval. Briefs без этого
  поля остаются валидными и читаются как `inherit`.
- `dispatch preflight` показывает разрешённый план и сверку с `runtime_access`. Настройки пользователя
  и аттестация Git-checkout не считаются доказательством текущих прав сети и файловой системы.
- Расширение `runtime_access` — это нативная реализация, которая наблюдает окружение именно этого
  worker, применяет план и выполняет handoff (`observe`, `apply`, `handoff`). Значение `none` ничего
  не доказывает, поэтому без нативной реализации coordinator блокирует `dispatch send` для плана с
  `sandbox`, `unsandboxed`, хостами или путями. Блокировка наступает до передачи работы: тихой
  подмены и отката на `inherit` нет, а нативное подтверждение ничем не заменяется. Харнесс такую
  реализацию не поставляет, и `my_runtime:factory` в примере выше — модуль вашего проекта. Пока он не
  подключён, любой `access_policy`, даже `mode: inherit`, блокирует `dispatch send`. `dispatch status`
  сохраняет статус и доказательство worker (`runtime_access`).

Если `dispatch send` остановлен, ошибка называет причину. Remedy одинаковый: подготовить указанные
хосты, пути и режим в новой сессии runtime; подключить реализацию `runtime_access`, которая
проверяет реальный запуск worker; повторить `dispatch preflight`.

### Доступ QA, Git и publish

`qa run`, `integration local-qa`, `integration refresh`, `integration resolve` и `dispatch publish`
выполняет сам coordinator в своём процессе. Если в проекте нет `access_policy`, они работают как
раньше (`legacy-inherit`). Отказ среды всё равно останавливает их, но с обычной классификацией
ошибки Git или checkout. Если `access_policy` есть, перед действием coordinator проверяет план:

- Какой план. Для `qa run` и `dispatch publish` — план, закреплённый в approved brief. Правка
  конфига после `dispatch propose` его не расширяет. Brief без плана остаётся `legacy-inherit`. Для
  `integration local-qa`, `integration refresh` и `integration resolve` — `defaults` и override
  операции (`qa`, `git`) из живого конфига. Override роли не применяется ни к одной из них.
- Что требуется. Запись в общий Git-каталог и общее хранилище `.harness`; запись в каталог
  clean-room checkout (`qa`) или в worktree batch (`git`); пути `cache` из `filesystem`. Для `git`
  и `publish` нужна ещё и доступность remote. Coordinator проверяет хост remote по `network.hosts`,
  когда режим `sandbox` или список хостов непуст. План должен разрешать эти требования. Brief,
  закреплённый до этой версии без записи в Git и хранилище, потребует нового `dispatch propose`.
  Требование QA-роли «только чтение» не распространяется на coordinator-операцию.
- Как проверяется. Coordinator реально пробует запись (создаёт и сразу удаляет файл с уникальным
  именем) и читает remote через `git ls-remote`. Режим `inherit` ничего не утверждает о самой среде.
  Он лишь означает, что coordinator работает в своём окружении. Coordinator принимает `sandbox` и
  `unsandboxed`, только если подключённая реализация `runtime_access` подтверждает их для процесса
  coordinator (`observe` с транспортом `in-process`). Без неё режим считается неподдерживаемым.
  Харнесс не поставляет такую реализацию и не заявляет нативной поддержки. Ваш проект определяет,
  что именно подтверждает ваша реализация.
- Результат. Подтверждённый отказ (`denied`), неподдерживаемый режим (`unsupported`) и непроверенное
  требование (`unverified`, например путь не существует или remote недоступен) останавливают
  действие до первого изменения. Ошибка называет ресурс, путь и причину. Её remedy — конкретное
  действие среды. Структурное evidence (`operation`, `status`, `plan_digest`, `checks`) доступно
  в записи попытки: `qa-lane/attempts/*.json` для `qa run` (стадия `access`) и запись попытки
  `integration local-qa` (стадия `access`, поля `access` и `remedy`). `dispatch publish` возвращает
  evidence успешной проверки в поле `access`.

Что остаётся нетронутым при отказе. Отказ `qa run` происходит до постановки в очередь. Lease и
запись очереди не создаются, dispatch остаётся `approved`, предыдущие evidence и попытки
сохраняются. Следующий запуск после исправления среды проходит без ручной очистки. Любой сбой
`qa run` уже после взятия lease (создание checkout, смена состояния, запись evidence, неожиданное
исключение) снимает lease, запись очереди и состояние dispatch. Отказ перед `refresh`, `resolve` и
`publish` происходит до fetch, rebase, создания batch и push. Откат отказавшего rebase возвращает
worktree на issue-ветку. Отказ записи метаданных или remote при rebase и push coordinator
классифицирует как ошибку доступа (`metadata-write-denied`, `remote-access-denied`,
`remote-unreachable`), а не как конфликт или обычный сбой Git. Для классификации coordinator
запускает Git с `LC_ALL=C`.

Диагностика по сообщению:

| Сообщение | Что сделать |
| --- | --- |
| `qa access is denied: shared Git metadata (write …)` | Выдать процессу coordinator запись в указанный общий Git-каталог в sandbox или правах файловой системы. |
| `… clean-room checkout (write …)` или `could not prepare clean QA checkout storage` | Выдать запись в `.harness/.sandboxes/runs/qa` (или каталог хранилища, названный в ошибке). |
| `… is unsupported` / `mode … is not provable for the coordinator` | Подключить `runtime_access`, подтверждающую режим для coordinator, либо задать `mode: inherit` для операции в `access_policy.operations`. |
| `host … is not in the approved network hosts` | Добавить хост в `network.hosts` операции и, для операции с brief, заново выполнить `dispatch propose`. |
| `remote … ` недоступен (`unverified`) | Восстановить связь с remote из среды coordinator и повторить команду. |
| `remote-access-denied` при push | Выдать учётным данным процесса право записи в issue-ветку remote; затем повторить команду. |

Обход блокировки инструментом или хуком не предусмотрен. При блокировке остановитесь и сообщите о
ней.

### Discovery Context и Context Package

Discovery Pipeline переносит проверенный контекст от проектирования к dispatch. `/grilling` ведёт
`Live Artifact` с кандидатными путями, но добавляет путь только после явного согласия пользователя.
`/to-spec` сохраняет утверждённый список в эпике под `## Relevant Files (Discovery Context)`.
`/to-tickets` назначает каждый путь подходящему tracer-bullet тикету и строит Path inventory. Один
cheap advisory-вызов может добавить только точные зависимости из этого Path inventory. Его вывод —
не evidence и не authority.

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
`harness/project/docs-agents/*`) остаются стартовыми файлами, но оценка учитывает их содержимое один
раз; причина второй копии называет оригинал и требует держать копии идентичными. Markdown-файл
входит в пакет как оглавление, если его оценка не меньше
`context_package_policy.section_index_min_tokens` (по умолчанию 20000). `sections` перечисляет
заголовки уровней 1–3 (вне fenced code) с `start_line`/`end_line`; оценка учитывает только это
оглавление, а роль читает лишь нужные ей диапазоны строк. Файл без заголовков учитывается целиком.
Для Python AST извлекает сигнатуры прямых локальных зависимостей, при этом текущий
Discovery-контракт ограничивает разворачивание одним уровнем. Неподдержанный формат получает первые
30 строк как deterministic fallback. При превышении token limit сборка завершается ошибкой и не
обрезает пакет молча. Legacy `--max-package-size-bytes` можно задать как дополнительную диагностику,
но он не заменяет token limit.

Запись package immutable, versioned и hash-проверяема. Она shared внутри batch. Один и тот же
base/candidate переиспользует один package ID между architect, developer и continuation sessions.
Новый package создаётся только после нового candidate. Coordinator не переиспользует package, если
его frozen источник из `memory.pointers` после регистрации изменился, стал недоступен или отозван.
Тогда следующий dispatch регистрирует новый package с актуальными указателями. Старый package
остаётся неизменным для уже выданных brief. Coordinator проверяет freshness до handoff и не создаёт
brief со stale package. Если и новый package не свежий по памяти, ошибка называет путь, ожидаемый и
фактический `source_hash`. Remedy предлагает `harness memory rebuild .` из основного checkout или
`--no-memory`. Brief передаёт compact summary (starting files, related tests, precedents и pinned
commits). Полный diff остаётся в package один раз. Package не заменяет immutable brief и не
отменяет обязательные self-report, heartbeat, review или QA.

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

Одна задача может пройти несколько ролей. Но handoff внутри одного batch всегда последовательный, и
в batch бывает только один active writer. Batch с пересекающимися файлами могут идти параллельно,
каждый в своих issue-ветке и worktree, пока хватает `concurrency_budget`. Пересечения разбираются
при интеграции и не блокируют запуск. Coordinator отклоняет два batch на один и тот же
незавершённый тикет, ветку или worktree. Тяжёлые integration/quality checks идут в одной
serialized quality-gate lane.

Для API/public contract, schema/data migration, outbox/queues, transactions,
authorization/security и concurrency/retry completion невозможен, пока coordinator не получил оба
независимых отчёта `code-review`: Standards и Spec.

После developer dispatch candidate commit получает детерминированную оценку рисков из DoD, changed
files и developer-reported triggers. Оценка решает, когда composite read-only `code-review`
**обязателен**, но не когда он *разрешён*. Review можно создать для любого candidate, и конвейер
`/implement` делает это всегда. Оси Standards и Spec остаются отдельными evidence. Отдельный QA
dispatch создаётся только после принятого review (если он обязателен): обязательный полный QA gate
в clean-room нельзя заменить локальной проверкой developer-а. Любой новый candidate commit после
finding или failed QA снова проходит оценку риска.

## 4. Coordinator CLI и lifecycle

Runtime-neutral режим не имеет команды «запустить всех». Coordinator CLI ведёт записи по
`planned → awaiting-approval ↔ active → completed | blocked | failed`. При `manual_all` каждый
report оставляет dispatch в `reported` до решения человека. При `low_risk` coordinator
автоматически принимает чистый завершённый report batch, если scope batch целиком лежит в
`low_risk_paths`. Решение он записывает в ledger. Blockers, failed checks, раскрытые risks, risk
triggers и findings любой оси review сохраняют ручной gate. Publish тоже требует отдельного
approval. При `milestone` coordinator также автоматически принимает чистый отчёт обычной роли. Но
QA, publish и рискованные переходы остаются ручными вехами. При `auto` coordinator проходит путь
от `batch approve` до принятого publish без человека. Политика согласует каждый шаг, а
`batch auto-decide` принимает решения по отчётам (см. «Автоматический путь `approval_policy: auto`»
ниже). Ручными остаются открытие PR и merge.

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
   `base_commit`/`integration_base_commit`; необновлённый локальный HEAD её никогда не заменяет.
   Полный маршрут `/implement` требует явно указать `--required-gate review --required-gate qa`
   (независимые Standards/Spec review и full clean-room QA), и coordinator сверяет их наличие в
   `required_gates` до отправки write-role (`developer`) worker-а. В допустимых прямых CLI-сценариях
   `--required-gate` можно опустить (по умолчанию `["none"]`). Дальше review, QA и publish проверяют
   закреплённый candidate, даже если `origin/<ref>` ушёл вперёд: обязательной проверки свежести базы
   и принудительного developer-перезапуска нет, и другие batch это не останавливает. Финальное
   обновление базы выполняет `integration refresh` при подготовке PR (раздел «Integration accounting
   после publish»).
2. Сверить активные batch, `concurrency_budget`, writer и quality-gate lane. Пересечение файлов
   другого batch не повод откладывать запуск, а занятая serialized quality-gate lane не мешает
   параллельной реализации. Если batch упёрся в бюджет, дождитесь завершения активного batch или
   поднимите `concurrency_budget`.
3. Создать и отдельно утвердить architect dispatch, принять его отчёт и только потом создать
   developer dispatch. Порядок жёсткий: coordinator отклоняет `dispatch create --role developer`,
   пока для того же batch нет architect-отчёта, принятого через `batch decide --decision accept`.
   Правило живёт в `coordinator.py`, поэтому действует и для ручного CLI, и для `/implement`. CLI
   сохраняет immutable brief до передачи:

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

   Для роли с `transport: "in-process"` adapter не передаётся вовсе. `dispatch send --dispatch <id>`
   возвращает путь к brief. После этого coordinator **немедленно** запускает роль как субагента
   текущей сессии. Между этими действиями нет никаких чтений предыдущих dispatch/report/template. Без
   `.harness/orchestration.json` к `dispatch create` добавляются `--model` и `--effort` вызывающей
   сессии. Для coordinator и architect выбирайте `medium`, если разработчик явно не одобрил иное.

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
   или флаг передан не с `--decision accept` на отчёте architect. Тогда отчёт остаётся ожидающим
   решения, а batch не меняется. Coordinator сохраняет проверенный план в batch как `commit_plan`.
   Решение architect получает `commit_plan_sha256`. Этот план становится `commit_plan` каждого
   developer brief batch-а, включая `developer-retry`. Если план в batch не совпадает с digest
   принятого решения, developer brief не создаётся. Без файла brief содержит по одной entry `step-N`
   на пункт DoD с `covers: [N]`. При `low_risk` и `milestone` coordinator автоматически принимает
   чистый architect report с планом по умолчанию. Поэтому architect, который предлагает другой план,
   указывает это в `risks`, и report ждёт ручного решения.

   `dispatch send --role code-review` — единственный случай, когда нужен ещё один обязательный флаг:
   `--checkout <путь>`. Он указывает на worktree, реально зачекаученный на `candidate_commit`
   dispatch-а (см. clean-room QA lane ниже: та же изоляция нужна и для review). Все остальные роли
   не передают `--checkout`. Без него coordinator отклоняет `dispatch send` и показывает точный текст
   ожидаемого флага.
4. Принять один schema-validated completion report с evidence. Coordinator сохраняет его как
   canonical JSON и детерминированную Markdown-проекцию. После этого dispatch остаётся `reported`, а
   batch ждёт следующего решения:

   ```bash
   python .harness/orchestration/coordinator.py --repo . report submit \
     --file developer-report.json
   ```
   До следующего dispatch coordinator записывает отдельное решение. При `manual_all` или нечистом
   report человек принимает report, override-ит warning либо требует retry. При `low_risk` и чистом
   report coordinator сам записывает `accept` с rationale
   `Auto-accepted due to low_risk policy and clean report`, вычисляет `next_action` и готовит
   следующий допустимый dispatch. При `milestone` чистый отчёт вне вехи также получает `accept`.
   После developer coordinator оценивает риск, а QA-dispatch ждёт отдельного approval. Чистый
   QA-report при `milestone` ждёт решения человека.

   Policy-цепочка (`policy-decide`, `risk-assess`, `next-dispatch`) идёт отдельными командами уже
   после записи report. Каждая команда может остановиться: ledger занят, поднят `needs_attention`,
   изменилось состояние batch. Тогда `report submit` всё равно завершается с кодом 0. Report
   записан, ответ называет его (`report`, `report_sha256`) и несёт объект `completion`. Объект
   содержит `route: "report-completion"`, `failed_step`, состояние каждого шага (`done`,
   `already-done`, `not-applicable`, `failed`, `not-run`), `error`, `remedy` и точную команду
   `command`. Её выполняет сам coordinator (`run_by: "coordinator"`), а не worker:

   ```bash
   python .harness/orchestration/coordinator.py --repo . report complete --dispatch <dispatch-id>
   ```

   `report complete` идемпотентна: каждый шаг выводится из ledger, поэтому уже записанное решение,
   risk assessment или следующий dispatch не повторяются, а повторный запуск ничего не пишет.
   Команда повторяет только policy, записанную при `report submit` (`auto_accept_policy` в статусе
   dispatch), никогда не записывает report заново и не создаёт dispatch для роли, сдавшей report.
   Шаг `risk-assess` оценивает candidate report-а: у developer это его `commit_sha` и
   `changed_files`, а у read-only verification — candidate, закреплённый в её dispatch, с файлами из
   diff от base batch, как их считает `risk assess`. Шаг, которому нужен человек, останавливается с
   remedy этого шага. Для report, оставленного человеку, все шаги — `not-applicable`. Повторно
   отправлять report нельзя: он уже записан.

   Ручное решение выглядит так:

   ```bash
   python .harness/orchestration/coordinator.py --repo . batch decide \
     --batch <batch-id> --decision accept --approved-by 'имя утверждающего' \
     --approved-at 2026-09-09T12:02:00Z
   ```

   Developer report против brief с `commit_plan` несёт `commit_map`. Это пары
   `{commit_sha, plan_entry_id}` для каждого коммита после `snapshot_commit` (у rebase — после
   `rebase_target`). В initial и rebase отчёте это отношение. Коммит, закрывающий несколько
   entries, даёт по паре на каждую entry. Entry, закрытая несколькими коммитами, даёт по паре на
   каждый коммит. Отображение не one-to-one (объединённый коммит, разделённая или незакрытая entry) —
   это расхождение. Тогда отчёт обязан нести `dod_coverage` — ровно по записи на каждый пункт DoD:
   `{"dod_item": <n>, "commits": [<sha>, ...]}` из коммитов этого dispatch или
   `{"dod_item": <n>, "not_covered": "<причина>"}`. Также отчёт обязан нести непустой
   `divergence_justification`: что объединено, разделено или добавлено и почему. При one-to-one
   `dod_coverage` необязателен (coordinator выводит покрытие из `covers` плана), а
   `divergence_justification` отклоняется.
   `report submit` и `batch decide` отклоняют структурные ошибки, каждую с remedy:
   неотображённый созданный коммит, коммит не из этого dispatch, неизвестная entry, повтор пары,
   расхождение без `dod_coverage` или без обоснования, покрытие без пункта, с чужим пунктом или
   чужим коммитом, `not_covered` без причины. В `developer-retry` и в отчётах других ролей эти два
   поля отклоняются.

   `report submit` также сверяет `dod_coverage` с `commit_map` и `covers` плана. Команда отклоняет
   пункт DoD, заявленный покрытым набором коммитов, если `commit_map` не сопоставляет ни один из
   них с entry, у которой этот пункт есть в `covers`. Remedy называет пункт, заявленные коммиты и
   entries плана, покрывающие этот пункт. Обратное направление допустимо. `not_covered` с причиной
   валиден и для пункта, который сопоставление формально покрывает. Как любой `not_covered`, он
   требует ручного решения. One-to-one отчёт без `dod_coverage` получает покрытие из `covers` и не
   требует этой сверки.

   Обоснованное расхождение с полным покрытием само по себе не делает отчёт нечистым. При
   `low_risk` и `milestone` coordinator автоматически принимает отчёт, чистый в остальном. Запись
   решения получает `commit_plan_divergence` (`developer_dispatch_id`, `justification`,
   `merged_commits`, `split_entries`, `unclosed_entries`). Ту же запись получает и ручное принятие.
   Любой пункт `not_covered` делает отчёт нечистым при любой policy. Auto-accept не срабатывает,
   `--decision accept` отклоняется. Принять отчёт можно только через `--decision override-warning` с
   `--note`, отличным от `none` (решение получает `dod_not_covered` с причинами). Иначе отчёт можно
   вернуть через `retry`. Выход за scope — предупреждение, а не отказ (#633). Completed-отчёт
   developer с `changed_files` вне `write_paths` `report submit` записывает. `batch decision-packet`
   показывает эти пути в `scope_warnings`. Такой отчёт не принимается автоматически ни при одной
   policy, а `--decision accept` отклоняется. Принять его можно только через
   `--decision override-warning` с `--note`, отличным от `none`, и явным `--approved-by`. Решение
   получает `scope_warnings`, а override добавляет coordinator finding. Следующий brief code-review
   несёт эти пути как пункт, который review должен закрыть. Принятие architect-отчёта с
   `--commit-plan-file` проходит, если `expected_paths` плана выходят за `allowed_paths` batch-а.
   Тогда packet (с тем же `--commit-plan-file`) и решение содержат те же `scope_warnings`. Запрет
   записи вне `write_paths` для самого worker-а, проверка `changed_files` в checkpoint и отчёты
   других write-ролей остаются строгими.
   `batch decision-packet` показывает `dod_coverage`, `dod_coverage_source` (`report` или
   `derived`) и `commit_plan_divergence`. Brief code-review несёт `commit_plan_divergence`
   последнего принятого initial или rebase developer report (у остальных ролей поле `null`). По
   нему reviewer проверяет, что границы коммитов остались reviewable.

   Дефект, который coordinator нашёл в чистом developer report, не тратит developer-retry до review
   (маршрут `carry-over`). Retry developer report без accept — исключение только для невыполненного
   пункта DoD или изменения вне scope. В остальных случаях report принимается с находками:

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
   `override-warning` на completed developer work report. Иначе решение отклоняется с remedy, и
   batch не меняется. Coordinator записывает каждую находку в batch append-only как запись
   `carried_items`: `item_id` `coordinator-finding-<n>` (порядковый номер в batch), `source` (`kind`,
   `dispatch_id` и `report_sha256` принятого report, `candidate_commit`), `summary`, `files`,
   `expected_evidence`, `attached_at`, `attached_by` и `record_sha256`. Coordinator сверяет
   этот hash при каждом чтении batch.

   Policy auto-accept не принимает файл находок. После него находки добавляет `batch carry-over`:

   ```bash
   python .harness/orchestration/coordinator.py --repo . batch carry-over \
     --batch <batch-id> --findings-file .harness/.sandboxes/scratch/findings.json
   ```

   Команда не создаёт dispatch и не меняет candidate, поэтому человек её не утверждает. Она
   записывает решение coordinator `carry-over` с `approved_by: policy:carry-over`. Находки она
   прикрепляет к последнему принятому developer work report. Batch с `next_action: qa` переходит в
   `code-review`. Команда отклоняется с remedy: пока какой-либо report ждёт решения; если в batch нет
   принятого developer work report; если после него уже создан dispatch, не отменённый и не
   переведённый `batch resume` в `abandoned`. Для неотправленного code-review (или qa, созданного
   low_risk-цепочкой) remedy — `dispatch cancel` и повтор `batch carry-over`. Для отправленного —
   сообщить дефект при решении его report. Для уже решённого report — приложить находку через
   `batch decide --findings-file` при accept следующего developer report.

   Пока находка открыта, risk assessment ведёт candidate в `code-review`, даже если ни один триггер
   не совпал (`review_required` записи оценки не меняется). Находка закрыта, когда принят (`accept`
   или `override-warning`) code-review, чей brief её нёс. Retry review оставляет её открытой.

   Каждый brief несёт секцию `carried_items` — общий канал переносимых пунктов. Это объект: ключ —
   вид источника (`coordinator-finding`, `review-finding`, `incomplete-item`), значение — список
   `{item_id, source, summary, files, expected_evidence}`. Пустой канал — `{}`. Brief code-review и
   developer work несёт все открытые `coordinator-finding`. Developer brief, отвечающий на `retry`
   code-review с маршрутом `fix-forward`, несёт ещё находки осей Standards и Spec этого review
   как `review-finding` (`item_id` `review-finding-<n>`; `source` — `dispatch_id`, `report_sha256`,
   `axis`, `severity`; `files: []`; `expected_evidence` — evidence находки). Поэтому единственный
   developer-retry закрывает обе группы и тратит `retry_policy.max_developer_retries` один раз.
   Accept с находками и `batch carry-over` бюджет не тратят. Непустая секция входит в transition как
   `carried_items_sha256`. Поэтому находка, добавленная после `dispatch propose`, требует нового
   approval.

   Отчёт code-review отчитывается по каждому пункту brief в необязательном `review.carried_items`:
   `[{"item_id": ..., "status": "closed" | "open" | "unverified", "evidence": ...}]`. `report submit`
   отклоняет пункт, которого brief не нёс, повтор пункта, неизвестный статус и пустое evidence.
   Пропуск пункта структурно допустим, но пропуск, как и `unverified` и `open`, — это carried gap, и
   такой отчёт не clean: policy его автоматически не принимает, а `--decision accept` отклоняется.
   `override-warning` требует `--note`, отличный от `none`, и записывает в решение
   `carried_items_gap`. Отчёт можно и вернуть через `retry`. Правила blocker и warning сохраняют
   приоритет. Пункт `open` — структурное evidence категории `code`, поэтому такой retry ведёт в
   developer-retry маршрутом `fix-forward`. `batch decision-packet` показывает `carried_items`
   (каждый пункт с `source`, `summary`, `status` — у отчёта code-review `omitted` для пропущенного
   пункта — и `evidence`) и `carried_items_gap`.

   Read-only роль (architect, verification, code-review или qa), не выполнившая часть brief,
   перечисляет невыполненные пункты в необязательном поле отчёта `incomplete_items`:
   `[{"brief_item": ..., "reason": ..., "target_role": ...}]`. Текст пишется на английском, потому что
   пункт уходит в brief следующей роли как agent-to-agent текст. Каждая строка непустая и не длиннее
   1600 символов. `target_role` — сама роль отчёта (для суженного retry) или более поздняя роль:
   architect → architect, developer, code-review или qa; verification → verification, code-review
   или qa; code-review → code-review или qa; qa → qa. Пункт, который не дал выполнить инструмент
   (например, классификатор безопасности), несёт собственный `tooling_blocker` той же формы, что и у
   отчёта, при любом outcome. `report submit` с remedy отклоняет поле у write-роли, пункт без
   `reason`, неизвестную или недопустимую `target_role`, кириллицу и некорректный `tooling_blocker`.

   Отчёт с непустым `incomplete_items` не принимается автоматически ни при одной `approval_policy`, а
   простой `accept` или `override-warning` отклоняется. Человек выбирает одно из двух решений:

   ```bash
   python .harness/orchestration/coordinator.py --repo . batch decide \
     --batch <batch-id> --decision accept --approved-by 'имя утверждающего' \
     --approved-at 2026-09-09T12:02:00Z --carry-incomplete
   python .harness/orchestration/coordinator.py --repo . batch decide \
     --batch <batch-id> --decision retry --approved-by 'имя утверждающего' \
     --approved-at 2026-09-09T12:02:00Z --narrowed
   ```

   `--carry-incomplete` (с `accept` или `override-warning`) записывает routing record `carry-over` с
   `carried_item_ids` (`incomplete-item-<n>`, сквозная нумерация в batch). `next_action` меняется как
   при любом accept этой стадии. Batch не хранит пункты. Work brief целевой роли читает их из
   immutable отчёта и несёт в `carried_items` под видом `incomplete-item`. Так продолжается, пока не
   принят (`accept` или `override-warning`) dispatch этой роли, чей brief их нёс. Retry оставляет
   пункт открытым. Открытый пункт для code-review ведёт candidate в code-review, даже если risk
   assessment не нашла триггеров. Флаг отклоняется без пунктов, с другим решением и тогда, когда хоть
   один пункт нацелен на саму роль отчёта. Такой пункт закрывает только суженный retry.

   `--narrowed` (только с `retry`) повторяет ту же стадию на том же SHA. Brief нового dispatch несёт
   только пункты отчёта. Definition of Done в brief остаётся полным DoD batch. Маршрут —
   `narrowed-retry` с `reason_category: null`. Если хоть один пункт несёт `tooling_blocker`, маршрут —
   `tooling-retry` с категорией `tooling` (подтвердить ложное срабатывание, завести bug-тикет на
   инструмент; серию ограничивает attention `tooling-retry-repeated`). Бюджет
   `retry_policy.max_developer_retries` не тратится, а новый dispatch утверждается по
   `approval_policy`. `--narrowed` отклоняется: для developer и publish; для отчёта без пунктов;
   вместе с `--reason-category` или `--retry-role developer`; при структурном evidence другого
   маршрута. Такое evidence — finding или severity warning/blocker на оси, открытый carried item,
   failed check, сдвинутый candidate. Retry без `--narrowed` игнорирует пункты и маршрутизируется как
   раньше.

   `source` пункта `incomplete-item` содержит `dispatch_id` и `report_sha256` отчёта, его `role`,
   `target_role` (роль, чей brief несёт пункт), `route` (`carry-over`, `narrowed-retry` или
   `tooling-retry`), `reason` и `reason_category` (`tooling` у пункта с `tooling_blocker`, иначе
   `null`). `summary` — это `brief_item`, `files` пуст. `batch decision-packet` показывает
   `incomplete_items` отчёта; без `--findings-file` он добавляет `route_preview["carry-over"]` —
   запись, которую сделает `--carry-incomplete`, либо отказ. `batch decision-packet --narrowed`
   показывает маршрут суженного retry.

Минимальный ручной brief хранит ticket и dispatch ID, роль и её access, выбранный
profile/model/effort, allowed paths (scope записи), issue-ветку/worktree, DoD, запреты, команды,
dependencies, approval. Для write-роли completion report обязан включать commit SHA, exact changed
files, результаты всех checks, risks, blockers и следующее решение coordinator-а. Write-роль может
остановиться до изменений (например, из-за risk trigger при отсутствии необходимого gate). Тогда
coordinator принимает честный отчёт с `outcome: blocked`, пустым `changed_files` и `commit_sha`,
привязанным к проверенному checkout (`snapshot_commit`). Coordinator регистрирует отчёт и
возвращает `decision_packet` с `recovery_route` (`retry`, `block`, `abandon`). Незавершённый
candidate он не регистрирует. Для read-only роли вместо SHA указывается
`not applicable — read-only role`.

Новые факты не меняют отправленный brief: coordinator добавляет отдельное решение с evidence. Если
изменились scope, DoD, assignment или proof, текущий dispatch заканчивается и создаётся новый.
Повтор после `blocked` или `failed` — тоже новый dispatch с новым ID и brief.

### Маршрутизация `retry` и решение `abandon`

`batch decide --decision retry` больше не означает «снова developer». Coordinator сохраняет на
решении routing record: `route`, `previous_role`, `reason_category`, `next_role`, `next_action`,
`rationale` и `candidate_commit` (пока он не изменился). Coordinator определяет причину только по
структурированным данным report: outcome, findings, severity осей Standards/Spec, failed checks и
тому, изменился ли candidate. Свободный текст `blockers`/`output` не классифицируется. Явную причину
можно передать через `--reason-category` (`code`, `requirements`, `candidate-change`,
`verification-infrastructure`, `transport`, `context-pressure`, `tooling`, `block-bypass`,
`unknown`). Но она не отменяет найденный finding. Rate limit, недоступный Bash/WSL wrapper и
transport failure — это operational evidence (`verification-infrastructure` или `transport`), а не
code finding. Context limit даёт `context-pressure`, только если для отчитавшегося dispatch записано
`critical`-наблюдение `context_pressure` (см. ниже). Голое утверждение даёт `unknown`.

| Стадия отчёта | `accept` | `retry` | `block` / `fail` | `abandon` |
| --- | --- | --- | --- | --- |
| architect | developer | новый architect | terminal | `abandoned` |
| developer | risk assessment | `developer-retry` продолжает непринятый candidate этого report (`snapshot_commit` — его `commit_sha`); retry создаёт новый candidate, и тот получает новую risk assessment; при непустом закрытом списке перенесённых пунктов маршрут — `fix-forward` | terminal | `abandoned` |
| code-review | qa | новый code-review на том же `candidate_commit`, если report `blocked`, причина — `verification-infrastructure`/`transport`/`context-pressure`, findings пусты, обе оси без findings, нет failed check и candidate не менялся; иначе `developer-retry`, а при непустом закрытом списке перенесённых пунктов — `fix-forward` | terminal | `abandoned` |
| qa | publish | новый qa на том же SHA при том же условии (QA остаётся read-only); defect или новый candidate — `developer-retry` | terminal | `abandoned` |
| publish | `completed` | новый publish на том же принятом SHA при `verification-infrastructure`/`transport`/`context-pressure`; `developer-retry`, если candidate должен измениться | terminal | `abandoned` |

`code`, `requirements`, `candidate-change` и `unknown` всегда ведут в `developer-retry`. Повторить
read-only стадию на том же SHA могут только три operational-категории, и лишь при пустых findings,
неизменном candidate и отсутствии scope/requirement blocker. Противоречивая или неподтверждённая
причина всегда даёт безопасный маршрут `developer-retry`. Повтор на том же SHA — это новый
immutable dispatch: новый dispatch ID, повторная проверка свежести Context Package и собственное
явное approval при `manual_all`, а прежние brief, report и blocker остаются audit evidence.
Фиктивные и пустые commit не используются, и новый candidate всегда требует новой risk assessment.
`block` и `fail` сами retry не запускают. `--retry-role developer` принудительно выбирает developer
retry там, где coordinator иначе повторил бы ту же роль на том же SHA.

`tooling` — отдельная операционная категория: hook, классификатор безопасности или ledger
заблокировал законное действие роли. Coordinator присваивает её только по структурному полю
`tooling_blocker` (`tool`, точная `command`, `message`) в report с `outcome: blocked`. Категорию не
должны перекрывать finding, failed check, сдвинутый candidate или developer-категория. Явная
`--reason-category tooling` без этого поля даёт `unknown`. Другая названная операционная категория
идёт своим прежним маршрутом. Маршрут `tooling` на любой стадии — `tooling-retry`: новый dispatch той
же стадии на том же SHA (architect, verification, code-review, qa или publish). Для developer это
`developer-retry`, который продолжает его последний коммит. Этот коммит записывается в
`candidate_commit` routing record. В таблице выше «три operational-категории» по-прежнему означают
`verification-infrastructure`, `transport` и `context-pressure`.

Если hook заблокировал именно `git commit` developer, developer ничего не откатывает, а добавляет в
`tooling_blocker` поле `uncommitted_files` — непустой список без повторов, где каждый путь записан
так, как его печатает Git, и лежит внутри зоны записи. `commit_sha` при этом указывает на последний
коммит developer, и перезапуск `tooling-retry` наследует эти изменения. `dispatch preflight`
пинит HEAD на последний коммит developer и допускает грязный worktree, только если его
незакоммиченные пути (без состояния coordinator и sandbox-ов инструментов) точно совпадают со
списком из report и лежат в зоне batch. Лишний, недостающий или лежащий вне зоны файл preflight
отклоняет, а сообщение и remedy называют каждое расхождение. Чистый worktree без списка проходит, как
раньше, а для остальных dispatch preflight не проверяет worktree на незакоммиченные изменения.

`block-bypass` означает, что read-only роль (code-review, qa или verification) обошла блокировку
hook-а или инструмента вместо остановки с `tooling_blocker`. Эту категорию называет только
approver. Report с нарушением не считается evidence: его findings, failed checks и outcome не влияют
на маршрут, и лишь сдвинутый candidate по-прежнему ведёт в `developer-retry`. Маршрут —
`bypass-rerun`: новый dispatch той же стадии на том же SHA (для verification — на её
зарегистрированном candidate) без нового candidate commit. `batch decide` требует `--note` с
описанием нарушения, а сам report не принимается и не закрывается через override-warning. Новый
dispatch всегда требует явного approval (`--approved-by`) при любой `approval_policy`, кроме `auto`,
и `bypass-rerun` не расходует `retry_policy.max_developer_retries`. Для architect, developer и
publish `block-bypass` отклоняется: такой report по-прежнему получает `retry` с developer-категорией
(`code`, `requirements`, `candidate-change`) или `block`.

Retry непринятого developer report продолжает его историю. Пока batch ждёт этот `developer-retry`,
coordinator берёт candidate из immutable report (`commit_sha`, сверенный по hash) и пинит на него
`snapshot_commit` нового developer dispatch. Worktree не откатывается ни к base, ни к более старому
принятому candidate. `commit_map` retry считает только коммиты поверх `snapshot_commit`. Каждый
новый коммит закрывает ровно одну distinct entry плана, а `dod_coverage` и
`divergence_justification` в retry-отчёте отклоняются. Путь по умолчанию —
`dispatch propose`/`create` без `--candidate-commit`. Тогда brief получает `candidate_commit: null`
и не требует risk assessment, как developer retry после code-review. Чтобы привязать этот SHA к
transition digest, передайте `--candidate-commit <commit_sha>`. Тогда до
`dispatch propose`/`create`, пока batch в `awaiting-approval`, нужна `risk assess` для того же SHA.
Такая оценка — только evidence. `next_action` остаётся `developer-retry`, `required_next_role` и
требование нового candidate сохраняются, code-review и QA не открываются. `risk assess` для
другого SHA отклоняется. Retry-report с тем же SHA не принимается. Retry после code-review, qa или
publish по-прежнему пинит `snapshot_commit` на последний принятый developer candidate.

Fix-forward (#503) — маршрут существующего developer-retry, а не новый переход. Решение `retry`,
ведущее в `developer-retry`, записывает в routing record `retry_item_ids` — закрытый, возможно
пустой список перенесённых пунктов, который понесёт brief developer-retry. При retry code-review в
него входят открытые `coordinator-finding`, находки осей Standards и Spec этого review
(`review-finding`) и открытые `incomplete-item` для developer. При retry qa, publish или
verification входят те же пункты без находок review, а при retry developer work report — пункты его
собственного brief, ни один из которых не принят. Непустой список делает маршрут `fix-forward`;
`tooling-retry` developer сохраняет свой маршрут и пишет тот же список. Brief developer-retry несёт
ровно эти пункты, а расхождение с `retry_item_ids` отклоняется как нарушение инварианта.

Developer продолжает от `snapshot_commit` новыми коммитами поверх него и в completion report
отчитывается полем `carried_item_closure` — по одной записи на каждый пункт brief:
`{"item_id": <id>, "commits": [<sha>, ...]}` с коммитами цепочки retry, которые закрывают пункт,
или `{"item_id": <id>, "not_closed": "<причина>"}`. Цепочка retry — это этот dispatch и предыдущие
попытки, передавшие ему тот же закрытый список. Retry developer-отчёта и `tooling-retry` developer
передают следующей попытке пункты своего brief, а `snapshot_commit` следующей попытки — это HEAD
предыдущей. Коммиты отсчитываются от `snapshot_commit` первой попытки цепочки, от которого ещё
происходит `snapshot_commit` этого dispatch; при утверждённом rebase target отсчёт идёт от него, а
после ещё не принятой попытки `rebase-fix-forward` в цепочке — от её target. Поэтому пункт, закрытый
предыдущей попыткой, указывает её коммит, а после rebase — его перенесённую копию.

После попытки `rebase-fix-forward` цепочке принадлежат коммиты двух видов: новые коммиты её попыток
и перенесённые копии, у которых `rebased_from` в `commit_map` отчёта попытки указывает на коммит
цепочки. Более ранний rebase учитывается: копия копии ведёт к исходному коммиту. Перенесённая копия
коммита, который существовал до цепочки (он лежит в `snapshot_commit` первой попытки или ниже),
отклоняется с тем же remedy, что и оригинал. Исключение одно: старый rebase stale-base не пишет
`rebased_from`, и его closure по-прежнему считает все коммиты выше batch target. Completed-отчёт
такого brief обязан нести поле, blocked или failed отчёт может его нести, а отчёты остальных brief —
не могут. `report submit` отклоняет с remedy пропущенный, неизвестный или повторный пункт, пустую
причину, пустой список коммитов, неразрешимый SHA и коммит, который не создала цепочка retry;
коммиты сверяются с Git, в том числе у brief без `commit_plan`. Пункт `not_closed` — carried gap, и
такой отчёт не clean: policy его автоматически не принимает, а `--decision accept` отклоняется.
`override-warning` требует `--note`, отличный от `none`, и записывает в решение
`carried_items_gap`. `commit_map` retry-отчёта сохраняет строгое правило #478, а
`batch decision-packet` показывает у пунктов developer-retry статус `closed` с SHA коммитов или
`open` с причиной.

Без утверждённого rebase target `commit_sha` retry-отчёта обязан быть потомком `snapshot_commit`.
Отчёт после amend, squash или reset отклоняется с сообщением `developer-retry candidate <sha> does
not descend from snapshot_commit <snapshot>: the retry rewrote the history it continues (amend,
squash or reset)`. Remedy: восстановить переписанные коммиты из `git reflog`, повторить исправление
новыми коммитами без amend и squash и отчитаться новым HEAD. Retry rebase-отчёта legacy-записи
stale-base (`base_rebase_required`, записанной до ADR 0014) по-прежнему проверяется от её rebase
target.

Rebase-fix-forward (#504) — тоже маршрут существующего developer-retry, а не новый переход. Решение
`retry`, которое ведёт в `developer-retry` или `fix-forward` (retry developer, verification,
code-review, qa или publish report; не `tooling-retry` и не `bypass-rerun`), делает `git fetch` для
`origin/<integration_ref>`. Маршрут становится `rebase-fix-forward`, если выполнены оба условия: tip
ушёл вперёд от закреплённой `integration_base_commit`; candidate, который продолжит retry
(собственный у retry developer-отчёта, иначе последний принятый), ещё не содержит этот tip. Routing
record дополнительно называет `rebase_target_commit` (новый tip) и `integration_base_commit` (база,
от которой он ушёл) и сохраняет `retry_item_ids`. `next_action` остаётся `developer-retry`, и такой
retry расходует один `retry_policy.max_developer_retries`. Ошибка fetch отклоняет
`batch decide --decision retry` с remedy. `decision-packet` показывает её в `route_preview.retry`.
Решение только предлагает target: `integration_base_commit` batch до accept не меняется.

Target попадает в brief только через dispatch с явным approval при любой `approval_policy`, кроме
`auto`. Approval другой политики для него отклоняется. `dispatch propose` показывает переход с полем
`rebase_target_sha`, а brief получает `rebase_target_commit`. Кроме этого brief поле заполнено
только у developer-retry замещающего batch (см. «Замещающий batch» ниже). У остальных brief оно
равно `null`, в том числе у developer dispatch legacy-записи stale-base:

```bash
python .harness/orchestration/coordinator.py --repo . batch decision-packet --batch <batch-id>
python .harness/orchestration/coordinator.py --repo . batch decide --batch <batch-id> \
  --decision retry --reason-category code --approved-by 'имя утверждающего' \
  --approved-at 2026-10-07T09:00:00Z
python .harness/orchestration/coordinator.py --repo . dispatch propose \
  --batch <batch-id> --role developer --runtime claude
python .harness/orchestration/coordinator.py --repo . dispatch create \
  --batch <batch-id> --role developer --runtime claude --approved-by 'имя утверждающего' \
  --approved-at 2026-10-07T09:05:00Z --transition-digest <transition_digest>
```

Developer переносит коммиты над старой базой ровно на этот target
(`git rebase --onto <rebase_target_commit> $(git merge-base <snapshot_commit> <rebase_target_commit>)`),
разрешает конфликты и добавляет fix-коммиты в том же dispatch. Его `commit_map` перечисляет каждый
коммит прежнего candidate (после `git merge-base` от `snapshot_commit` и target до
`snapshot_commit`) ровно один раз: `{"commit_sha": <копия>, "rebased_from": <оригинал>}` —
перенесённая копия наследует пункт плана оригинала, или `{"rebased_from": <оригинал>, "dropped":
"<причина>"}` — коммит, который rebase не перенёс. Каждый коммит после target встречается ровно
один раз: как перенесённая копия или как новый коммит `{commit_sha, plan_entry_id}` по строгому
правилу retry. `changed_files` и `carried_item_closure` считаются от target. Проверка «candidate —
потомок `snapshot_commit`» для такого brief не действует. `report submit` отклоняет с remedy
пропущенный, повторный и неизвестный коммит прежнего candidate, копию, совпадающую со своим
оригиналом (merge target вместо rebase), пустую причину `dropped`, коммит, который dispatch не
создал, и `rebased_from`/`dropped` в отчёте brief без target.

`report submit`, `batch decision-packet` и решение `accept`/`override-warning` возвращают
`rebase_check`: `rebase_target_commit`, `previous_base_commit`, пары `rebased` с `patch_id_match`
(сравнение `git patch-id --stable`), `dropped` и `patch_id_mismatches`. Расхождение patch-id
означает конфликт, разрешённый с изменениями: отчёт не отклоняется и не теряет clean-статус, а пара
показывается для delta-review. Accept такого отчёта закрепляет `integration_base_commit` = target,
и так же действует accept более позднего retry, чей candidate уже стоит на этом target. Retry ещё не
принятого rebase-отчёта, как и его `tooling-retry`, считает от target и `changed_files`, и коммиты
`carried_item_closure`. Если tip уйдёт ещё раз, review и QA проверяют закреплённый candidate, а ветку
обновляет `integration refresh` при подготовке PR (ADR 0014).

Delta-review после fix-forward (#625) — тоже не новый переход и не новое решение. Coordinator сам
выбирает объём следующего code-review в `dispatch propose` и `dispatch create --role code-review`
без `--delta-review-of`. Coordinator делает выбор, когда выполнены все условия: candidate —
`commit_sha` последнего принятого developer work report batch; этот report — developer-retry; его
цепочку подряд повторённых попыток developer-retry открыл маршрут `fix-forward` или
`rebase-fix-forward`; последний code-review перед цепочкой завершил отчёт на `snapshot_commit` её
первой попытки и был принят или повторён в developer. Этот code-review — прежний review. Brief
получает поле `delta_review_scope`: `mode` (`delta` или
`full`), `route`, `prior_review` (dispatch ID, `report_sha256`, проверенный candidate,
`review_base`, `risk_assessment_id`), `developer_dispatch_id`, `delta_base`, `delta_commits`,
`reviewed_copies`, `closure` (`carried_item_closure` принятого отчёта) и `escalations`. Transition
связывает раздел полем `delta_review_sha256`. `dispatch propose` показывает его как
`delta_review_scope`. У остальных brief поле равно `null`, в том числе при явном
`--delta-review-of`: test-only delta-review работает как раньше.

Delta — это `git diff <delta_base> <candidate_commit>`. Без rebase `delta_base` — candidate прежнего
review, а после rebase в цепочке берутся коммиты после последнего rebase target. Ведущие
перенесённые копии считаются проверенными (`reviewed_copies`), если по парам `rebased_from` из
`rebase_check` отчётов цепочки они ведут к коммиту из диапазона прежнего review и на каждом шаге
совпадает `git patch-id`; `delta_base` — родитель первого другого коммита. Coordinator выбирает режим
`full` автоматически, без ручного выбора, если найдена хотя бы одна эскалация `{reason, evidence}`:

- `new-risk-trigger` — триггер risk assessment candidate (вместе с триггерами developer) или
  коммитов и файлов delta, которого не было в assessment прежнего review;
- `file-outside-carried-items` — файл delta вне файлов перенесённых пунктов developer-retry (для
  `review-finding` это `review_scope` его review);
- `patch-id-mismatch` — пара из `patch_id_mismatches` любого отчёта цепочки;
- `dropped-commit` — запись `dropped` из `rebase_check` любого отчёта цепочки: candidate потерял
  изменение, которое проверял прежний review, а delta оставшихся коммитов этого не показывает;
- `no-new-commits` — новых коммитов для проверки нет.

В этом случае brief — обычный полный review, а раздел остаётся audit evidence. В режиме `delta`
`carried_items` brief дополнительно несёт `review-finding` и `incomplete-item` для developer из
brief принятого developer-retry. Reviewer проверяет обе оси только на delta, а для остального
candidate опирается на отчёт прежнего review; каждый пункт он учитывает в `review.carried_items`, и
пропуск, `open` или `unverified` — это carried gap. `review_scope` и `review.scope` остаются
полными. Accept любого review, delta или полного, ведёт в `qa`, а clean-room QA идёт на новом SHA с
полными `verification_commands`. Retry delta-review передаёт следующему developer-retry
перенесённые пункты, которые review не отметил `closed`, а собственные findings этого review
получают следующие номера `review-finding-N`.

Code-review `blocker` никогда не принимается. Пока `retry_policy.max_developer_retries` ещё допускает
developer retry, для него доступны `retry` или `abandon`. После исчерпания budget `retry`
отклоняется, а blocker закрывается через `block`, `fail` или `abandon`. Затем работа разбивается
или перепланируется в новом batch. `tooling-retry` не расходует этот budget и не отклоняется при его
исчерпании. Каждый такой retry по-прежнему решает человек, а серию ограничивает
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
закрывает issue и не создаёт PR. Убирается только то, что не относится к evidence: staged-копии
report в agent inbox и записи QA-очереди dispatch, которые уже не запустятся (живой QA lease
по-прежнему снимает только `qa clear-stale-lease`). Batch записывает `abandoned.last_accepted` —
последний принятый этап и его candidate. От него можно создать свежий batch на той же ветке и том
же candidate. `abandon` никогда не служит автоматическим fallback для `block`, `fail` или `retry`.
Команда `batch abandon` для batch, у которого не будет ни одного report, остаётся прежней. Она
завершает такой batch в `failed`.

#### Замещающий batch: `batch create --supersedes`

Если batch закрыт решением `batch decide --decision abandon` после тупика (например, blocker
code-review при исчерпанном бюджете retry), работу продолжает замещающий batch. Он создаётся для
того же ticket и той же issue-ветки и начинает с `abandoned.last_accepted`, а не с нового architect
и ручного cherry-pick принятых коммитов:

```bash
python .harness/orchestration/coordinator.py --repo . batch create \
  --ticket '#443' --branch feature/issue-443-<slug> --worktree <тот же worktree> \
  --allowed-path '<scope>' --definition-of-done '<item>' --prohibited-change '<rule>' \
  --supersedes <abandoned-batch-id> --approved-by 'имя утверждающего' \
  --approved-at 2026-09-20T09:00:00Z
```

`--supersedes` требует approval человека. `--approved-by` и `--approved-at` проверяются так же, как
у `batch abandon`: TTL approval и подтверждение в терминале при `human_approval_gate: tty`.
Approver с префиксом `policy:` отклоняется. Флаги approval без `--supersedes` тоже отклоняются:
обычный batch утверждается через `batch approve`. Замещающий batch после создания тоже проходит
`batch approve`. Команда называет remedy и ничего не пишет в таких случаях: источника нет в текущем
поколении ledger; его состояние не `abandoned`; `abandoned.last_accepted` равен `null`; ticket или
issue-ветка отличаются; последний принятый candidate не резолвится в коммит. Batch, закрытый
`batch abandon`, остаётся в `failed` без `last_accepted`. При `null` ничего не было принято. В обоих
случаях нужен обычный batch. Исключение — источник с `null`, который сам замещал batch. Тогда
remedy предлагает снова передать в `--supersedes` тот batch (`supersedes.batch_id` источника), чей
`last_accepted` по-прежнему последний. Abandoned batch не держит ticket, ветку и worktree. Но
другой незавершённый batch с тем же ticket, веткой или worktree по-прежнему блокирует создание.

Новый batch и его immutable plan несут одну и ту же неизменяемую ссылку `supersedes`: `batch_id`,
`approved_by`, `approved_at`, копию `last_accepted`, `definition_of_done_matches`, `architect`,
`start_commit` и `rebase_target_commit`. Batch, чья ссылка разошлась с plan, не проходит проверку
целостности. В `coordinator_decisions` нового batch одна запись `supersede`. Её routing record
маршрута `supersede` называет `previous_role`, `next_role`, `next_action`, `candidate_commit`
(`start_commit`), `rebase_target_commit`, `superseded_batch_id`, `rationale` и `decided_at`.

Что переносится:

- при том же Definition of Done (списки совпадают поэлементно) — принятый architect источника по
  ссылке `supersedes.architect` (`batch_id`, `dispatch_id`, `report`, `report_sha256`,
  `commit_plan_sha256`) и commit plan, закреплённый при его accept через `--commit-plan-file`
  (#478). Если источник сам перенёс architect, передаётся та же ссылка. Batch начинает с
  `next_action: developer`: developer dispatch создаётся сразу, а architect dispatch не допускается.
  При другом Definition of Done не переносится ничего, и стадия architect проходит заново;
- `start_commit` — candidate из `last_accepted` (`null`, если принят только architect). У
  замещающего batch без принятого developer этот candidate — его собственный `start_commit`.
  Поэтому следующий замещающий batch в цепочке не теряет принятый candidate. Первый developer
  dispatch берёт его как `snapshot_commit`. Brief, preflight и проверка свежести Context Package
  видят один и тот же SHA. Если `start_commit` — потомок integration base, закреплённой при create,
  это обычный initial developer: его `commit_map` покрывает все коммиты после base, включая
  коммиты `start_commit`;
- `rebase_target_commit` — эта base, если `start_commit` её не содержит. Тогда первый developer —
  developer-retry маршрута rebase внутри retry (#504). Он перебазирует коммиты на target, исправляет
  поверх и сдаёт пары `rebased_from`. `report submit` возвращает `rebase_check`. Такой dispatch
  требует явного approval с transition digest при любой `approval_policy`, кроме `auto`. Target
  остаётся в brief, пока продолжаемый snapshot его не содержит. Так бывает, например, после retry
  ещё не перебазированного отчёта.

Что не переносится никогда: risk assessment, code-review, QA, carried items, candidate registrations
и решения оператора. Они остаются evidence abandoned batch и доступны только по
`supersedes.batch_id`. Risk assessment, review и QA проходят заново на новом candidate. Запись
`supersede` — не retry и не расходует `retry_policy.max_developer_retries`: у замещающего batch
свой бюджет. Abandoned batch только читается. Worktree должен стоять на `start_commit`. Если
тупиковая попытка оставила выше непринятые коммиты, runtime attestation отклонит developer с
remedy. Вернуть ветку назад решает человек. Coordinator ищет источник только в текущем поколении
ledger.

#### Поле `route`: routing record, decision packet и audit

Каждое решение `retry` и `abandon` в `batch decide` записывает выбранный маршрут восстановления в
`routing.route` — одно значение закрытого набора `RECOVERY_ROUTES`: `developer-retry`,
`same-candidate-rerun` (новый code-review, qa или publish на том же SHA), `verification`,
`architect-retry` и `abandon`. Остальные значения `batch decide` либо записывает при особых
условиях, либо не записывает вовсе:

- `report-completion` `batch decide` не записывает. Его называет `completion` у `report submit`,
  когда policy-цепочка после записанного report остановилась (см. шаг 4 выше и «Занятый ledger»
  ниже).
- `carry-over` записывают `accept` или `override-warning` с `--findings-file` и `batch carry-over`
  (см. шаг 4). Это routing record с `previous_role: developer`, `next_role` и `next_action`
  `code-review`, `candidate_commit`, `carried_item_ids` и `rationale`, без `reason_category` и
  `decided_at`; к `next_action` он не применяется, а следующий шаг идёт через risk assessment, как
  при любом accept developer. Тот же маршрут записывает `accept` или `override-warning` read-only
  отчёта с `--carry-incomplete` (см. шаг 4): тогда `previous_role` — стадия отчёта, а `next_role` и
  `next_action` — обычный следующий шаг её accept.
- `tooling-retry` записывает `retry` с категорией `tooling` (см. выше), а также `retry --narrowed`,
  если пункт несёт `tooling_blocker`.
- `bypass-rerun` записывает `retry` с категорией `block-bypass` (см. выше).
- `narrowed-retry` записывает `retry --narrowed` по невыполненным пунктам read-only отчёта (см.
  шаг 4); его routing record дополнительно называет `carried_item_ids`.
- `fix-forward` записывает `retry`, который ведёт в `developer-retry` с непустым закрытым списком
  перенесённых пунктов (см. выше).
- `rebase-fix-forward` записывает `retry`, который ведёт в `developer-retry`, пока integration base
  ушла вперёд (см. выше); его routing record дополнительно называет `rebase_target_commit` и
  `integration_base_commit`.
- `supersede` `batch decide` не записывает: его записывает `batch create --supersedes` в
  `coordinator_decisions` нового batch (см. «Замещающий batch» выше).

Каждый routing record с `next_action: developer-retry` (`developer-retry`, `fix-forward`,
`rebase-fix-forward` и `tooling-retry` developer) дополнительно называет `retry_item_ids`. Маршрут
ставится там же, где `next_action`, по тем же структурированным данным и никогда по свободному
тексту. У `abandon` routing record той же формы, но `reason_category`, `next_role`, `next_action` и
`candidate_commit` равны `null`, а `rationale` содержит только структурные факты (`--reason`
остаётся в `note`). Нормативная таблица «ситуация → маршрут → кто утверждает → evidence» — раздел
«Recovery route table» в `.harness/orchestration/playbook.md`.

`batch decision-packet` показывает маршрут до записи решения: поле `route_preview` содержит
`retry` — routing record, вычисленный так же, как в `batch decide` (без `decided_at`), и `abandon` —
`{"route": "abandon"}`. Packet принимает те же `--reason-category`, `--retry-role developer` и
`--narrowed`, что и `batch decide`, и ничего не пишет. Для уже решённого report и для пакета
следующего dispatch `route_preview` равен `null`:

```bash
python .harness/orchestration/coordinator.py --repo . batch decision-packet \
  --batch <batch-id> --reason-category verification-infrastructure
```

Preview не проверяет `retry_policy.max_developer_retries`. При исчерпанном бюджете он по-прежнему
показывает маршрут `developer-retry`, `fix-forward` или `rebase-fix-forward`, а
`batch decide --decision retry` такое решение отклоняет. Как и `batch decide`, preview делает fetch
`origin/<integration_ref>` и потому показывает предложенный `rebase_target_commit`.
Если маршрут retry вычислить нельзя (например, упало настроенное расширение retry reason
classifier), packet всё равно строится. Тогда `route_preview.retry` равен
`{"route": null, "refused": ..., "remedy": ...}` с той ошибкой, которой откажет
`batch decide --decision retry`. С `--findings-file <path>` packet добавляет
`route_preview["carry-over"]`. Это запись, которую сделает `batch decide --findings-file` на
ожидающем developer report, а без такого report — `batch carry-over`. Либо это отказ той же формы
`{"route": null, "refused": ..., "remedy": ...}`. Без флага `route_preview` не меняется.

Каждое решение `batch decide` хранит в transition audit record batch деталь `decision`:
`dispatch_id`, `decision`, `route` (`carry-over` у `accept` и `override-warning` с
`--findings-file` или `--carry-incomplete`, `null` у решения без маршрута — остальных `accept`,
`override-warning`, `block`, `fail`), `evidence` (`dispatch_id`, путь `report` и `report_sha256`
immutable report), `approver` и `approved_at`. `approver` —
`{"kind": "policy", "name": "low_risk" | "milestone" | "auto"}` для policy auto-accept или
`{"kind": "human", "name": <--approved-by>}` для явного решения. Решение `batch carry-over` пишет ту
же деталь с `route: "carry-over"` и `{"kind": "policy", "name": "carry-over"}`.
`batch create --supersedes` пишет деталь с `"dispatch_id": null`, `decision` и `route` `supersede`,
ссылкой на abandoned batch в `evidence` (`batch_id`, `last_accepted`, `architect`) и
`{"kind": "human", ...}`. Вид определяется путём, которым решение утверждено, а не строкой имени.
Деталь входит в ту же audit-запись и ту же контрольную сумму, что и переход batch. Маршрут вне
`RECOVERY_ROUTES` отклоняется с remedy при записи и при чтении batch.

### Автоматический путь `approval_policy: auto`

Политика `auto` действует, пока конфигурация проекта и план batch выбирают `auto` и в batch нет
`auto_stop`. Сессия не передаёт `--approved-by` и `--approved-at`. Политика согласует:

- `batch approve`: `coordinator_approval.approved_by` равен `policy:auto`;
- `dispatch create`, включая вехи `publish`, `risk-trigger`, `risk-reassessment-required`,
  `bypass-rerun`, `rebase-fix-forward` и `rebase-target`;
- `dispatch resume --trigger` для планового продолжения, кроме `human-decision`;
- `batch carry-over`: решение пишется как `policy:auto`, а не `policy:carry-over`.

Явный `--approved-by` при `auto` остаётся approval человека. Каждое согласование политики — одна
хешируемая запись в `batch.auto_decisions` (`sequence`, `kind`, `dispatch_id`, `approved_by`,
`approved_at`, `rationale`, `evidence`). Валидация ledger пересчитывает `record_sha256` и сверяет
запись с согласованием, которое она называет.

Чистый отчёт по-прежнему принимает цепочка `report submit` или `qa run`. Для остальных отчётов,
включая отчёт publish, сессия запускает:

```bash
python .harness/orchestration/coordinator.py --repo . batch auto-decide --batch <batch-id>
```

Команда вычисляет решение из фактов ledger, проводит его через проверки `batch decide` и пишет как
`policy:auto` с маршрутом, причиной и evidence. Сессия передаёт только входы, которые требуют
суждения:

- `--commit-plan-file` — план architect-а. Политика закрепляет план, если он лежит внутри
  `allowed_paths` и покрывает все пункты DoD. Иначе путь останавливается. Если команда не может
  прочитать файл как JSON-объект, она отказывает и ничего не пишет.
- `--findings-file` — находки координатора для принятого отчёта developer-а.
- `--bug-ticket` — bug-тикет на инструмент для `tooling-retry`. Без него команда отказывает и
  ничего не пишет.
- `--block-bypass --note` — роль обошла блокировку hook-а или инструмента.

Политика принимает чистый отчёт и записывает его риски как `accepted_risks`. Отчёт review, в котором
ось Standards или Spec называет `blockers`, не считается чистым. Для остальных отчётов
политика выбирает `retry` по `route_preview`. Политика никогда не выбирает `override-warning`,
`block`, `fail` или `abandon`. Отчёт conflict-resolver-а вне автоматического пути: его решает человек.

Закрытый список остановок:

| `category` | `reason` |
| --- | --- |
| `integrity-failure` | `stale`, `model-mismatch`, `worktree-mismatch`, `harness-snapshot-changed`, `deterministic-gate-failed`, `ledger-validation-failed` |
| `budget-exhausted` | `retry_policy.max_developer_retries`, `continuation_policy.max_continuations`, `continuation_policy.max_rate_limit_resumes`, `attention_policy.max_infrastructure_retries`, `tooling-retry-repeated` |
| `no-automatic-route` | `unknown-reason`, `abandon-dead-end`, `supersede-dead-end` |

`batch auto-decide` и `batch auto-report` находят остановку в порядке: целостность, бюджет, маршрут.
Coordinator пишет остановку один раз в `batch.auto_stop` вместе с итоговым отчётом.
`ledger-validation-failed` команда только выводит: ledger недостоверен. Остановка не создаёт решения
и не ослабляет валидацию. После остановки каждый шаг batch требует `--approved-by`, а вернуться в
`auto` batch не может.

Coordinator пишет итоговый отчёт один раз в `batch.auto_report`: при accept отчёта publish или при
остановке.
Отчёт перечисляет решения `policy:auto`, принятые риски, findings, retry и потраченный бюджет,
покрытие DoD по commit plan, результаты review и QA и остановку. Команда выводит отчёт:

```bash
python .harness/orchestration/coordinator.py --repo . batch auto-report --batch <batch-id>
```

Без записанного отчёта команда записывает остановку, которую показывает ledger, или выводит
live-отчёт с `recorded: false`. Политика не открывает и не мерджит PR: PR открывается только после
явного подтверждения человека, а auto-merge запрещён.

### Approval, привязанный к digest перехода

`dispatch propose` принимает те же аргументы, что и `dispatch create`, но не пишет brief. Он
регистрирует shared Context Package, который brief закрепит. Затем он возвращает канонический
переход и его `transition_digest`. Это SHA-256 от batch ID, ID и роли предыдущего dispatch, reason
category, следующей роли/действия и purpose, candidate SHA, base SHA, review scope, verification
commands, Context Package ID и required gates. `dispatch create` с явным approval обязан получить
этот digest в `--transition-digest`. Coordinator пересчитывает переход из ledger и отклоняет любое
расхождение. Поэтому изменение scope, candidate, роли, verification command, reason category или
Context Package требует нового `propose` и нового approval. Coordinator сохраняет digest в approval
и в immutable brief вместе с самим переходом. Ledger-валидация пересчитывает его. Policy approval
(`milestone`, `low_risk`) выводится из создаваемого перехода и привязан к его собственному digest.
При `human_approval_gate: "tty"` запрос подтверждения показывает digest.

Если Repo Map у закрепляемого пакета имеет tier не `full`, результат `dispatch propose` дополнительно
содержит `context_package_quality_warning`: tier, причину деградации и parser provenance. Это
предупреждение для утверждающего человека, а не блокировка dispatch. Для `full` поле отсутствует.

Без `repo_map_policy.min_tier`/`min_tier_by_role` деградация Repo Map никогда не блокирует dispatch.
Остаётся только описанное выше необязывающее предупреждение. `repo_map_policy` может содержать
`min_tier` (общий минимум для репозитория) и/или `min_tier_by_role` (переопределение по роли, ключи
ровно `architect`/`developer`/`code-review`). Тогда порядок разрешения такой: override роли, затем
`min_tier`, затем отсутствие гейта. Если для роли задан минимум, а фактический tier закреплённого
Context Package хуже требуемого (порядок уровней по ADR 0008: `minimal` < `full`), coordinator
отклоняет и `dispatch propose`, и `dispatch create` ещё до какого-либо approval. Причина называет
роль, фактический и требуемый tier. Эта проверка выполняется в общем пути перед веткой
`propose`/`create`. Поэтому готовый `--transition-digest` её не обходит. Роль, не перечисленная в
`min_tier_by_role`, при отсутствии общего `min_tier` сохраняет поведение по умолчанию (без
блокировки).

Просроченное (`approval_ttl_seconds`) или отклонённое в терминале approval — fail-closed: coordinator
не повторяет вызов сам и не подставляет более старое approval.

### Idempotency read-only retry

Brief ролей `architect`, `code-review`, `qa` и publish хранит
`retry_idempotency_key = sha256(role + candidate SHA + base SHA + review scope + reason category +
digest verification commands)`. Пока в открытом batch есть активный (не решённый, не cancelled, не
abandoned) dispatch с тем же ключом, новый dispatch отклоняется. Завершённый retry не мешает новому
dispatch на том же candidate — он получает новый immutable ID. Смена candidate всегда меняет ключ.
Повторный review/QA никогда не правит прежние report и brief.

### Context pressure

`dispatch context-pressure --dispatch <id> --observed-tokens <N> --source probe|provider-usage|
runtime-adapter` пишет наблюдение `observed_tokens`, `context_limit`, `warning_threshold`, `level`
(`ok`/`warning`/`critical`) и `recorded_at`. Без `--observed-tokens` число берёт настроенный
`context_telemetry_provider`. Источник — только provider/runtime: self-report модели отклоняется.
Лимит и доля предупреждения — значения, зафиксированные в brief. Запись — чистое наблюдение: она не
меняет `next_action`, не создаёт retry и не снимает approval. При `critical` она сообщает обязанность
worker-а. Write-роль создаёт checkpoint на ближайшей зелёной границе TDD либо возвращает structured
blocker. Read-only роль возвращает blocker. Continuation создаётся только из checkpoint и только с
новым model self-report. Категория `context-pressure` у retry требует такой `critical`-записи для
отчитавшегося dispatch.

### Attention state

`needs_attention` — флаг batch, а не lifecycle-состояние: он не меняет `state`, `next_action`,
candidate и evidence, но запрещает создание следующего dispatch. Флаг описывают поля
`needs_attention`, `attention_reason`, `attention_since`, `last_safe_action` и
`recommended_human_action`. Причины:

| `attention_reason` | Когда |
| --- | --- |
| `unknown-reason` | retry с причиной `unknown` |
| `infrastructure-retry-repeated` | operational retry одного candidate больше `max_infrastructure_retries` |
| `tooling-retry-repeated` | третий подряд `tooling-retry` на одном candidate; снимается после исправления инструмента |
| `retry-queued-too-long` | принятый retry ждёт dispatch дольше `retry_queue_seconds` |
| `stale-evidence` | закреплённый в незавершённом dispatch Context Package расходится с base или текущим developer candidate (тем же, что выбирает `snapshot_commit`) |
| `stale-dispatch` | живой dispatch молчит дольше `stale_dispatch_seconds` |

Флаг ставят `batch decide --decision retry`, `dispatch wait` (событие `stale`) и
`batch attention check --batch <id>`. Последняя команда просто оценивает batch сейчас. Снимает флаг
только человек: `batch attention resolve --batch <id> --note '…' --approved-by … --approved-at …`
подтверждает открытые findings (то же событие повторно не поднимается) и пишет событие в
`attention_events`. Если настроен `human_notifier`, он вызывается при постановке флага. Сбой
адаптера записывается как `failed` и не отменяет флаг.

### Совместимость и миграция

Все новые поля batch и четыре поля brief (`transition`, `transition_digest`, `retry_idempotency_key`,
`orchestration_policy`; либо все, либо ни одного) необязательны. Записи, созданные раньше, остаются
валидными, версия ledger не меняется, `ledger migrate` не нужен. Новые записи проходят ту же
целостностную проверку (`context_pressure` с hash, форма attention-полей, согласованность brief).
Изменилось поведение CLI: `dispatch create` с `--approved-by` теперь требует `--transition-digest`.
Brief без поля `commit_plan_divergence` и batch без `commit_plan` тоже остаются валидными. Entry
плана без `covers` покрывает пункт DoD по своей позиции. Brief без `carried_items`, transition без
`carried_items_sha256`, batch без `carried_items` и отчёт code-review без `review.carried_items`
тоже валидны. Пустой канал ничего не добавляет в transition, поэтому прежние digest не меняются.
Отчёт без `incomplete_items` и brief без вида `incomplete-item` тоже валидны. Новый вид источника,
поле отчёта и значение маршрута `narrowed-retry` введены без смены версии ledger и без миграции.
Routing record без `retry_item_ids`, поле отчёта `carried_item_closure` и значение маршрута
`fix-forward` (#503) тоже введены без смены версии ledger и без миграции. Brief developer-retry по
решению, записанному до них, получает раздел без сверки с `retry_item_ids`. Completed-отчёт
developer-retry, записанный до #503 без `carried_item_closure` для brief с перенесёнными пунктами,
по-прежнему решается через `batch decide`. Каждый пункт получает статус `omitted` и входит в
`carried_items_gap`. Поэтому `accept` и policy auto-accept отклоняются, а `override-warning` требует
`--note`, отличный от `none`. `retry`, `block`, `fail` и `abandon` доступны. Новый такой отчёт
`report submit` отклоняет. Brief без `rebase_target_commit`, transition без `rebase_target_sha` и
значение маршрута `rebase-fix-forward` (#504) тоже введены без смены версии ledger и без миграции:
переход без target сохраняет прежний digest. Brief без `delta_review_scope` и transition без
`delta_review_sha256` (#625) тоже валидны и введены без смены версии ledger и без миграции: переход
code-review без раздела сохраняет прежний digest. Ссылка `supersedes` в batch и plan и значение
маршрута `supersede` (#506) тоже введены без смены версии ledger и без миграции: batch и plan без
ссылки читаются как прежде.

Поле `route` в routing record и деталь `decision` в transition audit record batch тоже
необязательны и введены без смены версии ledger (остаётся 3). Решение, записанное до них, читается
как есть и остаётся валидным. `ledger migrate` не добавляет и не выводит для него route. Route
пишет только новое решение `batch decide`. Записанный `route` вне закрытого набора маршрутов
отклоняется при чтении batch.

### Инвентарь и закрытие тупикового batch

Посмотреть, что вообще заведено и что не закрыто:

```bash
python .harness/orchestration/coordinator.py --repo . batch list --open
python .harness/orchestration/coordinator.py --repo . batch list --ticket '#123'
```

Обычный путь к терминальному состоянию — `batch decide`, но он требует ровно один отчёт,
ожидающий решения, а отчёт требует живой dispatch с подтверждённой моделью. Worker, умерший до
self-report, никогда не отчитается, и такой batch не закрыть ни `fail`, ни `block`. Для этого
случая есть отдельная команда:

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

Для незавершённого initial developer закрепите сохранённый HEAD через `--candidate-commit` при
следующих `dispatch preflight`, `dispatch propose` и утверждённом `dispatch create`. Это startup
snapshot, а не принятый completed candidate: risk assessment и последующие gates следуют за
принятием итогового report. Commit map initial work покрывает весь исходный план от batch base,
включая сохранённые коммиты до сбоя. Прежние риски остаются в report. Для `developer-retry`
сохраняется отдельный контракт: только новые коммиты после snapshot.

Batch закреплён за рантаймом, под которым его спланировали. `batch create` записывает хэш всего
пакета рантайма в `.harness/`: верхнеуровневых модулей и всех подпакетов (`orchestration/`,
`gate_runner/`, `context_builder/` и других). Кроме того, он сохраняет неизменяемый снимок этого
пакета в `.harness/orchestration/state/runtimes/`. Если рантайм после этого переустановили
(например, `harness update` с ветки другой задачи), команды этого batch (`--batch`, `--dispatch` или
`dispatch_id` в файле `--file` у `report submit`, `dispatch checkpoint` и `dispatch telemetry`)
автоматически выполняются кодом снимка. Описания ролей тоже берутся из снимка. Новые batch
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

Никогда не правьте файлы в `.harness/orchestration/state/` руками. Эти записи и есть
доказательство, ради которого существует весь маршрут. Если штатной команды для вашего случая нет —
это дефект инструмента, а не повод открыть редактор.

### Model self-report и dispatch watchdog

Отправленный dispatch не считается живым сам по себе. Первым действием после получения brief роль
подтверждает действительно активную модель:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch self-report \
  --dispatch <dispatch-id> --model <фактическая модель> \
  --worktree "$(git rev-parse --show-toplevel)"
```

Совпадение с `resolved_model` immutable brief переводит dispatch в `working`. Если включён
`worker_attestation_required`, coordinator также проверяет переданный Git top-level, issue branch
write-роли либо pinned SHA review-роли. Расхождение немедленно переводит dispatch в `blocked`,
помечает batch `blocked` и завершает команду ошибкой. После этого coordinator не принимает
`report submit` для такого dispatch. Починка — новый dispatch с новым brief, а не правка
отправленного. Без успешного self-report completion report не принимается вообще. Поэтому
подменённая или неверно настроенная модель видна сразу, а не после потраченного окна.

Для architect/developer `worker_attestation_required` также требует, чтобы Git-worktree HEAD в
момент первого `self-report` буквально совпадал с immutable `snapshot_commit` из brief. При
разрешённом `dispatch resume` write-роли свежий self-report проверяет точный `commit_sha`
последнего зарегистрированного checkpoint. Исходный brief и граница commit-plan evidence остаются
неизменными. Для architect, developer, verification и code-review `dispatch create` выбирает
`snapshot_commit` в таком порядке:

1. явный `--candidate-commit`;
2. `commit_sha` непринятого developer report, пока batch ждёт его `developer-retry`;
3. последний принятый developer candidate — например, `developer-retry` после code-review blocker,
   qa или publish (повтор на том же SHA developer dispatch не создаёт);
4. иначе `base_commit`.

`dispatch preflight` и проверка свежести Context Package используют то же правило. Retry продолжает
историю этого candidate и добавляет отдельные логические коммиты по immutable commit plan.
Coordinator не выполняет и не предлагает `git reset --soft`. Если HEAD worktree не совпадает с pinned
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
`risk assess --batch <batch-id> --candidate-commit <sha> --changed-file <path>...`. После неё нужны
новые `dispatch propose` и `create` (неотправленный brief сначала отменяется через
`dispatch cancel`). Проверка при `dispatch send` остаётся на месте.

Пока роль работает, она отбивает heartbeat, а coordinator-сессия опрашивает состояние. По умолчанию
dispatch допускает до часа тишины для долгой сборки или теста. Но immutable brief требует heartbeat
сразу после self-report и затем не реже раза в пять минут. Это сохраняет быстрый сигнал о живом
worker, и работающий developer не становится stale из-за одного долгого tool call:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch heartbeat --dispatch <dispatch-id>
python .harness/orchestration/coordinator.py --repo . dispatch status \
  --batch <batch-id> --stale-after 3600
```

`dispatch status` показывает для каждого dispatch роль, транспорт, `resolved_model`, результат
self-report, время последнего heartbeat, `silent_seconds` и признак `stale`. Это обобщение
QA-lease-expiry на любой dispatch, а не только на clean-room QA lane. Stale — блокер, который
coordinator выносит человеку, а сам по таймауту состояние не меняет.

`dispatch heartbeat` принимает необязательную пару `--context-tokens <N> --context-source probe`.
Это число токенов, измеренное coordinator-ом в live-пробе контекста (см. «Отчётность и мониторинг
токенов» ниже), а не self-report роли. Так сохраняется правило из начала документа («Токены — только
наблюдаемая provider- или runtime-telemetry», строки 27-29): сама роль это значение не поставляет.
Значение попадает в открытое поле `extra` статуса dispatch-а. Схема ledger не меняется, новое
событие в `dispatch wait` не вводится.

`dispatch status` дополнительно отдаёт для каждого dispatch последнюю запись `telemetry`
(`dispatch telemetry`, см. ниже) и `context_advisory`. Это чистое чтение, и отсутствие телеметрии
ошибкой не считается: `telemetry` и `context_advisory.observed` тогда равны `null`, а
`context_advisory.level` остаётся `"ok"`.

### Занятый ledger, `report complete` и `ledger release-lock`

Каждая команда coordinator-а берёт эксклюзивный lock ledger-а (`.coordinator.lock` в каталоге
state). Lock записывает владельца — `pid`, `host` и `acquired_at`. Держит lock тот, кто первым
эксклюзивно создал эту запись. Процесс получает отказ и не пишет в чужой lock, если его ещё пустой
каталог lock успели снять и занять снова. Пока lock держит другая операция, команда записи
завершается ошибкой `ledger is locked by another operation`. Её remedy предлагает повторить
команду и называет `ledger release-lock`, а не ручное удаление.

Опрос coordinator-а переживает занятый lock:

- `dispatch wait` считает занятый lock пропущенным опросом и опрашивает дальше до своего
  `--timeout`. Report возвращается, как только lock освобождён. Если lock занят до конца ожидания,
  результат — обычный `{"dispatch_id": ..., "event": "timeout"}`;
- `dispatch status` при занятом lock завершается с кодом 0 и отвечает структурно:
  `{"ledger_busy": true, "retry_after_seconds": ..., "lock": {...}, "remedy": ...}`. Здесь `lock`
  содержит владельца и `held_seconds`. Это не недоступность coordinator-а: повторите
  `dispatch status` через `retry_after_seconds`.

Терпимость касается только этих двух команд опроса и `report complete`. Ни одна запись не идёт без
lock. У остальных команд занятый lock остаётся ошибкой. `report complete` при занятом lock
останавливается на шаге, который не смог прочитать состояние. Она ничего не пишет и в remedy
называет себя для повтора.

Lock, который остаётся занятым, снимает только команда с проверкой владельца:

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

Читаемой записи владельца нет в двух случаях. Первый: lock взят старым runtime, и записи нет вовсе.
Второй: владелец умер, создав `owner.json`, но не записав его; запись пустая или нечитаемая. Возраст
нечитаемой записи считается от mtime `owner.json`, а без записи — от mtime каталога lock. Пока lock
моложе порога, владелец может ещё записывать себя, поэтому команда отказывает. Владелец, который за
`LEDGER_LOCK_STALE_SECONDS` так и не записал созданную запись, считается завершившимся.

Успех возвращает `{"released": true, "reason": ..., "lock": {...}}`, а если lock нет —
`{"released": false, "lock": null}`. Отказ — это ошибка с причиной, владельцем и `held_seconds`, и
lock при этом не трогается. Объект `lock` описывает снятый lock:

| Поле | Значение |
| --- | --- |
| `path` | каталог lock |
| `owner` | запись владельца (`pid`, `host`, `acquired_at`) или `null`, если читаемой записи нет |
| `owner_record` | `readable` — запись прочитана; `unreadable` — `owner.json` есть, но пустой или нечитаемый; `absent` — записи нет |
| `acquired_at` | `acquired_at` из записи, иначе mtime `owner.json` для нечитаемой записи или каталога lock |
| `held_seconds` | сколько секунд lock держится к моменту проверки |

Команда снимает только тот lock, который проверила: если владелец сменился во время снятия, она
отказывает и просит повторить. Запуски `ledger release-lock` не пересекаются, потому что их
сериализует файловая блокировка ОС (`.coordinator.lock.release` в каталоге state), которую система
снимает сама при выходе процесса. Второй параллельный запуск получает отказ
`another ledger release-lock is releasing the ledger lock` и повторяется после первого. Удалять lock
или другие файлы state вручную нельзя.

### Отчётность и мониторинг токенов: `dispatch telemetry`

```bash
python .harness/orchestration/coordinator.py --repo . dispatch telemetry --file telemetry.json
```

Команда записывает source-observed метрики (worker- или coordinator-сессии) в audit trail batch-а:
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `max_context_tokens`,
`tool_calls`, `tool_output_bytes`, `poll_turns`, `restart_reason`, `recorded_at`. Недостающее
provider-поле остаётся `null`, а не оценочным нулём. Команда **intentionally data-only**: она только
пишет запись телеметрии и не может изменить состояние роли, планирование, approvals или model
routing.

В **возвращаемом значении** (не в сохранённой ledger-записи) команда добавляет
`context_advisory: {"level": "ok"|"warn"|"over", "limit", "warn_at", "observed"}`. Это
advisory-оценка `max_context_tokens` против порога `adaptive_continuation_policy.context_limit` в
проектной `.harness/orchestration.json`. Долю порога для предупреждения задаёт
`adaptive_continuation_policy.context_warn_ratio` (доля от `context_limit`, по умолчанию `0.8`;
`warn_at = round(context_limit * context_warn_ratio)`). `level` — `"ok"`, пока `observed` (или его
отсутствие) ниже `warn_at`; `"warn"` — в диапазоне `[warn_at, context_limit)`; `"over"` — на
`context_limit` и выше. Это чистая оценка: она не триггерит checkpoint автоматически. Решение о
checkpoint остаётся за coordinator-ом, как и для любого другого сигнала, кроме auto-resume по 429
(см. the continuation section above). Baseline из `playbook.md` («Baseline metrics») тем же образом
остаётся ориентиром, а не скрытым лимитом.

### Checkpoint и новая worker session

Write-роль (developer, database-migrations, messaging-integration, conflict-resolver) может
растянуть один dispatch на несколько worker session, если весь TDD-цикл в одну сессию раздувает её
контекст. Read-only роль (architect, qa, code-review) — не может: попытка checkpoint для неё
отклоняется сразу.

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
подтверждённого self-report. Команда переводит dispatch-status в `checkpointed` и не трогает outcome
enum (`completed`/`blocked`/`failed`). Этот enum остаётся только у completion report.

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
новую сессию автоматически. Новое решение человека/coordinator-а не требуется. Любая другая
причина, включая отсутствующую или нераспознанную, трактуется как planned trigger. Это safe default
в сторону approval, а не от него:

- `--trigger` обязателен и должен быть одним из `context-limit`, `tdd-cycles`, `failure-log`,
  `vertical-slice`.
- для `context-limit`/`tdd-cycles`/`failure-log` `--measured-value` обязан быть не меньше
  своего порога `adaptive_continuation_policy` (`context_limit`/
  `tdd_cycle_count`/`failure_log_bytes`) из `.harness/orchestration.json`. Без явной конфигурации
  используются задокументированные значения по умолчанию (150000 / 3 / 20000), а не зашитые
  внутри порознь для каждого места.
- `--file` обязан содержать JSON с `dispatch_id`, `remaining_definition_of_done`, `risks` и
  `dependencies`, буквально совпадающими с последним checkpoint (первые два поля) и с dispatch
  (`dependencies`). `blockers` в сравнение не входит: их формулировка может измениться между
  сессиями без реального дрейфа scope/DoD/risks/dependencies. Расхождение — сигнал, что они реально
  изменились. Тогда coordinator обязан закрыть текущий dispatch и открыть новый через обычный
  approval, а не резюмировать этот.
- авторизация записывается тем же `coordinator_decisions`, что accept/retry/block/fail/abandon/
  cancel — новый тип записи не вводится.

`resume` принимает только `checkpointed` dispatch, возвращает его в `dispatched` и отбрасывает
предыдущий model self-report. Это значит, что новая сессия обязана заново пройти `dispatch
self-report` и `dispatch heartbeat` — ровно так же, как при первом contact, — прежде чем следующий
checkpoint или completion report будет принят. Круг замыкается тем же dispatch ID: checkpoint →
`dispatch resume` → новая self-report/heartbeat → в итоге один completion report.

Обновление installed runtime не меняет runtime snapshot активного batch. Если старый snapshot
содержит ошибку continuation, сохраните checkpoint и Git history, затем выберите штатный recovery
на исправленном runtime с новым approval. Новый batch должен закрепить исходную integration base,
полный DoD/commit plan и сохранённый writer HEAD. Review и QA проверяют весь итоговый candidate.
Записанные briefs, reports и runtime snapshots старого batch остаются audit evidence.

### Clean-room QA lane

Coordinator сам выполняет одобренный `qa` dispatch в отдельном temporary Git worktree,
отсоединённом ровно на `candidate_commit`. Команды берутся буквально из `verification_commands`
immutable brief, а несовпадение HEAD или грязный worktree останавливает проверку. Запуск не
передают runtime adapter:

```bash
python .harness/orchestration/coordinator.py --repo . qa run \
  --dispatch <qa-dispatch-id> --lease-seconds 1800
```

В репозитории существует одна FIFO-полоса тяжёлых проверок. Если она занята, команда сохраняет
запрос и возвращает `state: queued` с позицией, а повторный вызов для того же dispatch запустит его,
только когда он станет первым. Состояние и текущий owner видны без запуска gate:

```bash
python .harness/orchestration/coordinator.py --repo . qa status
```

Lease содержит dispatch ID, host, PID, время взятия и expiry. Истёкший lease **не** снимается
автоматически: coordinator сперва сверяет owner, затем записывает собственное решение с теми же
host/PID/expiry и только после этого удаляет stale request:

```bash
python .harness/orchestration/coordinator.py --repo . qa clear-stale-lease \
  --expected-host <host> --expected-pid <pid> --expected-expiry <ISO-8601> \
  --approved-by 'имя coordinator-а' --approved-at 2026-09-10T12:00:00Z \
  --reason 'проверено, что владелец больше не выполняется'
```

#### Подготовка окружения и категории провала

Необязательный `qa_preparation` в `.harness/orchestration.json` задаёт команды подготовки окружения
(например, `uv sync --locked`). Необязательные `qa_environment_probes` (например, проверка
доступности реестра пакетов) и `qa_project_file_checks` (например, `uv lock --check`) задают
независимые факты. Их команды выполняются в том же checkout только после упавшей подготовки.
`qa run` выполняет их и затем `verification_commands` в одном чистом checkout точного
`candidate_commit`. Он останавливается на первой упавшей команде. Без `qa_preparation`
(конфигурации только с `verification_commands`) поведение и форма report не меняются.

Report и артефакт с подготовкой несут `qa_stages`. На каждую выполненную команду там есть стадия
(`preparation`, `environment-probe`, `project-file-check` или `gate`), точная команда, результат,
exit code и санитизированная диагностика упавшей команды. Также они несут `failed_stage` и
`code_checks_started` (`started`, `not_started` или `unknown`). Записи сверяются с неизменяемым
артефактом: команды и exit codes должны совпасть с его блоками. Если подготовка упала, команды gate
в `checks_run` записываются как `not-run`. Это не выдуманная упавшая проверка кода.

Причину провала подготовки подтверждают независимые факты: ни exit code, ни ключевое слово в логе
не решают в одиночку, а сама стадия подготовки инфраструктурную причину не доказывает. Сигнатуры
лога лишь подкрепляют диагноз, и без настроенных проб и проверок файлов проекта инфраструктурная
причина не подтверждается. Тогда провал требует triage:

| Диагноз | Подтверждение | Report | Маршрут `batch decide --decision retry` |
| --- | --- | --- | --- |
| `infrastructure` | упала проба окружения, а все проверки файлов проекта прошли (и их задано не меньше одной); в выводе нет сигнатуры дефекта проекта; код не проверялся | `blocked`, «код не проверен» | `same-candidate-rerun` без `--reason-category`: новый qa dispatch на тот же SHA, бюджет developer retry не тратится |
| `project-defect` | упала проверка файлов проекта (проба при этом не упала), либо сигнатура дефекта (например, несовместимый lock-файл), согласованная с отслеживаемым манифестом или lock-файлом, названным в выводе; проверки файлов проекта её не опровергают | `failed` | `developer-retry`, причина `code` |
| `unknown` | пробы и проверки не заданы, не подтверждают причину или противоречат друг другу (например, упали и проба, и проверка) | `blocked`, нужен triage | `retry` без `--reason-category` отклоняется; coordinator называет причину или блокирует batch |

Провал самого gate остаётся обычным провалом проверок кода и идёт по прежнему маршруту
`developer-retry`.

**Восстановление на том же SHA.** После подтверждения готовности окружения coordinator выполняет
`batch decide --decision retry`. Затем он создаёт новый qa dispatch для того же `candidate_commit` с
теми же командами и границами. Прежние brief, report, артефакт и approval сохраняются как audit
evidence. Сбой подготовки или записи report не оставляет lease, запись очереди или состояние
`working`: тот же approved dispatch можно запустить заново без правки ledger и без нового batch.

#### Ограниченные инфраструктурные повторы

Проект может отдельно разрешить policy-повтор подтверждённого инфраструктурного отказа:

```json
{
  "infrastructure_retry_policy": {"enabled": true},
  "attention_policy": {"max_infrastructure_retries": 2}
}
```

Без opt-in действуют ручные gates. Политика не выдаёт права платформы, не подменяет native
approval и не принимает failed QA. Бюджет использует существующий
`attention_policy.max_infrastructure_retries`: default — 2, а `0` запрещает повторы.
Developer retry budget не расходуется. Opt-in, бюджет и команды preparation/probe/project-file-check
сохраняются в `orchestration_policy.infrastructure_retry` и связаны с approval digest.
Исторические briefs без этой секции остаются в ручном режиме. Правка live config не включает
повтор для старого approval и не меняет команды нового уже утверждённого brief.

После QA preparation failure policy продолжает только маршрут `same-candidate-rerun` с
подтверждённым `infrastructure`, `code_checks_started: not_started` и неизменным SHA. Перед
решением coordinator проверяет доступ и заново выполняет закреплённые независимые пробы окружения и
проверки файлов проекта в чистом checkout; если среда ещё не готова, нового dispatch нет. После
устранения причины в прежних границах выполните:

```bash
python .harness/orchestration/coordinator.py --repo . report complete --dispatch <qa-dispatch-id>
```

Каждый состоявшийся повтор получает новый immutable dispatch и источник решения
`policy:infrastructure-retry`, а прежние brief/report/artifact сохраняются. Повтор `report complete`
возвращает уже созданный dispatch, а прерывание после решения докатывает недостающий шаг. Команда
не запускает worker и не открывает PR.

Отказ доступа до запуска QA или publish не создаёт report о проверке кода. Для opt-in dispatch
coordinator сохраняет structured access attempt и checksum. Когда тот же ресурс стал доступен,
запросите проверку и новое policy-утверждённое задание:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch retry-infrastructure --dispatch <dispatch-id>
```

Этот путь разрешён только для отказа, подтверждённого probe (`denied`), при свежем подтверждении
готовности. Старый unsent brief отменяется с audit причины, а новый сохраняет этап, SHA, команды,
runtime/model/effort, scope и доступ; повтор команды возвращает прежний результат. Publish remote
тоже должен остаться тем же. Затем запускайте `qa run` или `dispatch publish` с новым ID.
Неподтверждённый сетевой отказ (`unverified`), неподдержанный режим (`unsupported`), отказ самим
планом и произвольная ошибка Git требуют ручного разбора. Git-операции без утверждённого dispatch
сохраняют ручной маршрут: opt-in не разрешает произвольную команду Git.

Смена candidate, команд или доступа, неизвестная причина и исчерпание бюджета останавливают
policy и поднимают `needs_attention`. Снимает attention человек. Это не увеличивает бюджет
и не разрешает изменённый контракт. Новые требования проходят обычные proposal и approval.
Не редактируйте ledger и не переписывайте candidate ради инфраструктурного восстановления.

**Native smoke.** Проверка поддержки требует реальной coding-среды. Создайте отдельный smoke-проект,
локальный bare remote и новый пустой cache внутри разрешённого shared storage. Зафиксируйте полный
SHA harness и candidate, чистоту checkout и точные preparation/gate команды. Через публичный CLI
пройдите batch/dispatch approval, native handoff роли, preparation/clean-room QA и publish
accepted SHA, а затем подтвердите отказ доступа, отсутствие retry до готовности, ограниченный
повтор и сохранение evidence. Dynamic IDs и digest берите из JSON команд. Native approval принимает
coding-runtime согласно действующей политике пользователя, и orchestration policy её не заменяет.
Не меняйте machine/global профили, не очищайте пользовательский cache, не создавайте PR и не
выполняйте merge. Внешний push требует разрешения на конкретный smoke remote, а push в локальный
bare remote проверяет Git/publish, но не внешний Git-доступ.

Первые шаги из source checkout (пути и actual model задаёт человек):

```bash
python harness/bin/harness.py init "$SMOKE_REPO" --capability backend-orchestration \
  --base-branch integration/smoke --language ru --tracker-type local \
  --qa-gate-command "$SMOKE_GATE"
python "$SMOKE_REPO/.harness/orchestration/coordinator.py" --repo "$SMOKE_REPO" batch create \
  --ticket local:619 --branch feature/issue-619-native-smoke --worktree "$SMOKE_REPO" \
  --integration-ref integration/smoke --allowed-path smoke.py --expected-file smoke.py \
  --expected-service smoke --expected-changed-lines 2 \
  --goal 'Change answer to 42 and verify the native smoke' \
  --definition-of-done 'answer returns 42; exact candidate passes QA and local publish' \
  --prohibited-change 'global config, other projects, secrets, PR, merge'
```

Перед этим локальный `origin` должен содержать `integration/smoke`, а checkout — чистую issue branch.
Создайте tracked `smoke.py`, manifest/lock и реальные project-owned команды подготовки, независимой
пробы сети/cache и offline проверки файлов. Настройте opt-in до proposal, создайте отдельный cache и
зафиксируйте его пустоту. Для QA используется, например,
`UV_CACHE_DIR=<новый-cache> UV_PROJECT_ENVIRONMENT=.harness/.venv uv sync --locked`, а developer
использует другой cache. Runner выбирает Python из `.harness/.venv` текущего clean-room checkout.
Команда gate должна проверять фактическое изменение в `smoke.py`, а также расположение `sys.prefix`
и импортируемого пакета в этом checkout. Сохраните фактический `executed_command` из stage evidence.

Выберите transport до создания batch. Для отдельной native CLI-сессии задайте
`assignment_plans.<role>.transport: "external"` и реальные `runtimes`, `provider_profiles` и команды
проверок. В authored assignment plan обязательны architect, developer, code-review и qa, и health
проверяет этот набор. Подготовьте project-owned adapter, который получает `dispatch --repo --brief` и
действительно запускает coding CLI в approved worktree с моделью и effort из brief; он
возвращается после запуска worker и не управляет ledger. Для `in-process` роль запускают как
субагента текущей coordinator-сессии, и отдельную CLI-сессию таким субагентом считать нельзя.

Из JSON `batch create` возьмите `batch_id`, после чего человек или явно назначенный им coordinator
выполняет `batch approve` с `--approved-by` и фактическим `--approved-at`. В том же smoke checkout:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch propose \
  --batch "$BATCH" --role architect --runtime "$RUNTIME" --model "$MODEL" --effort "$EFFORT"
python .harness/orchestration/coordinator.py --repo . dispatch create \
  --batch "$BATCH" --role architect --runtime "$RUNTIME" --model "$MODEL" --effort "$EFFORT" \
  --transition-digest "$DIGEST" --approved-by "$APPROVER" --approved-at "$APPROVED_AT"
python .harness/orchestration/coordinator.py --repo . dispatch send --dispatch "$DISPATCH"
```

Для выбранного `external` последняя команда содержит реальный adapter:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch send \
  --dispatch "$DISPATCH" --adapter "$SMOKE_REPO/scripts/native_codex_adapter.py"
```

`DIGEST` берётся из просмотренного proposal, `DISPATCH` — из create. Handoff выводит путь brief,
report staging path и ожидаемую модель. При `external` роль запускает adapter, при `in-process` —
coordinator в своей coding-сессии. Роль выполняет
`dispatch self-report --dispatch "$DISPATCH" --model "$ACTUAL_MODEL" --worktree "$SMOKE_REPO"`,
затем `dispatch heartbeat --dispatch "$DISPATCH"` и настоящий `report submit --file <report>`.
Architect не меняет код и не запускает full gate. После проверки report coordinator сохраняет
его план как `{"commit_plan": [...]}` в `$COMMIT_PLAN` и выполняет
`batch decide --batch "$BATCH" --decision accept --commit-plan-file "$COMMIT_PLAN" --approved-by "$APPROVER" --approved-at "$APPROVED_AT"`.
Повторите proposal/create/send для developer: его commit/report задают полный candidate SHA.
`risk assess --batch "$BATCH" --candidate-commit "$SHA" --changed-file smoke.py` определяет,
нужен ли review, и требуемый review пропустить нельзя; его send использует checkout точного SHA.

Для нового qa proposal/create добавьте `--candidate-commit "$SHA"` и используйте `qa run`,
не `dispatch send`. Устройте отказ записи только в созданном cache (в POSIX fixture —
`chmod u-w "$SMOKE_CACHE"`). Сохраните `qa_stages` и отказ `report complete` до готовности.
Восстановите `chmod u+w "$SMOKE_CACHE"`. `report complete` должен вернуть новый ID на тот же SHA,
а replay — тот же ID. Запустите `qa run` нового ID и вручную примите только успешный report.
Publish proposal/create задаёт `--role developer --purpose publish --candidate-commit "$SHA"`.
Для POSIX metadata fixture ограничьте запись только в smoke `.git`. Сохраните отказ
`dispatch publish --dispatch "$PUBLISH" --remote origin` и отказ `dispatch retry-infrastructure`
до восстановления. После восстановления получите новый ID и повторите publish в тот же `origin`.
`git ls-remote --heads origin refs/heads/feature/issue-619-native-smoke` должен вернуть ровно `$SHA`.
При прерывании восстановите write bit только своего fixture. Неполный или иной диагноз — blocker,
а не разрешение продолжать сценарий. Авторизованный inherit coordinator операций не доказывает
применение authored прав worker. Запускайте только режим, подтверждённый действующим extension.

В evidence сохраняйте runtime, версию, transport и режим, полные SHA, native параметры и proof
фактического доступа, команды с exit codes, readiness, dispatch IDs и checksum, предварительно
удалив secrets. Health-конфиг и checkout attestation сетевые права не доказывают.

Проверки этой реализации покрывают настоящие Git/ledger, preparation/gate, same-SHA retry,
budget, attention, metadata refusal и локальный publish. Это контролируемые проверки: они не
доказывают native handoff и всю runtime-матрицу. Для native handoff с authored планом доступа нужна
реализация `extensions.runtime_access`, которая наблюдает и применяет фактический доступ worker;
`none` такого proof не даёт, а явно выбранный непроверенный режим блокируется. `legacy-inherit`
сохраняет прежнюю передачу задания, но права worker не подтверждает.

Проверенный 2026-10-08 native smoke установлен из harness
`cca45e172c7b3685c012e7e6c077356d6cbd6722`: Linux, Codex CLI 0.160.1, настоящий project adapter и
две отдельные роли `architect`/`developer`. Candidate `1dba1785bc809971981129c747e85bffd9b29c51`
прошёл preparation и gate в новом clean-room checkout с пустым QA cache, свежей `.harness/.venv` и
скачанными зависимостями, после чего был опубликован в local bare remote. Отказ cache и отказ записи
`.git` остановили продолжение до readiness. Восстановление израсходовало два общих infrastructure
retry, replay сохранил successor IDs, а прежние records сохранили checksum. Batch завершён через
публичный CLI. Это подтверждает только следующие границы:

| Runtime / transport / режим | Результат native smoke | Граница evidence |
| --- | --- | --- |
| Codex CLI 0.160.1 / `external` / worker `legacy-inherit` | PASS: реальные роли, self-report, heartbeat, commit и report | Native CLI работал с `workspace-write`; authored worker access plan не проверен |
| Coordinator / `inherit` / QA и publish | PASS: fresh dependencies, exact-SHA gate, cache/Git denial, readiness, budget и replay | Фактические операции в одной разрешённой среде coordinator; publish только в local bare remote |
| Worker с authored `inherit`, `sandbox` или `unsandboxed` / `runtime_access: none` | Blocker | Нужен действующий extension с proof наблюдения и применения плана; attestation его не заменяет |
| `in-process`, Claude и другие runtime, внешний Git push | Не проверено | Для каждого сочетания нужен отдельный фактический запуск |

CLI flags, host execution coordinator и POSIX `chmod` fixture не доказывают всю permission-матрицу.
Для другого runtime, transport или authored режима поддержки требуют собственного native evidence.

После выполнения создаётся immutable completion report с командами, exit codes и кратким
санитизированным evidence. Полный санитизированный stdout/stderr сохраняется вне Git в
`.harness/orchestration/state/qa-artifacts/<sha256>.log`. Report ссылается на этот путь и checksum.
Провал gate остаётся QA finding и требует нового одобренного developer dispatch. Runner не правит
код и не перезапускает проверку самостоятельно.

После accepted QA evidence coordinator создаёт, но не запускает, publish dispatch для того же
candidate SHA. Этот SHA отправляет только developer publish, а ни QA, ни review, ни adapter не
создают и не мержат PR. После publish человек вручную запускает `/to-pull-requests <ticket>`: он
проверяет accepted QA evidence текущего SHA и ведёт обычный ручной PR workflow без повторного
тяжёлого gate. Хук `require-qa-gate.sh` принимает то же evidence сам, поэтому повторный gate и
маркер не нужны для чистого checkout с `HEAD`, равным accepted `candidate_commit`, и с `pass` по
каждой команде `qa_gate_commands`. Иначе остаётся прежний путь с маркером.

Обычная точка входа — `/implement <ticket>`. Эта сессия сама становится coordinator-ом и ведёт
описанный цикл. Она останавливается на пяти approval-гейтах (architect, developer, code-review, qa,
publish) и наблюдает за heartbeat каждого dispatch. Один тикет доводится до терминального состояния
batch до старта следующего. Ручной запуск по этому руководству остаётся полностью валидным. Для
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
группа `integration`. Ни одна из команд не пишет batch, plan, dispatch и reports. Git меняет
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
завершённый batch с accepted publish для пары ticket+branch. Опубликованный SHA он сверяет двумя
независимыми способами: по принятому отчёту publish (digest отчёта и brief) и по `ls-remote`
(ветка на remote — ровно candidate, integration ref — ровно закреплённый target). Evidence
берётся из accepted green QA того же SHA. Ответ содержит `integration_record_id`, пару
`candidate_sha`/`target_sha` и ссылки на исходное QA и publish по digest. Повторный `prepare` не
создаёт вторую запись и не теряет первую (`created: false`). Отказ всегда содержит remedy. Случаи
отказа: не найден batch для тикета и ветки; `--batch` чужой пары; batch не завершён или не
опубликован; несколько опубликованных batch без `--batch` (подмены одного batch другим нет);
`--candidate-commit` не равен опубликованному SHA; remote-ветка отсутствует или другая; нет accepted
green QA; integration ref уже ушёл вперёд, а записи ещё нет; remote недоступен; история batch
изменена после записи.

Запись лежит в `reports/integration/`, а evidence — в `reports/integration-evidence/` внутри
уже проверяемого каталога `reports`: версия схемы ledger и `ledger migrate` не меняются.

`status` — только наблюдение, под тем же read-only контрактом. Он не создаёт dispatch, ничего не
записывает и при повторе даёт тот же ответ. Состояния: `current` (integration ref всё ещё на
записанном target), `stale` (ref ушёл вперёд; `refresh_required: true`) и `unavailable` (remote не
ответил; подтвердить пару нельзя). Исходное QA относится только к записанной паре.
`source_evidence.applies_to_current_pair` равен `false` при `stale`, и старое QA не принимается для
новой пары candidate/target. `status` сам ничего не запускает. Обновить пару при `stale` позволяет
`integration refresh` (ниже). После refresh `status` показывает текущую пару (`candidate_sha`,
`target_sha`, исходные значения в `original_candidate_sha`/`original_target_sha`), список `refreshes`
и блок `verification`: `required: true`, пока нет passed CI или local-QA именно этой пары
(`resolver` не считается), и всегда `re_review_required: false`.

`refresh` — маршрут подготовки PR вместо обязательного developer-перезапуска из-за сдвига базы:

```bash
python .harness/orchestration/coordinator.py --repo . integration refresh \
  --ticket '#123' --branch feature/issue-123-short-name
```

Команда читает текущий SHA integration ref. Если он равен target пары, rebase не запускается
(`state: unchanged`, ничего не пишется). Иначе она перебазирует issue-ветку в worktree своего batch
на этот точный SHA и публикует ветку через `--force-with-lease` с ожидаемым старым SHA, так что
чужой коммит на remote не теряется. Команда отказывает с remedy, если worktree не на issue-ветке
на записанном candidate, если в worktree есть незакоммиченные изменения или операция в процессе или
если remote-ветка уже не равна записанному candidate. Stash, reset и обход не применяются, protected
и `integration/*` ветки целью записи не бывают, а чужие worktree не затрагиваются.

Чистый rebase возвращает `state: rebased`, `new_candidate_sha` и `verification_required: true`, не
вызывает resolver и не тратит его два цикла. Текстовый конфликт возвращает `state: conflict` и
`resolver` (`conflicting_files`, `candidate_sha`, `target_sha`, `worktree`, `cycles_spent: 0`);
rebase при этом отменяется, а ветка и worktree остаются как были. Rebase пишет immutable
`IntegrationRefreshRecord` в `reports/integration-refresh/` (прежний и новый candidate, target,
коммиты до и после). Старое QA остаётся историческим evidence, а новый candidate подтверждают CI
или local-QA пары через `link-evidence`; повторный review из-за refresh не нужен. Конфликт и работу,
которой нужен developer, ведёт маршрут rebase из ADR 0012, и `refresh` от него не зависит.

`resolve` — маршрут текстового конфликта (роль `conflict-resolver`, ADR 0015). Он ничего не пишет в
Git и отклоняет чистый rebase, потому что это работа `refresh`. Конфликт превращается в новый batch
вида `resolver` рядом с завершённым batch тикета, при этом его brief, отчёты и история не меняются:

```bash
python .harness/orchestration/coordinator.py --repo . integration resolve \
  --ticket '#123' --branch feature/issue-123-short-name
```

Ответ содержит `batch_id`, конфликтные файлы, candidate и target SHA, scope и остаток бюджета.
`next_action` batch — `resolve-conflict`. Дальше идёт обычный путь: `batch approve`, затем
`dispatch create --role conflict-resolver --purpose work` (сначала `--propose`). Brief несёт
неизменяемую секцию `resolver`: тикет, требования обеих сторон (`sides.candidate` — DoD исходного
batch; `sides.target` — plan-записи тикетов `(#N)` из subject коммитов цели, иначе subject и тело
коммита), SHA candidate и target, scope, запреты, план коммита, проверки, остаток бюджета и
`report_staging_path`. Роль открывает skill `resolving-merge-conflicts`, сохраняет требования обеих
сторон и не добавляет функциональность вне них.

Бюджет — два автоматических target SHA, а третий требует решения человека. Цикл тратит только
зафиксированный отчёт resolver-а по новому target SHA, тогда как чистый rebase, ответ человека и
правка на том же target цикл не тратят. Правки на одном target ограничены
`retry_policy.max_developer_retries`. Бюджет выводится из append-only событий
`reports/resolver-events/` (`cycle-spent`, `same-target-fix`, `human-decision`, `scope-change`,
`exhausted`), поэтому потеря сессии или resume его не сбрасывают.

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
Изменение scope — обычное approval нового dispatch (`--kind scope-change` лишь фиксирует его).
Исходный brief не переписывается. Существующая проверка отклоняет resume с изменившимися фактами.
Потерянная runtime-сессия возобновляется через существующие checkpoint/resume или
`batch resume`. Счётчики берутся из событий.

После исчерпания бюджета (`exhausted`) останавливается только эта задача: ветка и evidence
сохраняются. Несовместимость интеграции продолжает тот же resolver после решения человека.
Собственный дефект тикета (`resolver.cause: task-defect`) возвращается обычному developer-у.

Отчёт resolver-а несёт верхнеуровневый блок `resolver`: `preserved_requirements` (каждое требование
обеих сторон дословно из brief), `human_decisions` (id только тех событий human-decision, что
отвечают на checkpoint этого dispatch), `target_sha`, `resolved_candidate_sha`, `cause`,
`changed_files` и `commits` с записью плана для каждого коммита. Автономный
`human-decision --extends-budget` без checkpoint фиксируется лишь событием ledger и в отчёт не
попадает. Кроме ещё одного автоматического target-цикла, он даёт ещё `max_developer_retries`
попыток исправления того же target. Принятая резолюция идёт узким маршрутом: повторный code-review
пропускается, но QA и CI либо local-QA новой пары candidate/target обязательны
(`integration status` держит `verification.required`).

`collect-ci` собирает CI-доказательство для комбинированного результата PR (ADR 0016):

```bash
python .harness/orchestration/coordinator.py --repo . integration collect-ci \
  --ticket '#123' --branch feature/issue-123-short-name --pull-request 45
```

Команда работает только с трекером `github` и списком `ci_required_checks` из
`.harness/project.json`. CI принимается, если выполнены оба условия: проверки шли на merge commit
PR, родители которого — ровно текущие candidate и target; все обязательные проверки прошли. Тогда
команда пишет immutable запись с `verification: collector-accepted`. `integration status`
показывает блок `qa_replacement` (источник, репозиторий, PR, SHA пары, merge commit, id check run).
Если integration ref ушёл вперёд, `qa_replacement.applies` становится `false`, а
`re_refresh_required` — `true`: нужен новый refresh и новый сбор. Запасной путь — полный локальный
QA. Он нужен при `fallback` с причиной `unsupported_tracker`, `not_configured`, `unavailable`,
`unknown_checkout`, `head_only`, `stale_candidate`, `stale_target`, `missing_check`, `pending_check`
или `inconclusive_check`. В этом случае ничего не записывается. Недоступность CI не считается ни
находкой в коде, ни успехом. Завершённый `failure` на подтверждённой паре записывается как
`collector-failed`. Вручную привязанный через `link-evidence` CI остаётся `unverified` и проверку
пары не закрывает. CI не заменяет исходные QA-отчёты.

`local-qa` — запасной путь, когда CI комбинированного результата отсутствует, недоступен или
непригоден (`--ci-condition absent|unavailable|unusable`, обязательные `--reason` и `--record`):

```bash
python .harness/orchestration/coordinator.py --repo . integration local-qa \
  --record <integration-record-id> --ci-condition absent --reason 'CI не настроен для ветки'
```

Команда закрепляет immutable запрос (пара candidate/target, причина, полный список
`verification_commands`) и запускает существующий gate runner в изолированном clean-room checkout
точного candidate. Ветка задачи, terminal source batch и принятые отчёты при этом не меняются, и
код не чинится. Запуск идёт через общую FIFO-очередь QA (`qa status`): два тяжёлых QA не работают
одновременно, а остальные batch продолжают реализацию. Результат команда пишет отдельной immutable
записью с командами, санитизированным артефактом и checksum и привязывает его как evidence
`kind: local-qa` с `verification: verified`, но только если пара и remote-ветка не сдвинулись:
прежний результат не подтверждает другой SHA или движение target. Провал проверки — finding с
`state: failed`, который остаётся для маршрутизации PR-сессией. Недоступность инфраструктуры даёт
отдельный `state: unavailable` без findings. Повтор возможен только явным `--retry` и не более
`max_infrastructure_retries`, после чего наступает `state: exhausted`. Вручную привязанный
`local-qa` остаётся `unverified` и проверку пары не закрывает.

`next` — read-only шаг продолжения PR (ADR 0017). Он ничего не пишет в Git, ledger, dispatch и PR и
не обращается к трекеру. Он классифицирует `status`, evidence, открытые resolver batch и события
бюджета:

```bash
python .harness/orchestration/coordinator.py --repo . integration next \
  --ticket '#123' --branch feature/issue-123-short-name [--pull-request 45]
```

Шаги (`step`): `unavailable` (integration ref не читается — операционная остановка, не дефект кода),
`resolver-open` (открыт resolver batch; PR-сессия ждёт, цикл не тратится), `refresh`, `route-failure`
(провал проверки текущей пары), `human-decision` (бюджет resolver исчерпан), `confirm-pr` (до PR),
`verify` (с `--pull-request`: обновлённой паре нужны CI либо local-QA) и `handoff` (пара актуальна и
проверена: `candidate_sha`, `target_sha`, `qa_source` `original-qa|ci|local-qa`, `reference`,
`refreshed`). Старое evidence допускает вход в подготовку PR (`confirm-pr`), но не считается QA
нового candidate: `verify` и `handoff` требуют проверки именно обновлённой пары.

Правило маршрута провала: провалом считается только `collector-failed` CI или `failed`
сгенерированного local-QA текущей пары, у которой нет passed проверенной проверки. Passed, verified
проверка текущей пары снимает провал независимо от порядка записи. Провал, записанный после неё, не
открывает маршрут, пока passed продолжает относиться к текущей паре. Если пара прошла refresh или
resolver, маршрут — `resolver` (поведенческая несовместимость после чистого rebase, следствие
правки resolver; бюджет `cycles_total`/`cycles_spent`/`remaining`/`internal_fix_budget` и
`fixes_on_target` в ответе; при исчерпании — `human-decision` с
`integration resolver-event --extends-budget`). Если пара исходная, маршрут — `developer`:
собственный дефект задачи идёт обычному developer с review и QA (ADR 0012). Завершённый исходный
batch терминален, и `batch decide` на нём отказывает. Поэтому подсказка `next` называет исполнимый
путь: новый batch того же тикета и issue-ветки обычным маршрутом `/implement`
(`batch create --ticket T --branch B --worktree W --integration-ref I`, затем architect, developer,
code-review, QA и publish; завершённый batch новый не блокирует). База нового batch — integration
tip. Поэтому developer-отчёт отображает в `commit_map` и уже опубликованные коммиты ветки (с `dod_coverage` и
`divergence_justification`). После принятого publish `integration prepare --ticket T --branch B --batch
<новый batch>` создаёт новую запись, `integration next --record <новая запись> --pull-request N`
продолжает PR, а упавшее evidence прежней записи остаётся историей. У одного тикета и ветки теперь
две записи, и `<ticket-branch>` отклоняется отказом «several integration records match».
`--record <новая запись>` заменяет `<ticket-branch>` в командах `integration` скила `to-pull-requests`.
Операционный fallback CI и `unavailable`/`exhausted` local-QA не создают failed-evidence и не
становятся провалом кода. Для провала обновлённой пары `integration resolve` создаёт resolver batch с
`resolver.trigger: verification-failure` (`failed_evidence_ids`, пустые `conflicting_files`) даже
при `tip == target`. Сторона `target` берётся из коммитов, которые легли в target после исходного
target записи (`identity.target_sha..tip`). Scope остаётся собственными файлами задачи (правка
других файлов — новый утверждённый dispatch `scope-change`). Бюджет и лимит правок на том же target
те же, что у конфликта. Ответ человека и ожидание CI бюджет не сбрасывают и не тратят.

Подсказка `next` в результате `collect-ci`: `pending_check` — `{action: wait}`; `not_configured` и
`unsupported_tracker` — `{action: local-qa, ci_condition: absent}`; `unavailable` — `local-qa` с
`unavailable`; остальные fallback — `local-qa` с `unusable`; `failed` — `{action: route}`. При `accepted`
подсказки нет: полный локальный QA пропускается (`qa_replacement.applies`).

`/to-pull-requests` ведёт этот процесс. До PR нужно отдельное подтверждение человека, привязанное
к точной паре `candidate_sha`/`target_sha`. Смена пары перед открытием делает его недействительным.
После открытия или обновления PR идёт ожидание CI, при непригодном CI — запасной `local-qa`, затем
`handoff` для ручного merge. Перед merge показываются проверенные пары SHA и источник QA. Handoff
не обещает неизменность до merge, а новый target повторяет актуализацию и проверку. Server branch
protection и merge queue автоматически не включаются. Merge остаётся ручным, тикет закрывается
только после подтверждённого merge.

`link-evidence` — единственный публичный способ привязать к записи будущие результаты CI, local-QA
или resolver (`--kind ci|local-qa|resolver`). Каждая привязка — отдельная immutable запись со своей
парой `candidate_sha`/`target_sha` и `verification: unverified`; исходное evidence записи никогда не
получает новую пару, а проверка новой пары не подменяет старую. Повтор той же привязки
идемпотентен (`linked: false`). Сами CI, local-QA и resolver эта операция не запускает. В `status`
каждая привязка показывает, относится ли её пара к текущему tip
(`pair_checks[].applies_to_current_pair`).

### Advisory tool call

`.harness/orchestration/advisory.py` — дешёвый non-role CLI для чисто утилитарных подзадач:
ранжирование файлов по keyword, сводка лога и грубая риск-подсказка. Он выполняется вне
brief/report/self-report/heartbeat контракта. Это не dispatch: он не пишет ledger-запись и не
импортирует `ledger/`/`contract.py`/`coordinator.py`. Вывод эфемерен. Он печатается в stdout и
пересчитывается заново при каждом вызове. Он нигде не сохраняется как ground truth для другого
dispatch:

```bash
python .harness/orchestration/advisory.py rank-files --keyword payments -- services/payments/handler.py README.md
python .harness/orchestration/advisory.py summarize-log --file qa-output.log
python .harness/orchestration/advisory.py classify-risk --text "data migration for payments" --known-trigger data-migration
```

Вывод advisory не является авторизацией: coordinator/contract validation path не принимает его как
основание создать dispatch, понизить риск, принять QA или изменить scope. Единственным
авторитетным источником риска остаётся `coordinator.py risk assess`.

## 5. Необязательный внешний adapter

Для `transport: "external"` проект передаёт `--adapter <path>` в `dispatch send`. Это transport-only
граница. Coordinator передаёт этому исполняемому файлу одобренный immutable brief через `--repo` и
`--brief`.
Adapter отвечает за запуск worker в выбранной среде. Он не выбирает scope, не принимает report,
не планирует следующий dispatch и не мержит PR. После запуска worker возвращает completion report
по общим правилам раздела 4. Проверки доступа и среды запуска принадлежат проектному adapter.

## 6. Первый pilot и источник правил

Не задавайте лимиты токенов или «правильный» уровень параллелизма на глаз. В первом периоде pilot
записывайте по каждому закрытому ticket agent starts, tokens per batch, quality-gate wall time и
post-integration defects, включая источник и отсутствующие данные. Форма и единые правила подсчёта
лежат в `.harness/orchestration/pilot.md`.

Полный нормативный источник — `.harness/orchestration/playbook.md`, а границы отдельных ролей лежат
в `.harness/orchestration/roles/`. При противоречии между удобством конкретного runtime и этим
контрактом приоритет у manifest'а, immutable brief и явного approval.

Архитектурный контракт маршрута целиком зафиксирован в
[ADR 0003](https://github.com/PVMalove/claude-agent-harness/blob/master/docs/adr/0003-orchestration-core.md): opt-in capability, отдельное
approval для dispatch, review и QA для одного SHA, локальное санитизированное evidence и adapter
только для транспорта. Превращение `/implement` в coordinator-driven конвейер по умолчанию, model
self-report, dispatch watchdog, per-role transport и zero-config дефолты зафиксированы в
[ADR 0005](https://github.com/PVMalove/claude-agent-harness/blob/master/docs/adr/0005-implement-pipeline.md).
