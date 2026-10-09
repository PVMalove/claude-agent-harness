# Диаграммы харнесса

Двадцать семь автономных интерактивных HTML-диаграмм. Рядом с каждой лежит редактируемая спецификация
Archify (`*.json`), а в `previews/` — статичный PNG той же диаграммы для Markdown, который не умеет
рендерить HTML (например, README на GitHub).

| Диаграмма | О чём |
|---|---|
| [Пайплайн доставки](./delivery-pipeline.workflow.html) | Полный маршрут от идеи до merge: `/grill-with-docs` → `/to-spec` → `/to-tickets` → `/implement` → `/to-pull-requests`, с ветками `hitl` (`/to-guide`) и коротким `/fast-implement`. |
| [Навигация по справочнику](./harness-guide-navigation.workflow.html) | Установка, выбор capability, работа над задачей и команды проверки. |
| [Discovery Pipeline](./discovery-pipeline.workflow.html) | Explicit opt-in `Live Artifact` → `Relevant Files` → ticket-specific Path inventory → один cheap advisory → LLM-free Context Package. |
| [Конвейер `/implement`](./implement-pipeline.workflow.html) | Пять гейтов одного тикета: архитектор → approve → разработчик → code review → approve → QA (с циклом на исправления) → итоговый отчёт → публикация. |
| [Маршруты восстановления `/implement`](./recovery-routes.workflow.html) | Как решение coordinator по отчёту выбирает маршрут из закрытого набора `RECOVERY_ROUTES` по триггеру: дефект кода, пробел в требованиях, база, инструмент, обход блокировки, неполные пункты, инфраструктура, тупик. Схема показывает, тратит ли маршрут `max_developer_retries` и кто его утверждает. |
| [Автоматический путь `approval_policy: auto`](./auto-approval.workflow.html) | Путь от `batch approve` до принятого publish без человека: согласования `policy:auto`, решения `batch auto-decide` и закрытый список остановок в порядке integrity → budget → route. Итоговый отчёт `batch auto-report` получает человек; PR он открывает сам, auto-merge запрещён. |
| [Резолв runtime и dispatch](./backend-runtime.workflow.html) | Как назначение роли превращается в immutable brief, как выбирается транспорт (`external` или `in-process`) и как dispatch подтверждает свою модель и живость. |
| [Жизненный цикл batch](./backend-batch.lifecycle.html) | Состояния batch: `planned → awaiting-approval ↔ active → completed`, плюс выходы `blocked` и `failed`. Соседняя Discovery/implement схема и operational docs описывают checkpoint/resume и base gate. |
| [QA и создание PR](./qa-call-path.workflow.html) | Где `test_summary.py` вызывается в `/qa-gate`, какие QA-маршруты обходят обёртку и как явное подтверждение приводит к `gh`/`glab pr create`. |
| [Продолжение PR](./pr-continuation.workflow.html) | Маршрут `/to-pull-requests` после принятого publish: `integration prepare` → `status` → `next` → подтверждение пары SHA → PR → `collect-ci` или запасной `local-qa` → `handoff` и ручной merge. Ветки `refresh`/`resolve` с conflict-resolver и остановки `unavailable`, `resolver-open`, `route-failure`, `human-decision`. |
| [Архитектура переносимого harness](./harness-topology.architecture.html) | **Architecture:** границы исходного harness и целевого проекта, capability-каталог, CLI, единый snapshot и runtime discovery. |
| [Швы модулей harness](./harness-seams.architecture.html) | **Architecture:** модули harness и их связи после рефакторинга: CLI, coordinator и workflow, LifecycleLedger, QA lane, Context Builder, внешние git, gh и runtime adapter. Схема показывает общее ядро ошибок и вызовы с таймаутом и без него. |
| [Gated dispatch `/implement`](./implement-dispatch.sequence.html) | **Sequence:** участники и порядок взаимодействий: brief, approvals, candidate SHA, review, clean-room QA и публикация. |
| [Поток capability](./capability-delivery.dataflow.html) | **Data Flow:** происхождение capability и skills от каталога/vendor/overrides до snapshot и runtime consumers. |
| [Построение Repo Map](./repo-map-build.sequence.html) | **Sequence:** вход, кэш, проверка и offline-установка parser bundle, разбор в изолированном worker, граф и бюджет. |
| [Компоненты Repo Map](./repo-map-components.architecture.html) | **Architecture:** `repo_map.py`, контракт, `parser_bundle.py`, tree-sitter worker, registry, кэш и потребители (Context Builder, Coordinator). |
| [Поток памяти проекта](./project-memory.dataflow.html) | **Data Flow:** источники и политика памяти (`memory.enabled`, `memory_policy`, `allowed_paths`), writer'ы `build`/`rebuild`/`sync` под общей блокировкой в главном checkout, общий индекс SQLite FTS5, читатели и потребители. Context Package с изменённым замороженным источником не переиспользуется (#636). |
| [Изоляция процессов и файлов](./process-isolation.architecture.html) | **Architecture:** backend batches в worktrees, отдельный tree-sitter worker и sandbox для временных файлов. |
| [Выбор команды установки](./harness-install-choice.workflow.html) | `init`, `adopt`, `diff` или `update` в зависимости от `harness.lock` и занятых имён скиллов; итог — `harness health`. |
| [`/to-spec`](./to-spec-flow.workflow.html) | Две фазы: seam'ы и integration-ветка на подтверждение, затем черновик в `docs/tasks/`, эпик в трекере и ветка. |
| [Карта `/wayfinder`](./wayfinder-map.workflow.html) | Chart the map и Work through the map: destination, фронтир, туман, тикеты-вопросы, `Decisions so far` → `/to-spec`. |
| [`/fast-implement`](./fast-implement.workflow.html) | Pre-flight, Coding с вопросами о ревью и push, передача на `/to-pull-requests`; остановки `hitl` и блокеров. |
| [Цикл `/tdd`](./tdd-loop.lifecycle.html) | **Lifecycle:** Red → Green → Refactor и выход, когда требования слайса покрыты. |
| [Метки `status::*`](./triage-labels.lifecycle.html) | **Lifecycle:** путь тикета по меткам триажа и кто их ставит. |
| [Пример: эпик через `/implement`](./example-epic-afk.workflow.html) | CSV-экспорт: `/grill-with-docs` → `/to-spec` → `/to-tickets` → `/implement` → `/to-pull-requests`, разблокировка второго тикета. |
| [Пример: `/wayfinder`](./example-wayfinder-oauth.workflow.html) | Переход на внешний OAuth: карта #200, research- и grilling-тикеты, сессии по тикетам, передача на `/to-spec`. |
| [Вертикальные слайсы](./vertical-slices.workflow.html) | Почему `/to-tickets` режет работу на проверяемые слайсы, а не на горизонтальные слои. |

