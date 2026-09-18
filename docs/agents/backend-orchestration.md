# Руководство по backend-оркестрации

`backend-orchestration` — opt-in capability для согласованной backend-разработки несколькими
ролями. Она расширяет `pvmalove-suite`, но не является автономным scheduler: coordinator (человек
или назначенная им управляющая сессия) планирует batch, утверждает каждый dispatch и принимает
результат. Роли не расширяют свой scope, не выбирают модель и не мержат pull request.

Целостный действующий контракт capability, включая её место в системе, роли, clean-room QA и
локальное state-хранилище, приведён в [current-state.md](./current-state.md). Этот документ
содержит подробную процедуру настройки и запуска.

Используйте её, когда у задачи есть независимые backend-границы или обязательная независимая
проверка. Для обычной одной задачи достаточно стандартного pipeline `pvmalove-suite`.

## Владение правилами

`/implement` — короткий контракт coordinator-а: он сохраняет порядок handoff
`architect → developer → code-review → qa → publish`, явный approval перед каждым dispatch,
model self-report и watchdog. Он не является второй копией процедуры.

Полные правила принадлежат устанавливаемым модулям: `playbook.md` — lifecycle, authority,
immutable brief, evidence, параллелизм и метрики; `roles/` — границы и доказательство каждой роли;
`coordinator.py` — проверяемые переходы и audit; `orca_adapter.py` — только transport. При
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
- `.harness/orchestration/orca_adapter.py` — необязательная runtime-граница для Orca;
- `.harness/orchestration.json` — project-owned конфигурация назначений, зон и проверок.

Coordinator state, immutable briefs/reports и санитизированные QA-артефакты создаются локально в
`.harness/orchestration/state/`; содержимое этой директории gitignored и не является исходным
кодом проекта. Оно остаётся локальным evidence до явного решения coordinator-а о безопасной очистке:
роль и adapter не удаляют историю batch.

Внутренний протокол имеет фиксированные языки: agent-to-agent handoff, checkpoint, state evidence и
свободный текст в `.harness/orchestration/state/` пишутся на английском; completion report,
адресованный coordinator-у, — на русском и содержит `"report_language": "ru"`. Команды, пути, SHA,
имена тестов и цитаты исходных требований не переводятся. Это уменьшает двусмысленность между
разными runtime и оставляет отчёт человеку читаемым.

Manifest определяет режим роли (`write` или `read-only`), capability и risk triggers. Проектный
конфиг выбирает agent/fallback на уровне provider profile, а `model` и `effort` — отдельно для
каждой роли в её assignment plan, вместе с зоной, бюджетом параллелизма и командами проверки; он не может
ослабить границы manifest'а. Значения секретов не хранятся ни в конфиге, ни в brief, ни в report.

## 1. Включение

Для нового git-репозитория выберите только `backend-orchestration`: зависимость от
`pvmalove-suite` будет разрешена автоматически.

```bash
python3 harness/bin/harness init /path/to/repository \
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
python3 harness/bin/harness diff /path/to/repository
python3 harness/bin/harness update /path/to/repository --capability backend-orchestration
```

`update` не перезаписывает изменённые managed files без явного флага. `--force-managed-files`
обновляет только managed snapshot и сохраняет seed-документы; `--force-seed-files` перезаписывает
только seed, а `--force` объединяет оба действия. После любого
включения или изменения конфигурации выполните:

```bash
python3 harness/bin/harness health /path/to/repository
```

## 2. Настройка `.harness/orchestration.json`

Конфиг **не обязателен**. Без него coordinator работает на дефолтах: единственная зона `repository`
покрывает весь репозиторий, `verification_commands` берутся из `qa_gate_commands` в
`.harness/project.json`, `concurrency_budget` равен 1, а `model`/`effort` роли приходят из вызывающей
сессии (`dispatch create --model <model> --effort <effort>`). Транспорт в этом режиме всегда
`in-process`: provider profile нет, значит и Orca-агента запускать нечем. `harness health` такой
проект принимает. Конфиг нужен, когда проекту нужны настоящие зоны, разные модели по ролям,
Orca-транспорт или бюджет параллелизма больше единицы.

Запускайте coordinator-сессию с `medium` effort по умолчанию. Для architect в assignment plan также
выбирайте `medium`; более высокий effort требует явного решения разработчика для названного
труднообратимого вопроса, а не является дефолтом каждого ticket.

