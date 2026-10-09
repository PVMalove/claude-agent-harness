<p align="center">
  <img src="./docs/assets/harness-banner.svg" alt="Agent Harness — for coding agents" width="800">
</p>

<p align="center">
  <a href="https://github.com/PVMalove/claude-agent-harness/releases"><img src="https://img.shields.io/github/v/release/PVMalove/claude-agent-harness?label=Version&labelColor=34312E&color=D97757" alt="Version"></a>
  <img src="https://img.shields.io/badge/Runtime-Claude%20Code%20%7C%20Codex-E5A04B?labelColor=34312E" alt="Runtime: Claude Code | Codex">
  <img src="https://img.shields.io/badge/Python-3.12%2B-8FB573?labelColor=34312E" alt="Python 3.12+">
  <a href="https://github.com/PVMalove/claude-agent-harness/actions/workflows/verify.yml"><img src="https://github.com/PVMalove/claude-agent-harness/actions/workflows/verify.yml/badge.svg" alt="CI"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-MIT-9C958C?labelColor=34312E" alt="License: MIT"></a>
</p>

# Agent Harness

## Портативный фреймворк для оркестрации ИИ-агентов (Claude Code, Codex).

Harness — это единый набор скиллов, системных правил, hooks и контекстных документов, который одна команда разворачивает в любой целевой проект. Установка создаёт проверяемый независимый snapshot (capability): он работает через нативные механизмы агентов и живёт в проекте автономно, без привязки к исходному репозиторию.

Совместимость и стабильность полностью проверены для `Claude Code` и `Codex`.

### Доступные сборки (Capabilities)
При инициализации вы можете выбрать одну из готовых сборок; каждая сборка задаёт поведение агента:

 - project-foundation (По умолчанию) — лёгкая доменно-нейтральная база из 5 ключевых скиллов для старта.
 - mattpocock-suite — полная upstream-сборка (25 скиллов от Matt Pocock / aihero.dev), жестко привязанная к проверенному коммиту.
 - pvmalove-suite — продвинутый инженерный workflow. Сборка добавляет нативную привязку эпиков и тикетов, изолированную (namespaced) таксономию для триажа задач и двухосевое review. Она также добавляет генерацию PR через /to-pull-requests, настраиваемый язык вывода и строгий контроль качества через qa-gate.
 - backend-orchestration — специализированная сборка для распределенных систем. Сборка работает через систему специализированных ролей (Архитектор, Разработчик, QA и др.). Она оркестрирует эти роли для совместного проектирования микросервисов, DDD, CQRS и сложных паттернов взаимодействия (Sagas через MassTransit, Outbox/Inbox).

CONTEXT.md определяет архитектурные термины. Полное руководство по интеграции доступно в docs/.

## Архитектура

`harness/CAPABILITIES.json` задаёт capability `project-foundation`, `mattpocock-suite`,
`pvmalove-suite` и опциональную `backend-orchestration`. `harness init` разрешает выбранные скиллы
из закреплённых каталогов `skills/vendor/` и `skills/first-party/`. Команда копирует их вместе с
ресурсами в `.harness/` проекта и записывает lock. Корневой `docs/` и внутренние документы относятся
к исходному репозиторию; установщик их не переносит. Установщик устанавливает проектные руководства,
hooks, агентов и конфигурацию из отдельных шаблонов `harness/project/`.

В `pvmalove-suite` переопределены в `skills/first-party/pvmalove/`: `to-spec`, `to-tickets`, `implement`, `ask-matt`, `code-review`, `grilling`, `grill-me`, `grill-with-docs`, `triage`, `wayfinder`, `diagnosing-bugs`; доп. скиллы: `qa-gate`, `to-guide`, `setup-labels`, `to-pull-requests`, `fast-implement`, `delivery-stats`, `architect`.

[![Граница исходного харнесса и целевого проекта](./docs/diagrams/previews/harness-topology.architecture.png)](./docs/diagrams/harness-topology.architecture.html)

`/implement` проводит один тикет через этапы architect, developer, независимое review, clean-room QA
и publish. Coordinator фиксирует Context Package и candidate SHA, проверяет актуальность базы и
привязывает approval к digest конкретного перехода. PR требует отдельного подтверждения; разработчик
выполняет merge. После проверки `/to-pull-requests` готовит отдельный PR по правилам целевого
проекта.

