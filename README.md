# claude-agent-harness

Портативный харнесс для coding agents (Claude Code, Codex, Kimi Code, OpenCode и Hermes Agent) — набор скиллов, правил, хуков и документов, который одной командой разворачивается в любой проект и живёт там независимо от этого репозитория.

По умолчанию устанавливается лёгкая доменно-нейтральная capability **project-foundation** из 5 скиллов. Полная upstream-сборка — **mattpocock-suite**, 25 скиллов Matt Pocock (aihero.dev), закреплённых на конкретном коммите. **pvmalove-suite** добавляет управляемый инженерный workflow: native связи эпиков и тикетов, namespaced triage-таксономию, двухосевое review, ручной `/to-pull-requests`, настраиваемый язык вывода и конфигурируемый `qa-gate`.

Термины ниже (капабилити, vendor/first-party-скилл, переопределение, дрейф, проектный конфиг) разобраны в [CONTEXT.md](./CONTEXT.md); действующие архитектурные контракты — в [docs/adr/](./docs/adr/); целостный обзор текущей системы — в [docs/agents/current-state.md](./docs/agents/current-state.md); пошаговое использование — в [docs/agents/harness-guide.md](./docs/agents/harness-guide.md); как скиллы находят Codex, Kimi Code, OpenCode и Hermes Agent (не только Claude Code) — в [docs/runtime-discovery.md](./docs/runtime-discovery.md).

## Пайплайн одним взглядом

[![Пайплайн доставки: от идеи до merge](./docs/diagrams/previews/delivery-pipeline.workflow.png)](./docs/diagrams/delivery-pipeline.workflow.html)

Полный маршрут: `/grill-with-docs` снимает неопределённость → `/to-spec` публикует эпик → `/to-tickets`
режет его на слайсы и помечает каждый `afk` или `hitl` → `afk` идёт в `/implement`, `hitl` — в
`/to-guide` → PR открывает разработчик через `/to-pull-requests`. Ни один шаг не запускает следующий
сам. Картинка кликабельна: за ней интерактивная версия с поиском, фокусом и экспортом.

[![Архитектура переносимого Agent Harness](./docs/diagrams/previews/harness-topology.architecture.png)](./docs/diagrams/harness-topology.architecture.html)

Architecture-схема показывает границу между исходным harness и целевым проектом: capability
разрешается CLI из `CAPABILITIES.json` и пакетов skills, материализуется в единый
`.harness/skills` snapshot, затем становится доступной через нативные skill-roots либо fallback
`AGENTS.md` + `REGISTRY.md` для Hermes Agent.

## Быстрый старт

Предполагаемый интерфейс работает ещё до того, как есть репозиторий, стек или харнесс. После разовой установки глобального слоя (`bin/install-global`, раздел «Установка» ниже) — в любой новой сессии просто скажите агенту:

> Используй `start-project`, чтобы помочь мне сформировать и начать этот проект.

Скилл сам классифицирует работу (software, content, research, operations, personal или другой явный домен), держит раннюю идею в диалоге, пока она не готова стать чем-то постоянным, и по готовности первого durable-факта предлагает ровно один следующий артефакт: продолжить разговор, завести docs-only seed-репозиторий, или сразу собрать харнесс — вызывая `harness/bin/harness init` (раздел «Установка» ниже) от вашего имени. Имя capability и пути каталога знать не нужно — `start-project` выбирает их сам (по умолчанию `project-foundation` для доменно-нейтральных проектов; `mattpocock-suite`/`pvmalove-suite` — для инженерных). Если установлена опциональная личная/организационная надстройка, тот же запрос авторизует только выбор пакетов по каталогу — приватные знания остаются закрытыми.

Для репозитория, где уже есть настоящий код и свои конвенции (а харнесса ещё нет) — тот же принцип, но `integrate-project`, устанавливается и триггерится так же:

> Используй `integrate-project`, чтобы интегрировать харнесс в этот существующий проект.

Вместо того чтобы формировать идею с нуля, скилл сначала аудирует репозиторий как есть — стек и команды из реальных манифестов (`package.json`/`pyproject.toml`/`go.mod` и т.п., а не из README), конвенции веток и CI из фактических workflow-файлов, уже существующие `AGENTS.md`/`CLAUDE.md`/`.cursor/rules`/`.claude/` от других инструментов (не перезаписывает их молча — расхождения с этим харнессом идут пользователю на подтверждение), — и только потом предлагает capability и устанавливает харнесс тем же путём, что и `start-project`.

