# claude-agent-harness

Глоссарий терминов проекта, который разворачивает переносимый набор скиллов и правил для
coding agents. Операционные процессы и архитектурные решения находятся в `docs/agents/` и
`docs/adr/`, а не в этом файле.

## Термины

**Харнесс проекта** (project harness):
Установленный в целевой репозиторий набор инструкций, snapshot скиллов, discovery-ссылок,
реестра и lock-файлов, работающий независимо от исходного репозитория харнесса.
_Avoid_: инсталляция, харнесс без уточнения «проекта».

**Сидируемые файлы** (seed files):
Проектные файлы, которые CLI добавляет при отсутствии и после этого передаёт во владение целевому
проекту: инструкции, документы, hooks, rules, subagents и project config.
_Avoid_: шаблонные артефакты, foundation-файлы.

**Капабилити** (capability):
Именованный состав скиллов, описанный в `harness/CAPABILITIES.json`; состав может наследовать,
заменять или добавлять скиллы.
_Avoid_: пакет, набор скиллов.

В текущем составе `pvmalove-suite` 10 скиллов переопределены в `skills/first-party/pvmalove/`: `to-spec`, `to-tickets`, `implement`, `ask-matt`, `code-review`, `grilling`, `grill-me`, `grill-with-docs`, `triage`, `wayfinder`; доп. скиллы: `qa-gate`, `to-guide`, `setup-labels`, `to-pull-requests`.

**Vendor-скилл**:
Скилл из байт-в-байт snapshot закреплённого upstream-источника в `skills/vendor/`, связанный с
его revision, license и provenance.
_Avoid_: ванильный скилл, оригинальный скилл.

**First-party-скилл**:
Скилл, поддерживаемый в этом репозитории под `skills/first-party/`, включая новый скилл или
полную замену одноимённого vendor-скилла.
_Avoid_: кастомный скилл, если речь не о замене vendor-скилла.

**Переопределение капабилити** (capability override):
Механизм `extends`/`overrides`/`additions`, которым capability меняет источник унаследованного
скилла или добавляет новый без дублирования полного списка; подробный состав задаёт
`harness/CAPABILITIES.json`.
_Avoid_: патч, патчинг.

**Snapshot харнесса**:
Набор managed-файлов скиллов в `.harness/skills/`, зафиксированный в `.harness/harness.lock`.
_Avoid_: исходники скиллов — они находятся в `skills/` этого репозитория.

**Дрейф** (drift):
Неподтверждённое расхождение snapshot-файлов целевого проекта с ожидаемым содержимым в lock.
_Avoid_: рассинхронизация, устаревание.

**Проектный конфиг** (`.harness/project.json`):
Источник проектных значений для `qa-gate`, `pr-composer`, `code-review`, `to-guide` и branch
hooks: `language`, `base_branch`, `branch_pattern` и `qa_gate_commands`; необязателен только
`$schema`. Форма описана в `harness/project/project.schema.json`, а `harness health` применяет тот
же строгий контракт и отклоняет неизвестные поля.
_Avoid_: конфигурация проекта, settings.

**Интеграционная ветка эпика** (`integration/<service-or-team>`):
Ветка, которую `/to-spec` создаёт от `base_branch` после публикации эпика. `/to-tickets` переносит
её в дочерние тикеты; issue-ветки создаются от неё, а их PR направляются обратно в неё. Такой PR
ссылается на тикет через `Related to #<ID>`, но не закрывает его: после подтверждённого merge
тикет закрывают через CLI трекера. `Closes #<ID>` применяется только для PR в default branch;
после merge состояние тикета проверяют, поскольку GitLab может отключить или изменить closing pattern.
_Avoid_: project-level base branch, ветка реализации отдельного тикета.

**Проверка публичных метаданных** (`block-public-attribution.sh`):
Локальный PreToolUse hook, который разбирает только `tool_input.command` и проверяет содержимое
commit message, PR/MR title/body и явно переданного literal-файла сообщения; путь к этому файлу не
является метаданными. Неразбираемый payload, незакрытая кавычка или недоступный/переменный путь
блокируют публикацию; CI повторяет проверку уже опубликованных Git-метаданных.
_Avoid_: проверка всей команды, проверка пути body-файла.