Начальный шаблон намеренно пуст. Заполните provider profile, одну или несколько backend-зон,
назначение для **каждой** используемой роли и реальные project checks. `code-review` следует
назначить всегда: validator требует его, когда в конфиге есть назначения, поскольку это
обязательный gate для high-risk работы.

Ниже минимальный полный пример. Имена agent, model и effort принадлежат конкретному проекту.
`agent` и fallback задаются в provider profile. Assignment plan каждой роли содержит именованные
runtime-наборы (`codex`, `claude` и т.п.); в каждом обязательны `profiles`, `model` и `effort`.
Выбранный runtime фиксируется в immutable brief и не меняется при failover profile.
`agent` нужен только для последующего запуска через Orca, но показан сразу, чтобы один конфиг
подходил обоим режимам.

Если у роли несколько runtime, задайте `default_runtime` в её assignment plan; иначе каждый
`dispatch create` обязан явно передать `--runtime`. Скрытого fallback на Codex нет. Для review,
который проект всегда хочет запускать через Claude, это выглядит как
`"default_runtime": "claude"` рядом с `runtimes`. Новый template также включает
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
        "code-review"
      ],
      "agent": "codex",
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
        "code-review"
      ],
      "agent": "codex",
      "fallback": [],
      "known_limitations": ["Использовать только после безопасного отказа primary"]
    }
  },
  "assignment_plans": {
    "architect": {"zone": "payments", "runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-architect-model", "effort": "medium"}, "claude": {"profiles": ["backend-claude"], "model": "project-architect-claude-model", "effort": "medium"}}},
    "developer": {"zone": "payments", "write_paths": ["services/payments/**"], "transport": "orca", "runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-developer-model", "effort": "xhigh"}, "claude": {"profiles": ["backend-claude"], "model": "sonnet", "effort": "xhigh"}}},
    "database-migrations": {"zone": "payments", "runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-migration-model", "effort": "xhigh"}, "claude": {"profiles": ["backend-claude"], "model": "project-migration-claude-model", "effort": "xhigh"}}},
    "messaging-integration": {"zone": "payments", "runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-messaging-model", "effort": "high"}, "claude": {"profiles": ["backend-claude"], "model": "project-messaging-claude-model", "effort": "high"}}},
    "qa": {"zone": "payments", "runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-qa-model", "effort": "medium"}, "claude": {"profiles": ["backend-claude"], "model": "project-qa-claude-model", "effort": "medium"}}},
    "code-review": {"zone": "payments", "runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-review-model", "effort": "high"}, "claude": {"profiles": ["backend-claude"], "model": "project-review-claude-model", "effort": "high"}}}
  },
  "backend_zones": {
    "payments": {"paths": ["services/payments/**"]}
  },
  "concurrency_budget": 1,
  "developer_verification_commands": ["python -m pytest tests/unit"],
  "verification_commands": ["python -m pytest"]
}
```

`transport` — необязательное поле assignment plan и выбирается для каждой роли отдельно: `in-process`
(по умолчанию) исполняет роль как
субагента текущей coordinator-сессии в worktree того же batch. Оба варианта получают один и тот же
immutable brief, обязаны пройти model self-report и вернуть completion report по общим правилам,
поэтому логика coordinator-а от транспорта не зависит. `harness health` проверяет допустимость
значения. Для изолированного worker укажите `"transport": "orca"` явно. Для `in-process` `dispatch send` только фиксирует handoff: следующим действием coordinator
немедленно запускает субагента по уже immutable brief, до любого поиска старых report/template или
конфигурации. Architect собирает лишь targeted evidence для решения; полный набор
`verification_commands` выполняет clean-room QA, а developer получает
`developer_verification_commands`. Это необязательное поле: без него сохраняется совместимый
режим, в котором developer получает полный список. Задавайте в нём быстрые task-scoped проверки,
а в `verification_commands` — независимый полный gate. Code-review получает полный список, но
запускает каждую команду через `test_summary.py`: в report остаются исходная команда и bounded
summary, а санитизированный полный лог доступен только для упавшей проверки.

Зона — не подсказка, а граница: write-роль изменяет только разрешённые пути своей зоны. Если роли
нужен более узкий scope, задайте ей `write_paths`: brief и completion report будут проверяться по
этому списку, а не по широкому списку зоны. Model должен быть CLI-алиасом или ID без пробелов
(например, `sonnet`), а не отображаемым названием. Для
нескольких независимых batch заведите непересекающиеся зоны и увеличьте
`concurrency_budget` только после явного решения coordinator-а. Сначала прогоните `harness health`:
он проверит JSON, существование profile/zone, совместимость capability, fallback и режим
`code-review`.

### Бюджет контекста и preflight

Новый batch не создаётся, пока `batch preflight` (он же автоматически вызывается из `batch create`)
не подтвердит ограниченный scope. Укажите ожидаемые changed paths, один bounded context/service и
консервативный размер diff; значения выше project policy нужно сначала разделить через
`/to-tickets`, а не передавать в architect как discovery-задачу:

```bash
python .harness/orchestration/coordinator.py --repo . batch preflight \
  --ticket '#123' --zone payments \
  --definition-of-done 'Добавить валидацию платежа' \
  --expected-file services/payments/validation.py \
  --expected-service payments --expected-changed-lines 120