Всё, что ниже — что происходит под капотом, и как пользоваться харнессом напрямую через CLI, если нужно.

## Текущее состояние системы

Харнесс работает как переносимый снимок выбранной capability: runtime предоставляет модель и
инструменты, глобальный профиль — межпроектный entry contract, а проектный слой — skills,
инструкции, hooks и проверяемые lock-файлы. `project-foundation` даёт доменно-нейтральную основу,
`mattpocock-suite` — закреплённый upstream-набор, `pvmalove-suite` — инженерный workflow, а
`backend-orchestration` — его явную opt-in надстройку для координированной backend-работы.

`pvmalove-suite` наследует 15 skills без изменений; 10 переопределены в `skills/first-party/pvmalove/`: `to-spec`, `to-tickets`, `implement`, `ask-matt`, `code-review`, `grilling`, `grill-me`, `grill-with-docs`, `triage`, `wayfinder`; доп. скиллы: `qa-gate`, `to-guide`, `setup-labels`, `to-pull-requests`, `fast-implement`, `delivery-stats`. `backend-orchestration` выбирается отдельно и разрешает эту зависимость автоматически.

Проектный `.harness/project.json` определяет ветки, язык и QA. Работа начинается с тикета,
проходит в issue-ветке и требует явного подтверждения разработчика перед PR; merge всегда ручной.
При выбранной `backend-orchestration` coordinator ведёт утверждённые batch и immutable dispatch;
человек явно утверждает каждый dispatch. Coordinator применяет независимый review и clean-room QA
к candidate SHA. Runtime adapter доставляет только
одобренную работу, а публикация SHA и PR остаются за разработчиком.

Полный текущий контракт, роли, lifecycle, FIFO QA lane и локальное state-хранилище описаны в
[Текущем состоянии Agent Harness](./docs/agents/current-state.md). Пошаговая настройка и
операционные команды находятся в [руководстве по backend-оркестрации](./docs/agents/backend-orchestration.md)
и [справочнике харнесса](./docs/agents/harness-guide.md).

### Конвейер `/implement`

`/implement <id>` — не одна сессия, а конвейер из пяти ролевых гейтов, которым сессия управляет как
coordinator: архитектор разбирает текущую архитектуру и предлагает план → **человек утверждает
план** → разработчик реализует в своей issue-ветке, тестирует, коммитит и пушит → code review
проверяет кандидатный SHA → **человек утверждает переход к QA** → независимый QA в clean-room
worktree; найденные дефекты возвращают работу разработчику и QA повторяется → зелёный QA даёт
итоговый отчёт и публикацию точного принятого SHA → **PR разработчик открывает сам** через
`/to-pull-requests`. Короткий однопроходный путь без гейтов остался в `/fast-implement`.

[![Конвейер /implement с гейтами](./docs/diagrams/previews/implement-pipeline.workflow.png)](./docs/diagrams/implement-pipeline.workflow.html)

[![Последовательность gated dispatch в /implement](./docs/diagrams/previews/implement-dispatch.sequence.png)](./docs/diagrams/implement-dispatch.sequence.html)

Sequence-схема дополняет workflow: она фиксирует участников, два человеческих approval-gate,
передачу immutable brief, candidate SHA, независимые отчёты review/QA и публикацию только принятого SHA.

[![Поток capability от каталога к runtime](./docs/diagrams/previews/capability-delivery.dataflow.png)](./docs/diagrams/capability-delivery.dataflow.html)

Data Flow показывает происхождение и потребителей данных: каталог capability, vendor snapshot и
first-party overrides → `harness init/update` → `.harness/skills` → native runtime discovery или
Hermes fallback. Схема не содержит секретов и не описывает их значения.

Остальные визуальные карты — [резолв runtime и dispatch](./docs/diagrams/backend-runtime.workflow.html)
(транспорт `orca` или `in-process`, self-report модели, heartbeat),
[жизненный цикл batch](./docs/diagrams/backend-batch.lifecycle.html) и
[QA/создание PR](./docs/diagrams/qa-call-path.workflow.html). Все девять диаграмм, их исходники и
порядок обновления — в [docs/diagrams/](./docs/diagrams/README.md).

