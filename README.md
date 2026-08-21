# claude-agent-harness

Портативный харнесс для кодинг-агентов (Claude Code) — набор скиллов, правил, хуков и doc'ов, который одной командой разворачивается в любой проект и живёт там независимо от этого репозитория.

В чистом виде поставляет **mattpocock-suite** — 25 скиллов Matt Pocock (aihero.dev), закреплённых на конкретном коммите апстрима. Этот репозиторий добавляет поверх неё вторую, самодостаточную capability — **pvmalove-suite** — с личными доработками: эпик- и blocked-by-ticket-лейблы в `to-spec`/`to-tickets`/`implement` поверх канонической triage-таксономии апстрима, обязательная пауза перед открытием PR без авто-мерджа, настраиваемый язык вывода, и конфиг-драйвен `qa-gate`.

Термины ниже (капабилити, vendor/first-party-скилл, переопределение, дрейф, проектный конфиг) разобраны в [CONTEXT.md](./CONTEXT.md); значимые архитектурные решения и почему они приняты — в [docs/adr/](./docs/adr/); пошаговое использование — в [docs/agents/harness-guide.md](./docs/agents/harness-guide.md); как скиллы находят Codex, Kimi Code, OpenCode и Hermes Agent (не только Claude Code) — в [docs/runtime-discovery.md](./docs/runtime-discovery.md).

## Быстрый старт

Предполагаемый интерфейс работает ещё до того, как есть репозиторий, стек или харнесс. После разовой установки глобального слоя (`bin/install-global`, раздел «Установка» ниже) — в любой новой сессии просто скажите агенту:

> Используй `start-project`, чтобы помочь мне сформировать и начать этот проект.

Скилл сам классифицирует работу (software, content, research, operations, personal или другой явный домен), держит раннюю идею в диалоге, пока она не готова стать чем-то постоянным, и по готовности первого durable-факта предлагает ровно один следующий артефакт: продолжить разговор, завести docs-only seed-репозиторий, или сразу собрать харнесс — вызывая `harness/bin/harness init` (раздел «Установка» ниже) от вашего имени. Имя capability и пути каталога знать не нужно — `start-project` выбирает их сам (по умолчанию `project-foundation` для доменно-нейтральных проектов; `mattpocock-suite`/`pvmalove-suite` — для инженерных). Если установлена опциональная личная/организационная надстройка, тот же запрос авторизует только выбор пакетов по каталогу — приватные знания остаются закрытыми.

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
- **`pvmalove-suite`** — выбирается **вместо** `mattpocock-suite`, не вместе с ней (CLI откажет с `duplicate skill name`, если указать обе сразу). Расширяет её через `extends`/`overrides`/`additions` в `harness/CAPABILITIES.json`: 20 скиллов наследуются от `mattpocock-suite` без изменений (включая `triage` — используется апстримный вариант as-is), 5 (`to-spec`, `to-tickets`, `implement`, `ask-matt`, `code-review`) переопределены в `skills/first-party/pvmalove/`, плюс новый скилл `qa-gate`.

При выборе `pvmalove-suite` `harness init` дополнительно (один раз, при отсутствии файла — как `AGENTS.md`/`CLAUDE.md`) разворачивает в проект:

- `docs/agents/{git-workflow,worktrees,artifacts,issue-tracker,triage-labels}.md`
- `.claude/hooks/*.sh` + их проводку в `.claude/settings.local.json`
- `.claude/rules/karpathy-guidelines.md`
- `.claude/agents/pr-composer.md` (Claude Code subagent — вне системы skills/capability, отдельный механизм обнаружения)
- `.harness/project.json` — язык вывода, паттерн имени ветки, базовая ветка для PR, команды `qa-gate` (спрашивается интерактивно, либо флагами)

## Установка

Глобальный слой — `bin/install-global`, не персонализирован, ставится отдельно и один раз на машину (на пользователя `~`, не на конкретный проект). Поддерживает пять рантаймов, `--runtime` повторяем:

```bash
bin/install-global --target-home "$HOME" --runtime codex --runtime claude --runtime kimi --runtime opencode --runtime hermes
```

`bin/install-global` — bash-скрипт (`#!/usr/bin/env bash`, использует bash-массивы), нативно в PowerShell/cmd не запускается. На Windows выполняйте эту команду как есть, но в Git Bash (входит в Git for Windows) или WSL — не в PowerShell. В Git Bash сам форсирует `MSYS=winsymlinks:nativestrict` (настоящие символьные ссылки вместо тихой подмены копией/junction'ом) — нужен включённый Developer Mode или права администратора, иначе команда явно упадёт на создании линка.

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
