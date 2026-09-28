# Agent Harness

[![CI](https://github.com/PVMalove/claude-agent-harness/actions/workflows/verify.yml/badge.svg)](https://github.com/PVMalove/claude-agent-harness/actions/workflows/verify.yml)
[![Release](https://img.shields.io/github/v/release/PVMalove/claude-agent-harness)](https://github.com/PVMalove/claude-agent-harness/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

**v1.0.0** — переносимый харнесс для coding agents. Он разрешает выбранную capability из
закреплённых skills и ресурсов, устанавливает проверяемый снимок в целевой проект и запускает
его через нативные механизмы Claude Code и Codex. Совместимость v1.0.0 проверена для
этих двух runtime.
Термины определены в [CONTEXT.md](./CONTEXT.md), подробные инструкции — в [docs/](./docs/README.md).

## Архитектура

`harness/CAPABILITIES.json` задаёт `project-foundation`, `mattpocock-suite`, `pvmalove-suite` и
опциональную `backend-orchestration`. `harness init` разрешает выбранные skills из закреплённого
`skills/vendor/` и `skills/first-party/`, копирует их вместе с ресурсами в `.harness/` проекта
и записывает lock. Корневой `docs/` и внутренние документы принадлежат только этому исходному
репозиторию: установщик их не переносит. Проектные руководства, hooks, агенты и конфигурация
устанавливаются из отдельных шаблонов `harness/project/`.

В `pvmalove-suite` переопределены в `skills/first-party/pvmalove/`: `to-spec`, `to-tickets`, `implement`, `ask-matt`, `code-review`, `grilling`, `grill-me`, `grill-with-docs`, `triage`, `wayfinder`; доп. скиллы: `qa-gate`, `to-guide`, `setup-labels`, `to-pull-requests`, `fast-implement`, `delivery-stats`.

[![Граница исходного харнесса и целевого проекта](./docs/diagrams/previews/harness-topology.architecture.png)](./docs/diagrams/harness-topology.architecture.html)

`/implement` ведёт один тикет через architect, developer, независимое review, clean-room QA и
publish. Coordinator закрепляет Context Package и candidate SHA, проверяет свежесть базы и
привязывает approval к digest конкретного перехода. PR требует отдельного подтверждения;
merge выполняет разработчик.
После проверки `/to-pull-requests` готовит отдельный PR по правилам целевого проекта.

[![Конвейер implement](./docs/diagrams/previews/implement-pipeline.workflow.png)](./docs/diagrams/implement-pipeline.workflow.html)

Backend batches работают в отдельных worktrees; роли обмениваются неизменяемыми briefs и
reports. Tree-sitter parser запускается в отдельном worker-процессе, а временные файлы
хранятся под `.harness/.sandboxes/`: inbox ролей — в `scratch/`, тела PR и комментарии —
в `pr_body/`.

[![Изоляция процессов и временных файлов](./docs/diagrams/previews/process-isolation.architecture.png)](./docs/diagrams/process-isolation.architecture.html)

Действующие контракты — в [ADR](./docs/adr/), операции — в
[руководстве](./harness/docs/harness-guide.md) и
[правилах Git](./docs/agents/git-workflow.md).

## Скилы

Скилы вызываются в сессии агента как `/имя` (Claude Code) или по имени (Codex). Состав зависит
от capability: `project-foundation` — базовые `grilling`, `handoff`, `writing-for-agents`,
`research`, `domain-modeling`; `mattpocock-suite` — весь закреплённый upstream-набор;
`pvmalove-suite` — upstream-набор с переопределениями и дополнениями ниже; `backend-orchestration`
добавляет к нему coordinator и роли. Подробные русские описания — в [docs/skills/](./docs/skills/README.md).

### Собственные скилы `pvmalove-suite`

| Скил | Что делает |
|---|---|
| [`ask-matt`](./docs/skills/ask-matt.md) | Подсказывает, какой скил или сценарий подходит к ситуации: маршрутизатор по установленным скилам. |
| [`grilling`](./docs/skills/grilling.md) | Ядро «прожарки»: жёстко допрашивает по плану, решению или идее, пока не вскроются пробелы. |
| [`grill-me`](./docs/skills/grill-me.md) | Интервью, которое доводит план или дизайн до ясности. |
| [`grill-with-docs`](./docs/skills/grill-with-docs.md) | То же интервью, но по ходу пишет ADR и глоссарий. |
| [`wayfinder`](./docs/skills/wayfinder.md) | Планирует работу крупнее одной сессии как карту тикетов-решений в трекере и закрывает их по одному. |
| [`triage`](./docs/skills/triage.md) | Ведёт issues и внешние PR по машине состояний триажа: классифицирует, проверяет, пишет брифы для агентов. |
| [`to-spec`](./docs/skills/to-spec.md) | Превращает обсуждение в спецификацию и публикует её в трекер, без нового интервью. |
| [`to-tickets`](./docs/skills/to-tickets.md) | Режет план или спецификацию на вертикальные тикеты с блокирующими связями и публикует их. |
| [`to-guide`](./docs/skills/to-guide.md) | Делает из `hitl`-тикета пошаговое руководство с готовыми промптами для AI IDE, если код пишет человек. |
| [`implement`](./docs/skills/implement.md) | Ведёт тикет через architect, developer, review, clean-room QA и publish с подтверждениями разработчика. |
| [`fast-implement`](./docs/skills/fast-implement.md) | Реализует задачу в одной сессии, без гейтов подтверждения конвейера coordinator. |
| [`code-review`](./docs/skills/code-review.md) | Ревьюит изменения от фиксированной точки по двум осям — стандарты репозитория и соответствие спеке. |
| [`qa-gate`](./docs/skills/qa-gate.md) | Запускает полный локальный гейт качества из `.harness/project.json` и сообщает pass/fail. |
| [`to-pull-requests`](./docs/skills/to-pull-requests.md) | Готовит и открывает PR для уже запушенной issue-ветки по правилам проекта. |
| [`setup-labels`](./docs/skills/setup-labels.md) | Создаёт или обновляет метки репозитория по `docs/agents/triage-labels.md`; запускается один раз. |
| [`delivery-stats`](./docs/skills/delivery-stats.md) | Собирает статистику завершённого эпика: токены по моделям, кэш, стоимость, окно подписки, объём кода. |

### Закреплённые upstream-скилы `mattpocock-suite`

Имена, отмеченные *, в `pvmalove-suite` заменены собственными версиями из таблицы выше.

| Скил | Что делает |
|---|---|
| [`ask-matt`](./docs/skills/vendor/ask-matt.md)* | Маршрутизатор: какой скил или сценарий подходит к ситуации. |
| [`code-review`](./docs/skills/vendor/code-review.md)* | Двухосевое ревью изменений: стандарты и соответствие спеке. |
| [`codebase-design`](./docs/skills/vendor/codebase-design.md) | Общий словарь для проектирования «глубоких» модулей, швов и тестируемых интерфейсов. |
| [`diagnosing-bugs`](./docs/skills/vendor/diagnosing-bugs.md) | Цикл диагностики сложных багов и регрессий производительности. |
| [`domain-modeling`](./docs/skills/vendor/domain-modeling.md) | Строит и уточняет доменную модель: терминологию и архитектурные решения. |
| [`grill-me`](./docs/skills/vendor/grill-me.md)* | Интервью, доводящее план или дизайн до ясности. |
| [`grill-with-docs`](./docs/skills/vendor/grill-with-docs.md)* | Интервью с параллельной записью ADR и глоссария. |
| [`grilling`](./docs/skills/vendor/grilling.md)* | Жёсткий допрос по плану, решению или идее. |
| [`handoff`](./docs/skills/vendor/handoff.md) | Сжимает текущий разговор в документ передачи для другого агента. |
| [`implement`](./docs/skills/vendor/implement.md)* | Реализует работу по спецификации или набору тикетов. |
| [`improve-codebase-architecture`](./docs/skills/vendor/improve-codebase-architecture.md) | Ищет места для углубления модулей, выдаёт HTML-отчёт и разбирает выбранный вариант. |
| [`prototype`](./docs/skills/vendor/prototype.md) | Строит одноразовый прототип, чтобы ответить на вопрос дизайна: модель состояний, логика, UI. |
| [`research`](./docs/skills/vendor/research.md) | Исследует вопрос по первоисточникам и сохраняет выводы Markdown-файлом в репозитории. |
| [`resolving-merge-conflicts`](./docs/skills/vendor/resolving-merge-conflicts.md) | Разрешает конфликты незавершённого merge или rebase. |
| [`setup-matt-pocock-skills`](./docs/skills/vendor/setup-matt-pocock-skills.md) | Один раз настраивает репозиторий под инженерные скилы: трекер, метки, раскладку доменных документов. |
| [`tdd`](./docs/skills/vendor/tdd.md) | Разработка через тесты: red-green-refactor и интеграционные тесты. |
| [`teach`](./docs/skills/vendor/teach.md) | Обучает пользователя новому навыку или понятию прямо в рабочем пространстве. |
| [`to-questionnaire`](./docs/skills/vendor/to-questionnaire.md) | Превращает вопрос, на который нельзя ответить самому, в опросник для другого человека. |
| [`to-spec`](./docs/skills/vendor/to-spec.md)* | Синтезирует обсуждение в спецификацию и публикует её в трекер. |
| [`to-tickets`](./docs/skills/vendor/to-tickets.md)* | Режет план на тикеты с блокирующими связями. |
| [`triage`](./docs/skills/vendor/triage.md)* | Машина состояний триажа issues и внешних PR. |
| [`wait-what`](./docs/skills/vendor/wait-what.md) | Останавливается и заново объясняет последнее сообщение, которое не было понято. |
| [`wayfinder`](./docs/skills/vendor/wayfinder.md)* | Карта тикетов-решений для работы крупнее одной сессии. |
| [`wizard`](./docs/skills/vendor/wizard.md) | Генерирует интерактивный bash-мастер для шагов, которые может выполнить только человек. |
| [`writing-for-agents`](./docs/skills/vendor/writing-for-agents.md) | Правила написания документов для агентов: скилов, `AGENTS.md`, `CLAUDE.md`. |

### Глобальные скилы

Ставятся один раз на машину командой `bin/install-global.py` (см. ниже).

| Скил | Что делает |
|---|---|
| [`start-project`](./docs/skills/start-project.md) | Начинает проект с идеи, решает, нужен ли репозиторий, создаёт, проверяет или обновляет его харнесс. |
| [`integrate-project`](./docs/skills/integrate-project.md) | Проводит аудит существующей кодовой базы и встраивает в неё харнесс, не ломая текущие соглашения и CI. |

## Быстрый старт и установка

Нужны Python 3.12 или новее и Git. Глобальный слой ставится один раз для выбранных runtime;
`--runtime` можно повторять:

```bash
python3 bin/install-global.py --target-home "$HOME" --runtime codex --runtime claude
```

В PowerShell:

```powershell
python bin\install-global.py --target-home $HOME --runtime codex --runtime claude
```

После этого `start-project` помогает создать новый проект, а `integrate-project` — включить
харнесс в существующий. Для прямой установки в Git-репозиторий:

```bash
python3 harness/bin/harness.py init /path/to/repository --capability pvmalove-suite \
  --project-type software --stack python --base-branch main --language ru \
  --qa-gate-command "make test"
python3 harness/bin/harness.py health /path/to/repository
```

В PowerShell путь и команды задаются так же:

```powershell
python harness\bin\harness.py init C:\path\to\repository --capability pvmalove-suite `
  --project-type software --stack python --base-branch main --language ru `
  --qa-gate-command "python -m pytest"
python harness\bin\harness.py health C:\path\to\repository
```

Без `--capability` устанавливается доменно-нейтральная `project-foundation`. Для полного
закреплённого upstream-набора выберите `mattpocock-suite`; `backend-orchestration` добавляет
координатор и роли поверх `pvmalove-suite`. `harness diff` показывает изменения управляемого
снимка, `harness update` обновляет его с сохранением локальных правок. Команды и варианты
параметров приведены в [руководстве](./harness/docs/harness-guide.md). В целевой проект
попадают только выбранные ресурсы `harness/`, skills и шаблоны `harness/project/`;
корневой `docs/` служит документацией этого репозитория.
Шаблоны проектных руководств при `pvmalove-suite` и `backend-orchestration` разворачиваются
из `harness/project/docs-agents/` как `docs/agents/{artifacts,git-workflow,issue-tracker,triage-labels,worktrees}.md`.
Руководства по харнессу и backend-оркестрации входят в управляемый снимок: `harness/docs/` устанавливается
в `.harness/docs/{harness-guide,backend-orchestration}.md` и обновляется командой `harness update`.

## Здоровье проекта: `harness health`

`health` проверяет установку харнесса в репозитории и ничего не меняет без флагов. Запускайте его
после `init`/`update`, после клонирования проекта и когда скилы не видны агенту.

```bash
python3 harness/bin/harness.py health /path/to/repository            # локальные проверки
python3 harness/bin/harness.py health /path/to/repository --fix      # починить заготовки .harness
python3 harness/bin/harness.py health /path/to/repository --online   # плюс проверки трекера
python3 harness/bin/harness.py health /path/to/repository --json     # машиночитаемый отчёт
```

Отчёт сгруппирован, каждая строка помечена `✅` ok, `⚠️` warn, `❌` fail или `-` skipped:

| Группа | Что проверяет |
|---|---|
| `files` | `harness.lock`, `AGENTS.md`, discovery-ссылки `.agents/skills` и `.claude/skills`, `.harness/project.json`, overlay-локи, интеграции, конфиг оркестрации. |
| `directories` | Каталоги `.harness/` и `.harness/.sandboxes/*`: отсутствующий, но создаваемый каталог — ok. |
| `repo_map` | Уровень Repo Map (`full` или `minimal`) и причину деградации. |
| `environment` | ОС, Git и `user.name`/`user.email`, `.gitattributes` и переводы строк, Python ≥ 3.12, `uv`, кодировку вывода; на Windows — длинные пути, symlink и `bash` для хуков. |
| `orchestration` | Только при `backend-orchestration`: леджер, незавершённые и заблокированные batch, зависшие dispatch, осиротевшие worktree, объём одноразовых данных. |
| `tracker` | Только с `--online`: авторизация `gh`/`glab`, доступность origin, права и метки. |

Как читать результат:

- Код выхода `1` — есть хотя бы один `fail`; `warn` и `skipped` на код не влияют.
- Под проблемной строкой печатается `-> Как исправить:` и, если есть, готовая команда с абсолютными
  путями — её можно запускать из любого каталога.
- `--fix` только создаёт недостающие каталоги `.harness` и пересобирает `.harness/skills/REGISTRY.md`.
  Системные настройки, git config, права и worktree он не трогает — для них в отчёте только команда.
- `--online --fix` дополнительно создаёт отсутствующие метки трекера; существующие метки не
  перекрашиваются.
- `--json` выдаёт контракт `schema_version: 1` со стабильными `checks[].id` (например `files.lock`).

Все проверки описаны в [руководстве](./harness/docs/harness-guide.md#шаг-2--команды-cli).

## Пульт: `harness console`

`console` — интерактивный терминальный пульт (TUI) для диагностики, команд харнесса,
оркестрации, отчётов и Repo Map.

```bash
python3 harness/bin/harness.py console /path/to/repository
```

Нужен [`uv`](https://docs.astral.sh/uv/) в `PATH`: пульт сам запускается через
`uv run --no-project --with textual==<pin>` и не трогает зависимости целевого проекта. Первый запуск
скачивает `textual`, поэтому нужна сеть. Без `uv` или сети пульт печатает причину и обычный
текстовый отчёт `harness health`.

Главный экран — дашборд: счётчики проверок, число открытых batch оркестрации, уровень Repo Map,
версия харнесса и дрейф снимка. Действие «Online checks» пересчитывает счётчики с `--online`.
Разделы меню:

| Раздел | Что внутри |
|---|---|
| `Diagnostics` | Полный отчёт `health`, «online checks» и «apply fixes» (`health --fix`, требует повторного нажатия). |
| `Harness` | Команды CLI: init, update, diff, adopt, registry, lock-project-skills, list, health, cleanup, Repo Map, ledger и удаление worktree. |
| `Orchestration` | Команды coordinator для оператора: batch, dispatch, qa status, risk assess, context-package, ledger status. |
| `Reports` | Отчёты ролей из леджера с фильтрами по тикету, роли, outcome и дате, хронология batch и QA-логи. |
| `Repo Map` | Карта репозитория для HEAD: сводка, дерево файлов с сигнатурами, поиск символов, связи, хабы. |

Клавиши: `Esc` — назад или отмена, `e` — экспорт в Markdown, `j` — экспорт Repo Map в JSON,
`b` — хронология batch в отчёте или построение карты в `Repo Map`, `F3` — QA-логи в `Reports`,
`Ctrl+Q` — выход.

Рядом с каждой командой пульт показывает её CLI-эквивалент и запускает тот же CLI-процесс.
Команды из «Как исправить» он только показывает, но не выполняет. Перед необратимыми действиями
пульт переспрашивает, а `ledger reset` и hard cleanup требуют ввести `RESET` или `HARD`. Экспорты
сохраняются в `docs/tasks/<папка тикета>/artifacts/` или `docs/tasks/console-exports/` и никогда не
перезаписываются.

## Релизная политика

Версии следуют SemVer. После изменения `harness/VERSION`, `pyproject.toml` и секции
`CHANGELOG.md` разработчик публикует тег `vMAJOR.MINOR.PATCH` на проверенном коммите.
[GitHub CD](./.github/workflows/release.yml) запускает проверки `verify.yml`, сверяет тег с
версией, создаёт архив установки и SHA-256, затем публикует GitHub Release. Release Notes берутся
из секции версии в [CHANGELOG.md](./CHANGELOG.md); её отсутствие прерывает выпуск.
Архив и checksum доступны в [GitHub Releases](https://github.com/PVMalove/claude-agent-harness/releases).
Проверка после скачивания: `sha256sum --check claude-agent-harness-vX.Y.Z.tar.gz.sha256`.
Дополнительный parser bundle прикладывает ручной
[`release-parser-bundle.yml`](./.github/workflows/release-parser-bundle.yml) к уже созданному
Release того же коммита. Полная процедура описана в [releases.md](./docs/agents/releases.md).
В целевых проектах GitLab поддерживается через `glab` в workflow тикетов и merge requests;
релизный CD этого репозитория работает на GitHub.

## Структура проекта

| Путь | Назначение |
|---|---|
| `harness/` | CLI, каталог capability, runtime-модули и шаблоны целевого проекта |
| `skills/` | Закреплённый vendor-снимок и собственные skills |
| `global/`, `global-skills/`, `bin/` | Глобальный профиль, стартовые skills и установщик |
| `scripts/` | Сборка, проверка и clean-room сценарии исходного репозитория |
| `docs/` | ADR, руководства, русские описания и Archify-диаграммы исходного репозитория |
| `third_party/` | Provenance, lock и лицензии upstream |
| `.github/` | CI, CD и проверки upstream |

Лицензия проекта — [MIT](./LICENSE). Лицензия закреплённого upstream-снимка находится в
[`third_party/mattpocock-skills/LICENSE`](./third_party/mattpocock-skills/LICENSE).