При выборе `pvmalove-suite` `harness init` дополнительно (один раз, при отсутствии файла — как `AGENTS.md`/`CLAUDE.md`) разворачивает в проект:

- `docs/agents/{artifacts,backend-orchestration,current-state,git-workflow,harness-guide,issue-tracker,triage-labels,worktrees}.md`
- `.claude/hooks/*.sh` + их проводку в `.claude/settings.local.json`
- hook для блокировки автоматической атрибуции в commit/PR metadata; CI повторяет эту проверку
- `.claude/rules/karpathy-guidelines.md`
- `.claude/agents/pr-composer.md` (Claude Code subagent — вне системы skills/capability, отдельный механизм обнаружения)
- `.harness/project.json` — язык вывода, паттерн имени ветки, базовая ветка для PR, команды `qa-gate` (спрашивается интерактивно, либо флагами)

## Установка

Глобальный слой — `bin/install-global`, не персонализирован, ставится отдельно и один раз на машину (на пользователя `~`, не на конкретный проект). Поддерживает пять рантаймов, `--runtime` повторяем:

```bash
python3 bin/install-global --target-home "$HOME" --runtime codex --runtime claude --runtime kimi --runtime opencode --runtime hermes
```

Windows (PowerShell):
```powershell
python bin\install-global --target-home $HOME --runtime codex --runtime claude --runtime kimi --runtime opencode --runtime hermes
```

`bin/install-global` — Python-скрипт (`#!/usr/bin/env python3`, standalone floor — 3.9+), запускается одинаково на Linux/macOS/Windows — так же, как `harness/bin/harness` ниже; отдельного `.sh`/`.ps1` не нужно. Метаданные проекта в `pyproject.toml` отдельно объявляют `requires-python >=3.14`. На Windows для создания настоящих символьных ссылок на директории нужен включённый Developer Mode либо запуск терминала от имени администратора — без этого команда явно падает с подсказкой.

`bin/install-global` устанавливает только глобальный профиль и entry skills; MCP, plugins и project integrations он не устанавливает. Флаги `--check` (ничего не пишет, только сверяет), `--replace-conflicts` (перемещает конфликтующие файлы в backup) и `--skills-only` (без профиля) — полный разбор, что именно ставится каждому из пяти рантаймов и куда, в [docs/agents/harness-guide.md](./docs/agents/harness-guide.md), раздел 0.

Харнесс проекта — личная сборка:
```bash
python3 harness/bin/harness init /path/to/repository \
  --project-type software \
  --stack python \
  --capability pvmalove-suite \
  --base-branch main \
  --language ru \
  --qa-gate-command "make check" \
  --qa-gate-command "make test"
```

Windows (PowerShell):
```powershell
python harness\bin\harness init C:\path\to\repository `
  --project-type software `
  --stack python `
  --capability pvmalove-suite `
  --base-branch main `
  --language ru `
  --qa-gate-command "make check" `
  --qa-gate-command "make test"
```

(флаги `--language`/`--pr-base-branch`/`--branch-pattern`/`--qa-gate-command` можно опустить — `harness init` спросит их интерактивно)

Если PowerShell отвечает `python: The term 'python' is not recognized...` — сначала проверьте `[Environment]::GetEnvironmentVariable('Path','User')`: если Python там уже есть, но `Get-Command python,py` всё равно ничего не находит — откройте новое окно терминала (переменные окружения читаются один раз при старте процесса, старое окно их не подхватит само). Если Python в PATH действительно нет — установите его, либо вызывайте по полному пути, например `& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" harness\bin\harness init ...`.

Чистый апстрим без личных доработок — то же самое с `--capability mattpocock-suite`. Без `--capability` вообще — по умолчанию `project-foundation`, 5 лёгких скиллов на любой тип проекта, не только software.

Проект, где под именами выбранной capability уже лежат свои (не харнесс-управляемые) скиллы — `adopt` вместо `init`: сохраняет всё остальное, конфликтующие имена без `--replace-conflicts` просто перечисляет и падает.

```bash
python3 harness/bin/harness adopt /path/to/repository --capability pvmalove-suite --replace-conflicts
```