## Как обновлять

Диаграммы собирает скилл [archify](https://github.com/tt-a1i/archify), и все схемы собраны его
последним стабильным релизом 3.0.1. Правьте только `*.json` (в `meta.output` указан
`docs/diagrams/<spec>.html`), после чего одна команда из корня репозитория перегенерирует и
проверяет диаграмму:

```bash
ARCHIFY_CHROME=<путь к Chrome/Chromium> \
  node <archify>/bin/archify.mjs finalize <type> docs/diagrams/<spec>.json docs/diagrams/<spec>.html \
  --repo-root . --quality showcase --json
```

Если Chrome не запускается в своей песочнице, добавьте в окружение `ARCHIFY_CHROME_NO_SANDBOX=1`.

`<type>` — `workflow`, `architecture`, `sequence`, `dataflow` или `lifecycle`, в зависимости от
смысла схемы. `finalize` последовательно проходит `validate`, `deliver`, `check` и `browser-check` и
обязана завершиться `status: pass`. Квитанции (`*.finalize*.json`, `*.delivery.json`,
`*.browser-check.json`) в репозиторий не коммитятся.

PNG в `previews/` — светлый снимок HTML шириной 1440 px: диаграмма вместе с блоками пояснений под
ней, без блока «Node index», который Archify 3.0 добавляет ниже (окно расширяется до нижнего края
блоков пояснений перед снимком). После перегенерации HTML обновите
снимок, иначе README покажет устаревшую картинку. Справочник [`harness-guide.md`](../harness-guide.md)
ссылается на эти же файлы, и `scripts/verify.py` проверяет, что каждая такая ссылка существует.

Содержимое диаграмм ведётся на русском. Интерфейс самого просмотрщика (`Light`/`Dark`, `Present`,
`Export`, `Legend`) и подписи легенды в lifecycle остаются английскими: это фиксированный UI
рендерера, он не переводится и не влияет на семантику диаграммы.