[![Конвейер implement](./docs/diagrams/previews/implement-pipeline.workflow.png)](./docs/diagrams/implement-pipeline.workflow.html)

Backend batches выполняются в отдельных worktrees, а роли обмениваются только неизменяемыми briefs и
reports. Tree-sitter parser работает в отдельном worker-процессе. Временные файлы лежат в
`.harness/.sandboxes/`: inbox ролей — в `scratch/`, тела PR и комментарии — в `pr_body/`.

[![Изоляция процессов и временных файлов](./docs/diagrams/previews/process-isolation.architecture.png)](./docs/diagrams/process-isolation.architecture.html)

[ADR](./docs/adr/) описывают действующие контракты, а [справочник](./docs/harness-guide.md)
и [правила Git](./docs/agents/git-workflow.md) — операционные процедуры.

## Скиллы

Скиллы вызываются в сессии агента командой `/имя` (Claude Code) или по имени (Codex). Capability
определяет состав скиллов:

- `project-foundation` — базовые `grilling`, `handoff`, `writing-for-agents`, `research`,
  `domain-modeling`;
- `mattpocock-suite` — полный закреплённый upstream-набор;
- `pvmalove-suite` — upstream-набор с переопределениями и дополнениями, перечисленными ниже;
- `backend-orchestration` — `pvmalove-suite`, дополненный coordinator и ролями.

[docs/skills/](./docs/skills/README.md) содержит подробные описания на русском языке.

### Собственные скиллы `pvmalove-suite`