Обновление и диагностика:

```bash
python3 harness/bin/harness diff /path/to/repository
python3 harness/bin/harness diff /path/to/repository --json
python3 harness/bin/harness update /path/to/repository --capability pvmalove-suite
python3 harness/bin/harness registry /path/to/repository
python3 harness/bin/harness lock-project-skills /path/to/repository
python3 harness/bin/harness health /path/to/repository
python3 harness/bin/harness list /path/to/repository
```

Windows (PowerShell):
```powershell
python harness\bin\harness diff C:\path\to\repository
python harness\bin\harness diff C:\path\to\repository --json
python harness\bin\harness update C:\path\to\repository --capability pvmalove-suite
python harness\bin\harness registry C:\path\to\repository
python harness\bin\harness lock-project-skills C:\path\to\repository
python harness\bin\harness health C:\path\to\repository
python harness\bin\harness list C:\path\to\repository
```

`harness init`/`adopt`/`update` всегда пишут в проект компактный `.harness/skills/REGISTRY.md` —
это фоллбек-обнаружение для рантаймов без нативного project-скилл-рута (сейчас — Hermes Agent).
`harness registry` перегенерирует его вручную. Скиллы, которые лежат в `.harness/skills` у самого
проекта и не пришли ни из одной выбранной capability, `harness health` требует подтвердить через
`harness lock-project-skills` — команда фиксирует их sha256 в `.harness/overlays/project-local.lock`
(хэширует только git-видимые файлы, gitignore'нутые рантайм-артефакты вроде `node_modules` в лок не
попадают). Нативные MCP/plugin/hook/runtime-конфиги (`.mcp.json`, `.claude/settings.json` и т.п.)
таким же образом инвентаризируются в `.harness/integrations.json` — путь, sha256, целевые рантаймы,
текстовое verify-действие и имена секретных env-переменных, но никогда сами секреты.

`skills/REGISTRY.md` — отдельный сгенерированный каталог исходников этого репозитория,
обновляемый через `scripts/build-registry`; его не следует путать с runtime-реестром
`.harness/skills/REGISTRY.md` в целевом проекте.

## Политика репозитория

- Файлы под `skills/vendor/` никогда не редактируются вручную — только полная замена закреплённого снимка.
- Личные скиллы и надстройки живут в `skills/first-party/pvmalove/` и `harness/project/`, не смешиваются с vendor-деревом.
- `/to-spec` выбирает для эпика `integration/<service-or-team>` и создаёт её от проектной `base_branch`, если такой ветки ещё нет; `/to-tickets` переносит её в дочерние тикеты, а issue-ветки и PR используют её как базу.
- Апстримные ревизии закреплены, provenance (`third_party/mattpocock-skills/`) сохраняется.
- ADR (`docs/adr/`) фиксирует только действующее труднообратимое решение и создаётся по
  [`docs/adr/template.md`](./docs/adr/template.md). Номер всегда следующий после наибольшего в
  каталоге; язык совпадает с языком репозитория.
- `third_party/mattpocock-skills/UPSTREAM.lock` может отстать от реального апстрима незаметно —
  `scripts/check-upstream-drift` (сеть, читает только) сверяет пин с последним тегом на
  `mattpocock/skills` и раскладывает реальные изменения на «можно тянуть не глядя» (скиллы вне
  `pvmalove-suite.overrides`) и «сверить руками перед ресинком» (см. [ADR 0001](./docs/adr/0001-portable-capability-snapshots.md)). Гоняется вручную или
  еженедельно через `.github/workflows/upstream-drift.yml` (`workflow_dispatch` — можно и по
  требованию); падает (exit 1) только когда апстрим реально ушёл вперёд, не блокирует обычные PR.
- `docs/agents/*.md` и `harness/project/docs-agents/*.md` — одно и то же по смыслу в двух местах
  (вторая копия — то, что `pvmalove-suite` реально разворачивает в целевые проекты); `scripts/verify`
  сверяет обе копии по содержимому (без учёта BOM/CRLF) и не даст молча разойтись.
- `harness update` по умолчанию не перезаписывает изменённые managed skills и seed-файлы; для
  managed skills используется `--force`, для seed-файлов — отдельный `--force-seed-files`.
