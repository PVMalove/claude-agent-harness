# claude-agent-harness

Портативный харнесс для кодинг-агентов (Claude Code) — набор скиллов, правил, хуков и doc'ов, который одной командой разворачивается в любой проект и живёт там независимо от этого репозитория.

В чистом виде поставляет **mattpocock-suite** — 25 скиллов Matt Pocock (aihero.dev), закреплённых на конкретном коммите апстрима. Этот репозиторий добавляет поверх неё вторую, самодостаточную capability — **pvmalove-suite** — с личными доработками: другая таксономия triage-лейблов, обязательная пауза перед открытием PR без авто-мерджа, настраиваемый язык вывода, и конфиг-драйвен `qa-gate`.

Термины ниже (капабилити, vendor/first-party-скилл, переопределение, дрейф, проектный конфиг) разобраны в [CONTEXT.md](./CONTEXT.md); значимые архитектурные решения и почему они приняты — в [docs/adr/](./docs/adr/); пошаговое использование — в [docs/harness-guide.md](./docs/harness-guide.md).

## Архитектура

| Слой | Назначение |
|---|---|
| Runtime | Модель, встроенные инструменты, права, сессии — то, что уже умеет Claude Code |
| Глобальный профиль | Небольшой кросс-проектный контракт безопасности + `start-project` (не персонализирован, ставится как есть из апстрима) |
| Харнесс проекта | Инструкции проекта, выбранные скиллы, ссылки обнаружения и lock-файл |
| Опциональная надстройка | `pvmalove-suite` — личные скиллы, doc'и, хуки поверх обычного харнесса |

## Две capability

- **`mattpocock-suite`** — чистый снимок апстрима, файлы никогда не редактируются вручную (`skills/vendor/`, закреплено через `third_party/mattpocock-skills/UPSTREAM.lock`).
- **`pvmalove-suite`** — выбирается **вместо** `mattpocock-suite`, не вместе с ней (CLI откажет с `duplicate skill name`, если указать обе сразу). Расширяет её через `extends`/`overrides`/`additions` в `harness/CAPABILITIES.json`: 19 скиллов наследуются от `mattpocock-suite` без изменений, 6 (`triage`, `to-spec`, `to-tickets`, `implement`, `ask-matt`, `code-review`) переопределены в `skills/first-party/pvmalove/`, плюс новый скилл `qa-gate`.

При выборе `pvmalove-suite` `harness init` дополнительно (один раз, при отсутствии файла — как `AGENTS.md`/`CLAUDE.md`) разворачивает в проект:

- `docs/agents/{git-workflow,worktrees,artifacts,issue-tracker,triage-labels}.md`
- `.claude/hooks/*.sh` + их проводку в `.claude/settings.local.json`
- `.claude/rules/karpathy-guidelines.md`
- `.claude/agents/pr-composer.md` (Claude Code subagent — вне системы skills/capability, отдельный механизм обнаружения)
- `.harness/project.json` — язык вывода, паттерн имени ветки, базовая ветка для PR, команды `qa-gate` (спрашивается интерактивно, либо флагами)

## Установка

Глобальный слой (не персонализирован, как в апстриме):

```bash
bin/install-global --target-home "$HOME" --runtime claude
```

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

(флаги `--language`/`--pr-base-branch`/`--branch-pattern`/`--qa-gate-command` можно опустить — `harness init` спросит их интерактивно)

Чистый апстрим без личных доработок — то же самое с `--capability mattpocock-suite`.

Обновление и диагностика:

```bash
python3 harness/bin/harness diff /path/to/repository
python3 harness/bin/harness update /path/to/repository --capability pvmalove-suite
python3 harness/bin/harness health /path/to/repository
```

## Политика репозитория

- Файлы под `skills/vendor/` никогда не редактируются вручную — только полная замена закреплённого снимка.
- Личные скиллы и надстройки живут в `skills/first-party/pvmalove/` и `harness/project/`, не смешиваются с vendor-деревом.
- Апстримные ревизии закреплены, provenance (`third_party/mattpocock-skills/`) сохраняется.