```

Template ограничивает DoD (5), dependencies (3), файлы (12), сервисы (1), diff (800 строк) и
ожидаемый context (80k tokens). Проект может ужесточить эти значения через `preflight_policy`.
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

### Discovery Context и Context Package

Discovery Pipeline переносит проверенный контекст от проектирования к dispatch. `/grilling` ведёт
`Live Artifact` с кандидатными путями, но добавляет путь только после явного согласия пользователя.
`/to-spec` сохраняет утверждённый список в эпике под `## Relevant Files (Discovery Context)`, а
`/to-tickets` назначает каждый путь подходящему tracer-bullet тикету и строит path-only filtered Repo
Map. Один cheap advisory-вызов может добавить только точные зависимости из этого Repo Map; его
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
estimate. Для Python AST извлекает
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

В базовом наборе есть три write-роли и три read-only роли.

| Роль | Режим | Когда назначать |
| --- | --- | --- |
| `developer` | write | Обычное backend-изменение внутри service или bounded context. |
| `database-migrations` | write | Schema/data migration и её rollout/rollback. |
| `messaging-integration` | write | Outbox, routing, message schema, retry или DLQ. |
| `architect` | read-only | Труднообратимое граничное решение. |
| `qa` | read-only | Нужна независимая проверка через project-facing interface. |
| `code-review` | read-only | Обязателен для listed high-risk triggers; выдаёт отдельные Standards и Spec reports. |

Одна задача может пройти несколько ролей, но handoff внутри одного batch всегда последовательный и
в нём бывает только один active writer. Не открывайте два batch с пересекающимися service, bounded
context или infrastructure zone. Тяжёлые integration/quality checks идут в одной
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
`planned → awaiting-approval ↔ active → completed | blocked | failed`; каждый report возвращает
batch в `awaiting-approval` и оставляет dispatch в `reported` до решения человека:

1. Создать planned batch и затем отдельно утвердить его:

   ```bash
   python .harness/orchestration/coordinator.py --repo . batch create \
     --ticket '#123' --branch feature/issue-123-payment-validation \
     --worktree issue-123-payment-validation --zone payments \
     --definition-of-done 'Добавить валидацию платежа' \
     --prohibited-change 'Не менять migration или публичный API'
   python .harness/orchestration/coordinator.py --repo . batch approve \
     --batch <batch-id> --approved-by 'имя утверждающего' \
     --approved-at 2026-09-09T12:00:00Z
   ```

   `batch create` сначала выполняет `git fetch origin <ref>` — `--integration-ref`, если он передан,
   иначе `base_branch` проекта (для epic-less задач) — и фиксирует полученную вершину как
   `base_commit`/`integration_base_commit`; необновлённый локальный HEAD никогда не используется как
   замена. Перед созданием `code-review`- или `publish`-dispatch coordinator обязательно повторяет
   эту сверку: если `origin/<ref>` с тех пор сдвинулся, dispatch отклоняется, next_action переходит в
   `developer`, а снять блокировку может только новый developer dispatch (rebase) — его commit
   автоматически становится новым `candidate_commit` и заново проходит risk assessment.
2. Сверить активные batch, пересечения зон, writer и quality-gate lane. При конфликте оставить
   batch `blocked`, а не запускать параллельную запись.