**Реестр скиллов проекта** (`.harness/skills/REGISTRY.md`):
Компактный каталог имён, путей и описаний скиллов, используемый как fallback-маршрут для runtime
без native project skill root.
_Avoid_: `skills/` — это исходный каталог этого репозитория.

**Provenance-лок проекта** (`.harness/overlays/project-local.lock`):
Фиксация sha256-файлов project-owned скиллов, не покрытых выбранной capability.
_Avoid_: overlay-лок без уточнения «проекта».

**Инвентарь интеграций** (`.harness/integrations.json`):
Описание native runtime-конфигов с их путями, sha256, runtime-целями, verify-действиями и именами
секретных env-переменных; значения секретов сюда не входят.
_Avoid_: проектный конфиг, секреты.

**Роль оркестрации** (orchestration role):
Короткая декларация ответственности агента, её разрешённых границ, требуемого доказательства результата
и подходящего назначения. Первая backend-наборка ролей: `developer`, `architect`, `qa`,
`database-migrations`, `messaging-integration`, `code-review`.
_Avoid_: модель, provider, job title.

**Code-review role**:
Независимый read-only исполнитель review-гейта; не заменяет автора изменения и не владеет
интеграцией. Его назначение, охват и порог определяются риск-классификацией изменения.
_Avoid_: автоматический мержер, интегратор.

**Ядро оркестрации** (orchestration core):
Переносимый контракт ролей, назначений, batch и handoff, не зависящий от конкретного coding runtime.
_Avoid_: Orca workflow, scheduler конкретного провайдера.

**Runtime adapter**:
Необязательная реализация ядра для конкретной среды запуска, например Orca; переводит назначение в
команды среды, но не определяет правила ролей или workflow.
_Avoid_: ядро оркестрации, role manifest.

**Dispatch approval**:
Явное решение человека запустить согласованный batch. После него конфиг может разрешить роль,
агента и модель; одноразовый override действует только на этот запуск.
_Avoid_: автономный запуск, свободный выбор модели воркером.

**Coordinator**:
Человек или назначенная им управляющая сессия, которая планирует batch, сравнивает зоны, утверждает
dispatch, сохраняет immutable brief и принимает completion report. Это не роль-исполнитель и не
автономный scheduler.
_Avoid_: воркер, сам меняющий свой scope или состояние batch.

**Baseline оркестрации**:
Начальный замер запусков агентов на закрытый тикет, токенов на batch, wall-clock quality gate и
дефектов после интеграции, по которому задаются последующие численные цели.
_Avoid_: лимиты игрового референса, оценка на глаз.

**Role manifest**:
Переносимый Markdown-файл в `harness/orchestration/roles/`, один на роль: зона записи или read-only
граница, требуемое доказательство, capability и fallback-потребности. Он не хранит конкретные модели
проекта.
_Avoid_: проектная настройка агента, JSON-конфигурация роли, prompt задачи.

**Project orchestration config** (`.harness/orchestration.json`):
Валидируемое project-owned отображение роли на agent/model/fallback, бюджеты и команды проверки;
дополняет, но не смешивается с переносимым role manifest.
_Avoid_: `harness/CAPABILITIES.json`, role manifest.

**Write role**:
Роль, которой разрешено менять production-код только в объявленной зоне: `developer`,
`database-migrations` или `messaging-integration`.
_Avoid_: architect, qa, code-review.

**Read-only role**:
Роль, создающая решение, проверку или findings без изменения production-кода: `architect`, `qa` или
`code-review`.
_Avoid_: write role.

**Batch boundary**:
Одна issue-ветка и worktree, принадлежащие одному связанному набору работ. Параллельные batch не
пересекают service, bounded context или infrastructure zone; handoff роли внутри batch последователен.
_Avoid_: общая ветка нескольких воркеров, параллельная запись в одну zone.

**Risk review gate**:
Обязательный code-review для API/public contract, миграций, outbox/очередей, транзакций,
auth/security и concurrency/retry; остальные изменения проходят review по решению автора и разработчика.
_Avoid_: review каждой правки, отсутствие review для инфраструктурной границы.

**Assignment resolution**:
Слоистое разрешение назначения `role manifest → project mapping → one-run override`; более узкий
слой переопределяет предыдущий только после проверки capability и ограничений роли.
_Avoid_: свободный выбор агента воркером, role manifest с именем модели.

