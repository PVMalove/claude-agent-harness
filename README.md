# claude-agent-harness

Портативный харнесс для кодинг-агентов (Claude Code) — набор скиллов, правил, хуков и doc'ов, который одной командой разворачивается в любой проект и живёт там независимо от этого репозитория.

В чистом виде поставляет **mattpocock-suite** — 25 скиллов Matt Pocock (aihero.dev), закреплённых на конкретном коммите апстрима. Этот репозиторий добавляет поверх неё вторую, самодостаточную capability — **pvmalove-suite** — с личными доработками: эпик- и blocked-by-ticket-лейблы в `to-spec`/`to-tickets`/`implement` поверх канонической triage-таксономии апстрима, обязательная пауза перед открытием PR без авто-мерджа, настраиваемый язык вывода, и конфиг-драйвен `qa-gate`.

Термины ниже (капабилити, vendor/first-party-скилл, переопределение, дрейф, проектный конфиг) разобраны в [CONTEXT.md](./CONTEXT.md); значимые архитектурные решения и почему они приняты — в [docs/adr/](./docs/adr/); пошаговое использование — в [docs/agents/harness-guide.md](./docs/agents/harness-guide.md); как скиллы находят Codex, Kimi Code, OpenCode и Hermes Agent (не только Claude Code) — в [docs/runtime-discovery.md](./docs/runtime-discovery.md).

## Быстрый старт

Предполагаемый интерфейс работает ещё до того, как есть репозиторий, стек или харнесс. После разовой установки глобального слоя (`bin/install-global`, раздел «Установка» ниже) — в любой новой сессии просто скажите агенту:

> Используй `start-project`, чтобы помочь мне сформировать и начать этот проект.

Скилл сам классифицирует работу (software, content, research, operations, personal или другой явный домен), держит раннюю идею в диалоге, пока она не готова стать чем-то постоянным, и по готовности первого durable-факта предлагает ровно один следующий артефакт: продолжить разговор, завести docs-only seed-репозиторий, или сразу собрать харнесс — вызывая `harness/bin/harness init` (раздел «Установка» ниже) от вашего имени. Имя capability и пути каталога знать не нужно — `start-project` выбирает их сам (по умолчанию `project-foundation` для доменно-нейтральных проектов; `mattpocock-suite`/`pvmalove-suite` — для инженерных). Если установлена опциональная личная/организационная надстройка, тот же запрос авторизует только выбор пакетов по каталогу — приватные знания остаются закрытыми.

Для репозитория, где уже есть настоящий код и свои конвенции (а харнесса ещё нет) — тот же принцип, но `integrate-project`, устанавливается и триггерится так же:

> Используй `integrate-project`, чтобы интегрировать харнесс в этот существующий проект.

Вместо того чтобы формировать идею с нуля, скилл сначала аудирует репозиторий как есть — стек и команды из реальных манифестов (`package.json`/`pyproject.toml`/`go.mod` и т.п., а не из README), конвенции веток и CI из фактических workflow-файлов, уже существующие `AGENTS.md`/`CLAUDE.md`/`.cursor/rules`/`.claude/` от других инструментов (не перезаписывает их молча — расхождения с этим харнессом идут пользователю на подтверждение), — и только потом предлагает capability и устанавливает харнесс тем же путём, что и `start-project`.

Всё, что ниже — что происходит под капотом, и как пользоваться харнессом напрямую через CLI, если нужно.

## Архитектура

| Слой | Назначение |
|---|---|
| Runtime | Модель, встроенные инструменты, права, сессии — то, что уже умеет Claude Code |
| Глобальный профиль | Небольшой кросс-проектный контракт безопасности + `start-project` (не персонализирован, ставится как есть из апстрима) |
| Харнесс проекта | Инструкции проекта, выбранные скиллы, ссылки обнаружения и lock-файл |
| Опциональная надстройка | `pvmalove-suite` — личные скиллы, doc'и, хуки поверх обычного харнесса |

## Две capability

- **`mattpocock-suite`** — чистый снимок апстрима, файлы никогда не редактируются вручную (`skills/vendor/`, закреплено через `third_party/mattpocock-skills/UPSTREAM.lock`).
- **`pvmalove-suite`** — выбирается **вместо** `mattpocock-suite`, не вместе с ней (CLI откажет с `duplicate skill name`, если указать обе сразу). Расширяет её через `extends`/`overrides`/`additions` в `harness/CAPABILITIES.json`: 17 скиллов наследуются от `mattpocock-suite` без изменений, 8 переопределены в `skills/first-party/pvmalove/`: `to-spec`, `to-tickets`, `implement`, `ask-matt`, `code-review`, `grilling`, `triage`, `wayfinder`; доп. скиллы: `qa-gate`, `to-guide`, `setup-labels`.

## Dynamic Dispatcher & Workflow Engine (Experimental)

В `harness/runtime` реализован полноценный декларативный **Workflow Orchestrator**, разделяющий логику на уровни Domain, Application, Infrastructure и CLI.