3. Создать и отдельно утвердить architect dispatch, принять его отчёт, и только потом — developer
   dispatch. Порядок жёсткий: `dispatch create --role developer` отклоняется, пока для того же batch
   нет architect-отчёта, принятого через `batch decide --decision accept`. Правило живёт в
   `coordinator.py`, поэтому действует и для ручного CLI, и для `/implement`. CLI сохраняет immutable
   brief до передачи:

   ```bash
   python .harness/orchestration/coordinator.py --repo . dispatch create \
    --batch <batch-id> --role developer --runtime codex --approved-by 'имя утверждающего' \
     --approved-at 2026-09-09T12:01:00Z
   python .harness/orchestration/coordinator.py --repo . dispatch send \
     --dispatch <dispatch-id> --adapter .harness/orchestration/orca_adapter.py \
     --adapter-arg=--run --adapter-arg=<orca-run-id>
   ```

   Для роли с `transport: "in-process"` adapter не передаётся вовсе: `dispatch send --dispatch <id>`
   возвращает путь к brief, после чего coordinator **немедленно** запускает роль как субагента текущей
   сессии — никаких чтений предыдущих dispatch/report/template между этими действиями. Без
   `.harness/orchestration.json` к `dispatch create` добавляются `--model` и `--effort` вызывающей
   сессии; для coordinator и architect выбирайте `medium`, если разработчик явно не одобрил иное.

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
   До следующего dispatch coordinator должен записать отдельное решение. `reported` — не
   автоматический переход: человек принимает report, override-ит только warning или требует retry,
   а новая роль всё равно ждёт собственного approval:

   ```bash
   python .harness/orchestration/coordinator.py --repo . batch decide \
     --batch <batch-id> --decision accept --approved-by 'имя утверждающего' \
     --approved-at 2026-09-09T12:02:00Z
   ```

Минимальный ручной brief хранит ticket и dispatch ID, роль и её access, выбранный profile/model/effort,
zone и allowed paths, issue-ветку/worktree, DoD, запреты, команды, dependencies, approval. Для
write-роли completion report обязан включать commit SHA, exact changed files, результаты всех checks,
risks, blockers и следующее решение coordinator-а. Для read-only роли вместо SHA указывается
`not applicable — read-only role`.

Новые факты не меняют отправленный brief. Coordinator добавляет отдельное решение с evidence; если
изменились scope, zone, DoD, assignment или proof, текущий dispatch заканчивается и создаётся новый.
Повтор после `blocked` или `failed` — тоже новый dispatch с новым ID и brief.

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
`self-report` буквально совпадал с immutable `snapshot_commit` из brief. После `batch decide --decision
retry` (например, developer-retry после code-review blocker) новый developer dispatch **всегда** пинит
`snapshot_commit` обратно на `base_commit` batch-а, а не на отклонённый кандидатный коммит — чтобы retry
не мог молча унаследовать состояние отклонённого коммита. Это значит, что coordinator обязан сам
привести worktree к этому состоянию **до** `dispatch send`, иначе первый же `dispatch self-report`
новой worker session упадёт с `AttestationError`:

```bash
git -C <worktree> status --short
git -C <worktree> reset --soft <base_commit>
```

Используйте именно `--soft`, не `--hard`: он передвигает только HEAD, оставляя diff отклонённого
кандидата staged в рабочем дереве — новая worker session стартует с тем же кодом и правит только то,
что назвал review, вместо повторной реализации с нуля. Ошибка attestation-несовпадения теперь сама
называет точную команду для исправления.

Пока роль работает, она отбивает heartbeat, а coordinator-сессия опрашивает состояние:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch heartbeat --dispatch <dispatch-id>
python .harness/orchestration/coordinator.py --repo . dispatch status \
  --batch <batch-id> --stale-after 900
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
любого другого сигнала, кроме auto-resume по 429 (см. `current-state.md`). Baseline из
`playbook.md` («Baseline metrics») тем же образом остаётся ориентиром, а не скрытым лимитом.

### Checkpoint и новая worker session

