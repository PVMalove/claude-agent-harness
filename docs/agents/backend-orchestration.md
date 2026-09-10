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

`update` не перезаписывает изменённые managed files без `--force`. Seed-документы тоже сохраняются;
`--force-seed-files` перезаписывает их только после проверки локальных изменений. После любого
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
    "architect": {"zone": "payments", "runtimes": {"codex": {"profiles": ["backend-primary"], "model": "project-architect-model", "effort": "high"}, "claude": {"profiles": ["backend-claude"], "model": "project-architect-claude-model", "effort": "high"}}},
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
  "verification_commands": ["python -m pytest"]
}
```

`transport` — необязательное поле assignment plan и выбирается для каждой роли отдельно: `orca`
(по умолчанию) запускает isolated worker через `orca_adapter.py`, `in-process` исполняет роль как
субагента текущей coordinator-сессии в worktree того же batch. Оба варианта получают один и тот же
immutable brief, обязаны пройти model self-report и вернуть completion report по общим правилам,
поэтому логика coordinator-а от транспорта не зависит. `harness health` проверяет допустимость
значения.

Зона — не подсказка, а граница: write-роль изменяет только разрешённые пути своей зоны. Если роли
нужен более узкий scope, задайте ей `write_paths`: brief и completion report будут проверяться по
этому списку, а не по широкому списку зоны. Model должен быть CLI-алиасом или ID без пробелов
(например, `sonnet`), а не отображаемым названием. Для
нескольких независимых batch заведите непересекающиеся зоны и увеличьте
`concurrency_budget` только после явного решения coordinator-а. Сначала прогоните `harness health`:
он проверит JSON, существование profile/zone, совместимость capability, fallback и режим
`code-review`.

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
   возвращает путь к brief, а роль исполняет субагент текущей сессии. Без `.harness/orchestration.json`
   к `dispatch create` добавляются `--model` и `--effort` вызывающей сессии.
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

### Model self-report и dispatch watchdog

Отправленный dispatch не считается живым сам по себе. Первым действием после получения brief роль
подтверждает фактически активную модель:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch self-report \
  --dispatch <dispatch-id> --model <фактическая модель>
```

Совпадение с `resolved_model` immutable brief переводит dispatch в `working`. Расхождение немедленно
переводит его в `blocked`, помечает batch `blocked` и завершает команду ошибкой; после этого
`report submit` для такого dispatch не принимается. Починка — новый dispatch с новым brief, а не
правка отправленного. Completion report вообще не принимается без успешного self-report, поэтому
подменённая или неверно настроенная модель видна сразу, а не после потраченного окна.

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

## 5. Необязательный запуск через Orca

`orca_adapter.py` — transport-only граница: он переводит **уже одобренный** JSON brief в Orca task и
isolated worker. Он не выбирает scope, не запускает checks, не принимает report, не планирует
следующий dispatch и не мержит PR. Перед запуском выполните
`harness health`, проверьте, что profile выбранной роли содержит непустой `agent`,
а assignment plan обязательно содержит role-level `model` и `effort`,
а branch соответствует `branch_pattern` из `.harness/project.json` и не является base или
`integration/*`.

Пример brief для write-роли. `verification_commands` должен буквально совпадать с массивом в
`.harness/orchestration.json`; `write_paths` — буквально с путями выбранной зоны. Не добавляйте
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