Его главная идея — двухслойный UX:
1. **Claude Code (Frontend / Intent Parser)** получает команду пользователя (например: *"Запусти feature-development"*), понимает через скилл `run-workflow`, что это бизнес-процесс, и вызывает CLI харнесса.
2. **Harness (Backend / Orchestrator)** управляет декларативным графом, динамически подбирая `Worker` для каждого шага на основе `capabilities`, `priority` и `health`.

Пример конфигурации (`.harness/orchestration.toml`):
```toml
[workflows.feature-development]
steps = ["grill-with-docs", "to-spec", "to-tickets", "implement"]

[workflows.feature-development.mappings.to-spec]
context_id = "grill-with-docs.output.context_id"

[workflows.feature-development.mappings.to-tickets]
spec_file = "to-spec.output.spec_file"

[workflows.feature-development.mappings.implement]
ticket_id = "to-tickets.output.ticket_id"

[skills.implement.quality]
required = ["tdd", "code-review", "qa-gate"]
```

`implement` выполняет TDD, code review и qa-gate как обязательные внутренние фазы; их не нужно и нельзя дублировать отдельными шагами workflow.

Доступные команды CLI:
```bash
# Работа с пайплайнами
harness workflow list
harness workflow show feature-development
harness workflow plan feature-development # Показывает explainable routing план для всех шагов
harness workflow run feature-development --input '{"task": "Add OAuth"}'

# Управление состоянием (State Store в SQLite)
harness workflow resume <execution-id> # Продолжить упавший пайплайн с нужного шага
harness workflow status <execution-id>
harness workflow cancel <execution-id>

# Дебаг роутинга
harness skill explain implement # Показывает очки, capabilities и отклоненных кандидатов
```

*Примечание: Провайдеры (ClaudeProvider, AGYProvider, MCPProvider) на данном этапе являются экспериментальными заглушками (Mocks) для валидации архитектурных границ, а CLIProvider полностью функционален.*

При выборе `pvmalove-suite` `harness init` дополнительно (один раз, при отсутствии файла — как `AGENTS.md`/`CLAUDE.md`) разворачивает в проект:

- `docs/agents/{git-workflow,worktrees,artifacts,issue-tracker,triage-labels,harness-guide}.md`
- `.claude/hooks/*.sh` + их проводку в `.claude/settings.local.json`
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

`bin/install-global` — Python-скрипт (`#!/usr/bin/env python3`, требует 3.9+), запускается одинаково на Linux/macOS/Windows — так же, как `harness/bin/harness` ниже (тоже требует 3.9+ и явно откажется на более старой версии), никакого отдельного `.sh`/`.ps1` не нужно. На Windows для создания настоящих (не «сломанных» файлового типа) символьных ссылок на директории нужен включённый Developer Mode либо запуск терминала от имени администратора — без этого команда явно падает с ошибкой на создании линка и подсказывает, что делать.

Флаги `--check` (ничего не пишет, только сверяет), `--replace-conflicts` (бэкапит перед заменой) и `--skills-only` (без instruction-файла, только `start-project`) — полный разбор, что именно ставится каждому из пяти рантаймов и куда, в [docs/agents/harness-guide.md](./docs/agents/harness-guide.md), раздел 0.

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
python3 harness/bin/harness update /path/to/repository --capability pvmalove-suite
python3 harness/bin/harness registry /path/to/repository
python3 harness/bin/harness lock-project-skills /path/to/repository
python3 harness/bin/harness health /path/to/repository
python3 harness/bin/harness list /path/to/repository
```

Windows (PowerShell):
```powershell
python harness\bin\harness diff C:\path\to\repository
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

## Политика репозитория

- Файлы под `skills/vendor/` никогда не редактируются вручную — только полная замена закреплённого снимка.
- Личные скиллы и надстройки живут в `skills/first-party/pvmalove/` и `harness/project/`, не смешиваются с vendor-деревом.
- Апстримные ревизии закреплены, provenance (`third_party/mattpocock-skills/`) сохраняется.
- Новые ADR (`docs/adr/`) — по [`docs/adr/template.md`](./docs/adr/template.md): обязательные секции
  Context/Decision/Alternatives/Rejected/Consequences, язык — как у остального репозитория (сейчас
  русский). Решения 0001-0005 предшествуют шаблону и его секции не повторяют — не переписывать их
  задним числом.
- `third_party/mattpocock-skills/UPSTREAM.lock` может отстать от реального апстрима незаметно —
  `scripts/check-upstream-drift` (сеть, читает только) сверяет пин с последним тегом на
  `mattpocock/skills` и раскладывает реальные изменения на «можно тянуть не глядя» (скиллы вне
  `pvmalove-suite.overrides`) и «сверить руками перед ресинком» (ADR 0002). Гоняется вручную или
  еженедельно через `.github/workflows/upstream-drift.yml` (`workflow_dispatch` — можно и по
  требованию); падает (exit 1) только когда апстрим реально ушёл вперёд, не блокирует обычные PR.
- `docs/agents/*.md` и `harness/project/docs-agents/*.md` — одно и то же по смыслу в двух местах
  (вторая копия — то, что `pvmalove-suite` реально разворачивает в целевые проекты); `scripts/verify`
  сверяет обе копии по содержимому (без учёта BOM/CRLF) и не даст молча разойтись.