| Скилл | Назначение |
|---|---|
| [`ask-matt`](./docs/skills/ask-matt.md) | Определяет скилл или сценарий, подходящий для ситуации; маршрутизатор по установленным скиллам. |
| [`architect`](./docs/skills/architect.md#интерактивный-скилл-architect) | Вручную сравнивает архитектурные варианты и прецеденты, возвращает brief решения; при включённой памяти использует read-only поиск. |
| [`diagnosing-bugs`](./docs/skills/diagnosing-bugs.md) | Диагностирует сложные дефекты и регрессии; при включённой памяти ищет прошлые фиксы перед проверкой гипотез. |
| [`grilling`](./docs/skills/grilling.md) | Базовый механизм интервью: последовательно уточняет план, решение или идею до выявления пробелов. |
| [`grill-me`](./docs/skills/grill-me.md) | Интервью для уточнения плана или дизайна. |
| [`grill-with-docs`](./docs/skills/grill-with-docs.md) | Интервью с ведением ADR и глоссария по ходу обсуждения. |
| [`wayfinder`](./docs/skills/wayfinder.md) | Планирует работу, превышающую объём одной сессии, в виде карты тикетов-решений в трекере и обрабатывает их по одному. |
| [`triage`](./docs/skills/triage.md) | Проводит issues и внешние PR по машине состояний триажа: классификация, проверка, подготовка brief для агентов. |
| [`to-spec`](./docs/skills/to-spec.md) | Формирует спецификацию по итогам обсуждения и публикует её в трекер без повторного интервью. |
| [`to-tickets`](./docs/skills/to-tickets.md) | Декомпозирует план или спецификацию на вертикальные тикеты с блокирующими связями и публикует их. |
| [`to-guide`](./docs/skills/to-guide.md) | Готовит по `hitl`-тикету пошаговое руководство с промптами для AI IDE, если реализацию выполняет разработчик. |
| [`implement`](./docs/skills/implement.md) | Проводит тикет через этапы architect, developer, review, clean-room QA и publish с подтверждениями разработчика. |
| [`fast-implement`](./docs/skills/fast-implement.md) | Выполняет реализацию в одной сессии, без гейтов подтверждения в конвейере coordinator. |
| [`code-review`](./docs/skills/code-review.md) | Проверяет изменения от фиксированной точки по двум осям: стандарты репозитория и соответствие спецификации. |
| [`qa-gate`](./docs/skills/qa-gate.md) | Запускает полный локальный гейт качества из `.harness/project.json` и сообщает результат pass/fail. |
| [`to-pull-requests`](./docs/skills/to-pull-requests.md) | Готовит и открывает PR для опубликованной issue-ветки по правилам проекта. |
| [`setup-labels`](./docs/skills/setup-labels.md) | Создаёт или обновляет метки репозитория по `docs/agents/triage-labels.md`; выполняется однократно. |
| [`delivery-stats`](./docs/skills/delivery-stats.md) | Собирает статистику завершённого эпика: токены по моделям, использование кэша, стоимость, окно подписки, объём кода. |

### Закреплённые upstream-скиллы `mattpocock-suite`

`pvmalove-suite` заменяет скиллы, отмеченные знаком *, собственными версиями из таблицы выше.

| Скилл | Назначение |
|---|---|
| [`ask-matt`](./docs/skills/vendor/ask-matt.md)* | Маршрутизатор: определяет скилл или сценарий, подходящий для ситуации. |
| [`code-review`](./docs/skills/vendor/code-review.md)* | Двухосевая проверка изменений: стандарты и соответствие спецификации. |
| [`codebase-design`](./docs/skills/vendor/codebase-design.md) | Общий словарь для проектирования «глубоких» модулей, швов и тестируемых интерфейсов. |
| [`diagnosing-bugs`](./docs/skills/vendor/diagnosing-bugs.md)* | Цикл диагностики сложных дефектов и регрессий производительности. |
| [`domain-modeling`](./docs/skills/vendor/domain-modeling.md) | Формирует и уточняет доменную модель: терминологию и архитектурные решения. |
| [`grill-me`](./docs/skills/vendor/grill-me.md)* | Интервью для уточнения плана или дизайна. |
| [`grill-with-docs`](./docs/skills/vendor/grill-with-docs.md)* | Интервью с параллельным ведением ADR и глоссария. |
| [`grilling`](./docs/skills/vendor/grilling.md)* | Последовательное уточнение плана, решения или идеи. |
| [`handoff`](./docs/skills/vendor/handoff.md) | Сводит текущее обсуждение в документ передачи для другого агента. |
| [`implement`](./docs/skills/vendor/implement.md)* | Выполняет реализацию по спецификации или набору тикетов. |
| [`improve-codebase-architecture`](./docs/skills/vendor/improve-codebase-architecture.md) | Выявляет возможности углубления модулей, формирует HTML-отчёт и прорабатывает выбранный вариант. |
| [`prototype`](./docs/skills/vendor/prototype.md) | Создаёт временный прототип для ответа на вопрос дизайна: модель состояний, логика, UI. |
| [`research`](./docs/skills/vendor/research.md) | Исследует вопрос по первоисточникам и сохраняет выводы Markdown-файлом в репозитории. |
| [`resolving-merge-conflicts`](./docs/skills/vendor/resolving-merge-conflicts.md) | Разрешает конфликты незавершённого merge или rebase. |
| [`setup-matt-pocock-skills`](./docs/skills/vendor/setup-matt-pocock-skills.md) | Однократно настраивает репозиторий для инженерных скиллов: трекер, метки, структуру доменных документов. |
| [`tdd`](./docs/skills/vendor/tdd.md) | Разработка через тестирование: red-green-refactor и интеграционные тесты. |
| [`teach`](./docs/skills/vendor/teach.md) | Объясняет пользователю новый навык или понятие в рабочем пространстве. |
| [`to-questionnaire`](./docs/skills/vendor/to-questionnaire.md) | Преобразует вопрос, требующий внешнего ответа, в опросник для другого участника. |
| [`to-spec`](./docs/skills/vendor/to-spec.md)* | Формирует спецификацию по итогам обсуждения и публикует её в трекер. |
| [`to-tickets`](./docs/skills/vendor/to-tickets.md)* | Декомпозирует план на тикеты с блокирующими связями. |
| [`triage`](./docs/skills/vendor/triage.md)* | Машина состояний триажа issues и внешних PR. |
| [`wait-what`](./docs/skills/vendor/wait-what.md) | Повторно излагает последний ответ агента в другой формулировке. |
| [`wayfinder`](./docs/skills/vendor/wayfinder.md)* | Карта тикетов-решений для работы, превышающей объём одной сессии. |
| [`wizard`](./docs/skills/vendor/wizard.md) | Генерирует интерактивный bash-мастер для шагов, которые выполняет только человек. |
| [`writing-for-agents`](./docs/skills/vendor/writing-for-agents.md) | Правила подготовки документов для агентов: скиллов, `AGENTS.md`, `CLAUDE.md`. |

### Глобальные скиллы

Команда `bin/install-global.py` устанавливает их однократно для машины (см. ниже).

| Скилл | Назначение |
|---|---|
| [`start-project`](./docs/skills/start-project.md) | Формирует проект на основе идеи, определяет необходимость репозитория, создаёт, проверяет или обновляет его харнесс. |
| [`integrate-project`](./docs/skills/integrate-project.md) | Проводит аудит существующей кодовой базы и встраивает в неё харнесс с сохранением действующих соглашений и CI. |

## Быстрый старт и установка

Требуются Python 3.12 или новее и Git. Глобальный слой устанавливается однократно для выбранных
runtime. Параметр `--runtime` допускает повторение:

```bash
python3 bin/install-global.py --target-home "$HOME" --runtime codex --runtime claude
```

В PowerShell:

```powershell
python bin\install-global.py --target-home $HOME --runtime codex --runtime claude
```

Далее `start-project` создаёт новый проект, а `integrate-project` подключает харнесс к
существующему. Прямая установка в Git-репозиторий:

```bash
python3 harness/bin/harness.py init /path/to/repository --capability pvmalove-suite \
  --project-type software --stack python --base-branch main --language ru \
  --qa-gate-command "make test"
python3 harness/bin/harness.py health /path/to/repository
```

В PowerShell задайте путь и команды аналогично:

```powershell
python harness\bin\harness.py init C:\path\to\repository --capability pvmalove-suite `
  --project-type software --stack python --base-branch main --language ru `
  --qa-gate-command "python -m pytest"
python harness\bin\harness.py health C:\path\to\repository
```

Без `--capability` команда устанавливает доменно-нейтральную `project-foundation`.
`mattpocock-suite` даёт полный закреплённый upstream-набор, а `backend-orchestration` добавляет
coordinator и роли поверх `pvmalove-suite`. `harness diff` показывает изменения управляемого
snapshot, `harness update` обновляет его и сохраняет локальные правки.

`harness uninstall` полностью удаляет харнесс из проекта. Без `--apply` команда только показывает
план; с `--apply --confirm UNINSTALL` она удаляет `.harness/`, discovery-ссылки, seed-файлы и строки
харнесса в `.gitignore`, предварительно скопировав изменённые проектом файлы в
`.harness-uninstall-backup/`. Команды и параметры описаны в [справочнике](./docs/harness-guide.md).

Установщик переносит в целевой проект только выбранные ресурсы `harness/`, скиллы и шаблоны
`harness/project/`. Документация для разработчика — корневой `docs/`, справочник харнесса
[`harness-guide.md`](./docs/harness-guide.md) и руководство по backend-оркестрации
[`backend-orchestration.md`](./docs/backend-orchestration.md) — остаётся в исходном репозитории и в
целевой проект не попадает. Агентам доставляются только их собственные контракты:
`.harness/docs/project-memory.md` (контракт интерактивного поиска по памяти) и
`.harness/docs/technical-english.md`, а при `pvmalove-suite` и `backend-orchestration` ещё и шаблоны
проектных руководств из `harness/project/docs-agents/`, которые разворачиваются как
`docs/agents/{artifacts,git-workflow,issue-tracker,triage-labels,worktrees}.md`. `harness update`
обновляет эти файлы. Для существующего проекта следуйте
[инструкции обновления и включения памяти](./docs/harness-guide.md#обновление-существующего-проекта-и-включение-памяти):
обновление сохраняет `project.json`, поэтому schema и opt-in поля памяти нужно перенести отдельно.

При любой capability установщик ставит общий
[контракт технического английского](./harness/docs/technical-english.md) как
`.harness/docs/technical-english.md` и обновляет его в составе управляемого snapshot. `diff` и
`update` показывают отдельный unified diff недостающих обязательных ссылок для существующих
`AGENTS.md`, `CLAUDE.md` и установленных agent seeds. Команды сохраняют пользовательские инструкции.
После просмотра и явного согласования человек или уполномоченный агент применяет только эти
добавления обычным редактированием или patch. Подключённые входы требуют прочитать общий контракт
до первого English handoff. Повторное обновление не предлагает уже подключённые ссылки.

## Проверка состояния проекта: `harness health`

`health` проверяет установку харнесса в репозитории и без флагов не вносит изменений. Рекомендуем
выполнять команду после `init`/`update`, после клонирования проекта, а также когда агент не
обнаруживает скиллы.

```bash
python3 harness/bin/harness.py health /path/to/repository            # локальные проверки
python3 harness/bin/harness.py health /path/to/repository --fix      # восстановление заготовок .harness
python3 harness/bin/harness.py health /path/to/repository --online   # дополнительно проверки трекера
python3 harness/bin/harness.py health /path/to/repository --json     # машиночитаемый отчёт
```

Отчёт состоит из групп. Каждая строка имеет маркер `✅` ok, `⚠️` warn, `❌` fail или `-` skipped:

| Группа | Проверяемые объекты |
|---|---|
| `files` | `harness.lock`, `AGENTS.md`, discovery-ссылки `.agents/skills` и `.claude/skills`, `.harness/project.json`, overlay-локи, интеграции, конфигурация оркестрации. |
| `directories` | Каталоги `.harness/` и `.harness/.sandboxes/*`; отсутствующий каталог, который можно создать, получает статус `ok`. |
| `repo_map` | Уровень Repo Map (`full` или `minimal`) и причина снижения уровня. |
| `environment` | ОС, Git и `user.name`/`user.email`, `.gitattributes` и переводы строк, Python ≥ 3.12, `uv`, кодировка вывода; на Windows — длинные пути, symlink и `bash` для hooks. |
| `orchestration` | Только при `backend-orchestration`: ledger, незавершённые и заблокированные batch, dispatch без активности, неучтённые worktree, объём временных данных. |
| `tracker` | Только с `--online`: авторизация `gh`/`glab`, доступность origin, права и метки. |

Интерпретация результата:

- Код выхода `1` означает, что хотя бы одна проверка имеет статус `fail`. `warn` и `skipped` не
  влияют на код выхода.
- Под строкой с проблемой отчёт выводит `-> Как исправить:` и, если есть, команду с абсолютными
  путями. Эту команду можно выполнить из любого каталога.
- `--fix` создаёт недостающие каталоги `.harness` и пересобирает `.harness/skills/REGISTRY.md`.
  Этот флаг не изменяет системные настройки, git config, права доступа и worktree — для них отчёт
  приводит команду.
- `--online --fix` дополнительно создаёт отсутствующие метки трекера. Флаги не изменяют цвет
  существующих меток.
- `--json` возвращает контракт `schema_version: 1` со стабильными `checks[].id` (например,
  `files.lock`).

Полное описание проверок — в [справочнике](./docs/harness-guide.md#шаг-2--команды-cli).

## Пульт управления: `harness console`

`console` — интерактивный терминальный пульт (TUI) для диагностики, команд харнесса, оркестрации,
отчётов и Repo Map.

```bash
python3 harness/bin/harness.py console /path/to/repository
```

Пульту нужен [`uv`](https://docs.astral.sh/uv/) в `PATH`. Пульт запускается через
`uv run --no-project --with textual==<pin>` и не изменяет зависимости целевого проекта. При первом
запуске загружается `textual`, поэтому требуется доступ к сети. При отсутствии `uv` или сети пульт
выводит причину и текстовый отчёт `harness health`.

Пульт оформлен терракотовыми и янтарными акцентами на графитовом фоне с тонкими скруглёнными
рамками. На главном экране — знак харнесса и сведения об установке: версия, capability, путь
репозитория и ветка. Ниже расположен дашборд со счётчиками проверок, числом открытых batch
оркестрации, уровнем Repo Map, версией харнесса и состоянием дрейфа snapshot; действие
«Online checks» пересчитывает счётчики с `--online`. Разделы меню:

| Раздел | Содержание |
|---|---|
| `Diagnostics` | Полный отчёт `health`, действия «online checks» и «apply fixes» (`health --fix`, требует повторного нажатия). |
| `Harness` | Состояние установки (версия, capability, скиллы, управляемые файлы, дата lock) и сводка использования конвейера; команды CLI: init, update (в том числе `--force-managed-files` и `--force-seed-files`), diff, adopt, очистка `.harness` soft/hard (план и применение), удаление харнесса из проекта (план и применение), registry, lock-project-skills, list, health, Repo Map, ledger и удаление worktree. |
| `Orchestration` | Статистика конвейера (запуски, тикеты, состояния, dispatch по ролям, итоги отчётов, QA) и история batch с фильтром по состоянию — выбор batch открывает хронологию; команды coordinator: batch, dispatch, qa status, risk assess, context-package, ledger status. |
| `Reports` | Отчёты ролей из ledger с фильтрами по тикету, роли, outcome и дате, хронология batch и QA-логи. |
| `Repo Map` | Карта репозитория для HEAD: сводка, дерево файлов с сигнатурами, поиск символов, связи, хабы. |
| `Help` | Справка по разделам, клавишам и правилам безопасности пульта. |

Клавиши: `Esc` — возврат или отмена, `e` — экспорт в Markdown, `j` — экспорт Repo Map в JSON,
`b` — хронология batch в отчёте или построение карты в `Repo Map`, `F3` — QA-логи в `Reports`,
`F1` — справка с любого экрана, `Ctrl+P` — палитра команд, `Ctrl+Q` — выход.

Для каждой команды пульт показывает CLI-эквивалент и запускает тот же CLI-процесс. Команды из
«Как исправить» он только отображает и не выполняет. Перед необратимыми действиями пульт
запрашивает подтверждение, а для `ledger reset`, hard cleanup и удаления харнесса требует ввести
`RESET`, `HARD` или `UNINSTALL`. Экспорты сохраняются в `docs/tasks/<папка тикета>/artifacts/` или
`docs/tasks/console-exports/` и никогда не перезаписывают существующие файлы.

## Релизная политика

Версии соответствуют SemVer. Новую версию задают в обычном PR: в `harness/VERSION`,
`pyproject.toml` и секции `CHANGELOG.md`. После merge в `master` [GitHub CD](./.github/workflows/release.yml)
проверяет, существует ли тег `vMAJOR.MINOR.PATCH` для этой версии. Если тега нет, CD выполняет
проверки `verify.yml`, собирает архив установки с SHA-256, создаёт тег на проверенном коммите и
публикует GitHub Release; push в `master` без смены версии Release не создаёт. Ручная публикация
тега по-прежнему поддерживается. Release Notes CD берёт из секции версии в
[CHANGELOG.md](./CHANGELOG.md) и прерывает выпуск, если секции нет. Архив и контрольная сумма
доступны в [GitHub Releases](https://github.com/PVMalove/claude-agent-harness/releases); после
загрузки их проверяет `sha256sum --check claude-agent-harness-vX.Y.Z.tar.gz.sha256`.

Workflow [`release-parser-bundle.yml`](./.github/workflows/release-parser-bundle.yml) запускают
вручную: он прикладывает дополнительный parser bundle к Release того же коммита. Полная процедура
описана в [releases.md](./docs/agents/releases.md). В целевых проектах GitLab поддерживается через
`glab` для тикетов и merge requests, а релизный CD этого репозитория работает только на GitHub.

## Структура проекта

| Путь | Назначение |
|---|---|
| `harness/` | CLI, каталог capability, runtime-модули и шаблоны целевого проекта |
| `skills/` | Закреплённый vendor-snapshot и собственные скиллы |
| `global/`, `global-skills/`, `bin/` | Глобальный профиль, стартовые скиллы и установщик |
| `scripts/` | Сборка, проверка и clean-room сценарии исходного репозитория |
| `docs/` | ADR, руководства, описания на русском языке и диаграммы Archify исходного репозитория |
| `third_party/` | Provenance, lock и лицензии upstream |
| `.github/` | CI, CD и проверки upstream |

Проект распространяется по лицензии [MIT](./LICENSE). Лицензия закреплённого upstream-snapshot
находится в [`third_party/mattpocock-skills/LICENSE`](./third_party/mattpocock-skills/LICENSE).