Write-роль (developer, database-migrations, messaging-integration) может растянуть один dispatch на
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
непересекающуюся zone, последовательность ролей, immutable brief и требуемые risk gates;
не меняй protected или integration branch.
```

### Advisory tool call

`.harness/orchestration/advisory.py` — дешёвый non-role CLI для чисто утилитарных подзадач:
ранжирование файлов по keyword, сводка лога и грубая риск-подсказка. Он выполняется вне
brief/report/self-report/heartbeat контракта: не является dispatch, не пишет ledger-запись и не
импортирует `ledger.py`/`contract.py`/`coordinator.py`. Вывод эфемерен — печатается в stdout и
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

## 5. Необязательный запуск через Orca

`orca_adapter.py` — transport-only граница: он переводит **уже одобренный** JSON brief в Orca task и
isolated worker. Он не выбирает scope, не запускает checks, не принимает report, не планирует
следующий dispatch и не мержит PR. Перед запуском выполните
`harness health`, проверьте, что profile выбранной роли содержит непустой `agent`,
а assignment plan обязательно содержит role-level `model` и `effort`,
а branch соответствует `branch_pattern` из `.harness/project.json` и не является base или
`integration/*`.

Пример brief для write-роли. Для developer work-dispatch `verification_commands` должен буквально
совпадать с `developer_verification_commands` (либо с `verification_commands`, если focused-список
не задан); для остальных ролей — с `verification_commands`. `write_paths` — буквально с путями
выбранной зоны. Не добавляйте
поля или значения, похожие на секреты.

```json
{
  "ticket": "#123",
  "role": "developer",
  "access": "write",
  "zone": "payments",
  "write_paths": ["services/payments/**"],
  "branch": "feature/issue-123-payment-validation",
  "worktree": "issue-123-payment-validation",
  "definition_of_done": ["Добавить валидацию платежа и её тесты"],
  "prohibited_changes": ["Не менять migration или публичный API"],
  "verification_commands": ["python -m pytest"],
  "required_gates": ["code-review, если найден high-risk trigger"],
  "dependencies": ["none"],
  "resolved_provider_profile": "backend-primary",
  "resolved_model": "project-developer-model",
  "resolved_effort": "xhigh",
  "coordinator_approval": {
    "approved_by": "имя утверждающего",
    "approved_at": "2026-09-09T12:00:00Z"
  }
}
```

Сохраните этот JSON без последующего редактирования и передайте в adapter вместе с
coordinator-owned Orca Run ID:

```bash
python .harness/orchestration/orca_adapter.py dispatch \
  --repo . \
  --brief approved-dispatch.json \
  --run <orca-run-id>
```

Adapter проверяет approval, роль, zone, существование issue-ветки, путь, CLI-формат model, project checks,
role-level model/effort и budget до создания task. Он считает только active supervised workers текущего
Orca Run, поэтому завершённые или чужие сессии не исчерпывают budget. Он
пишет новую immutable запись в `.harness/orca-dispatches/`. При явной недоступности agent/model он
может перейти к project-configured fallback; при неопределённом результате не делает fallback,
чтобы не создать дублирующий dispatch. Исчерпанный `concurrency_budget` также останавливает запуск
до создания task.

Для read-only роли укажите её `access: "read-only"` и не передавайте `write_paths`. После старта
следите за результатом в Orca и всё равно получите completion report по правилам раздела 4.

## 6. Первый pilot и источник правил

Не задавайте лимиты токенов или «правильный» уровень параллелизма на глаз. В первом периоде pilot
записывайте по каждому закрытому ticket agent starts, tokens per batch, quality-gate wall time и
post-integration defects, включая источник и отсутствующие данные. Форма и единые правила подсчёта
лежат в `.harness/orchestration/pilot.md`.

Полный нормативный источник — `.harness/orchestration/playbook.md`; role-specific границы — в
`.harness/orchestration/roles/`. При противоречии между удобством конкретного runtime и этим
контрактом приоритет у manifest'а, immutable brief и явного approval.

Архитектурный контракт маршрута целиком зафиксирован в
[ADR 0003](../adr/0003-opt-in-human-governed-orchestration.md): opt-in capability, отдельное
approval для dispatch, review и QA для одного SHA, локальное санитизированное evidence и adapter
только для транспорта. Превращение `/implement` в coordinator-driven конвейер по умолчанию, model
self-report, dispatch watchdog, per-role transport и zero-config дефолты зафиксированы в
[ADR 0014](../adr/0014-coordinator-driven-implement-pipeline.md).