**Provider profile**:
Именованная project-owned запись о доступном агенте: capability, default model, fallback и known
limitations; при включённом Orca adapter добавляется agent identifier. Роль ссылается на требуемую
capability, а не на provider profile напрямую.
_Avoid_: agent name в role manifest, глобальная таблица моделей.

**Handoff brief**:
Неизменяемый стартовый контракт batch: ticket, zone, branch/worktree, DoD, запреты и команды.
_Avoid_: поток уточнений в исходной задаче, свободный prompt.

**Completion report**:
Единственный отчёт роли о завершении: commit SHA для write work, changed files, выполненные проверки и
их результат, риски и blockers. Новая информация после dispatch требует решения coordinator-а.
_Avoid_: сообщение «готово», изменение brief задним числом.

**Quality-gate lane**:
Сериализованная очередь тяжёлых integration/quality gate; независимые write batch и read-only work
могут идти параллельно, но лимит активных batch задаётся project config после baseline.
_Avoid_: конкурентные тяжёлые gate, фиксированный лимит без замера.

**Architecture decision brief**:
Read-only результат роли `architect`: границы, варианты, выбранное решение, риски и acceptance
criteria. ADR нужен только для труднообратимого решения с существенным trade-off.
_Avoid_: implementation prompt, production change.

**QA finding**:
Воспроизводимое read-only свидетельство роли `qa` о результате независимой проверки или дефекте;
не включает изменение теста или fixture самим QA.
_Avoid_: исправление, self-authored proof.

**Two-axis review gate**:
Read-only результат роли `code-review` для high-risk batch: самостоятельные отчёты Standards и Spec,
которые не смешиваются и не заменяются одним reviewer-ом.
_Avoid_: общий рейтинг findings, один универсальный review report.

**Specialist trigger**:
Граница, при которой write work назначается `database-migrations` (schema/data) или
`messaging-integration` (outbox, message schema/routing, retry/DLQ), а не `developer`.
_Avoid_: специализация каждой сервисной правки, параллельные writers в одном batch.

**Backend orchestration capability**:
Необязательная capability `backend-orchestration`, расширяющая `pvmalove-suite` и доставляющая
role manifests, config contract, lifecycle, handoff и optional Orca adapter без изменения
существующих проектов. Практический порядок включения и запуска —
`docs/agents/backend-orchestration.md`.
_Avoid_: неявное включение orchestration, изменение базовой capability.

**Batch lifecycle**:
Coordinator-owned последовательность `planned → approved → dispatched → working → completed | blocked |
failed`. Повторная попытка — новый dispatch с новым immutable brief, а не возврат состояния назад.
_Avoid_: self-transition воркера, повторное использование старого dispatch.

**Orca adapter**:
Необязательная runtime-граница в `backend-orchestration`: после ручного approval переводит валидный
JSON brief в Orca task и isolated worker, фиксируя неизменяемую запись dispatch. Он не выбирает
scope, не утверждает запуск, не выполняет project checks и не мержит PR.
_Avoid_: обязательная Orca dependency, config-only ядро с командами Orca.

**Role common contract** (`harness/orchestration/roles/_common.md`):
Общий Markdown-контракт для всех ролей: handoff, completion report, branch/worktree, commit proof и
escalation. Индивидуальный role manifest содержит только уникальные границы, trigger и доказательства.
_Avoid_: копирование общих правил в шесть role files, глобальный AGENTS.md.

**Role metadata**:
Минимальный YAML frontmatter role manifest: `name`, `mode`, `required_capabilities`, `risk_triggers`.
Он служит validator-у и dispatcher-у; поведение роли остаётся в Markdown-body.
_Avoid_: provider/model в role file, business logic во frontmatter.

**Role assignment plan**:
Упорядоченный список совместимых provider profile ID для роли в project config: первый default,
остальные fallback. Фактически выбранный профиль записывается в immutable brief.
_Avoid_: неупорядоченный пул, скрытый fallback runtime-а.

**Manifest authority**:
Принцип, по которому project config выбирает provider, budget и stack-команды, но не может расширить
write-zone, отменить proof или снять risk-gate переносимого role manifest.
_Avoid_: project override правил роли, runtime policy вместо manifest.

**Skill discovery roots**:
`.agents/skills` и `.claude/skills` — runtime-ссылки на `.harness/skills`; для Hermes Agent fallback
маршрутом служат `AGENTS.md` и `.harness/skills/REGISTRY.md`.
