# Гид по харнессу

Практический гид по `claude-agent-harness` для проекта, где установлена `pvmalove-suite`. Термины — в [../CONTEXT.md](../CONTEXT.md), архитектурные решения и почему они приняты — в [adr/](./adr/).

## 1. Конвейер: идея → релиз

Полное описание маршрута и всех развилок — в самом скилле `ask-matt` (`/ask-matt`, читает `skills/first-party/pvmalove/ask-matt/SKILL.md`), он служит живой картой поверх остальных скиллов и не дублируется здесь построчно. Коротко:

1. **`/grill-with-docs`** — вычерчивает идею интервью, откладывает `CONTEXT.md`/ADR по ходу.
2. Развилка: нужен прогон кода для ответа → **`/prototype`** через **`/handoff`**.
3. Развилка: многосессионная сборка → **`/to-spec`** → **`/to-tickets`** → **`/implement`** по тикету (с `/clear` между тикетами); в одну сессию — сразу **`/implement`**.
4. **`/implement`** прогоняет **`/tdd`**, закрывается **`/code-review`** (два независимых прогона — Standards и Spec), коммитит, открывает PR и останавливается на подтверждении разработчика — мерджит только человек.

On-ramp'ы: **`/triage`** (входящие issue/PR), **`/diagnosing-bugs`** (что-то сломалось), **`/wayfinder`** (туманный масштабный эффорт).

## 2. Что именно переопределено в pvmalove-suite

| Скилл | Что изменено относительно апстрима |
|---|---|
| `triage` | Каноническая пара ролей `bug`/`enhancement` + 5 состояний заменена на namespaced-таксономию: `hitl`/`afk` (кто делает работу) + `type::*` (какая это сессия) + `workflow::*` (где в пайплайне) + контекстные лейблы (`epic::*`, `task-report::required`, `out-of-scope`). Таксономия по умолчанию описана в `docs/agents/triage-labels.md` — редактируется прямо там, `triage/SKILL.md` не хардкодит лейблы. |
| `to-spec` | Пишет спеку сначала в `docs/tasks/` (см. `docs/agents/artifacts.md`), публикует через `--body-file`; вешает `epic::<slug>` на публикуемый issue. |
| `to-tickets` | Наследует ту же схему лейблов и `epic::<slug>` на дочерние тикеты; для локального трекера — один файл на тикет в `.scratch/<feature>/issues/`. |
| `implement` | После `/code-review` — коммит, `qa-gate`, `pr-composer` (если есть), `gh pr create`, затем обязательная пауза на подтверждение разработчика. Никогда не мерджит сам (см. `docs/agents/git-workflow.md`). |
| `ask-matt` | Описание главного потока учитывает PR-паузу `implement` (коммит → PR → пауза на ревью, не auto-merge). |
| `code-review` | Язык отчёта берётся из `language` в `.harness/project.json` (по умолчанию `ru`) вместо жёсткого английского. |

Новый скилл без апстримного аналога:

| Скилл | Назначение |
|---|---|
| `qa-gate` | Прогоняет `qa_gate_commands` из `.harness/project.json` по очереди, останавливается на первой ошибке с полным выводом, иначе — однострочный PASS. |

Плюс Claude Code subagent (не skill, отдельный механизм обнаружения `.claude/agents/`):

| Agent | Назначение |
|---|---|
| `pr-composer` | Заполняет тело PR по шаблону из `docs/agents/git-workflow.md` §3 (язык — из `project.json`), получает diff/лог/результат `qa-gate`, отдаёт готовый markdown вызывающему. |

## 3. Хуки (`.claude/hooks/`, проводка в `.claude/settings.local.json`)

| Хук | Событие | Что делает |
|---|---|---|
| `block-direct-master.sh` | PreToolUse(Bash) | Блокирует `git commit`/`git push` прямо в `master`/`main` |
| `block-pr-merge.sh` | PreToolUse(Bash) | Блокирует `gh pr merge` — мердж только вручную |
| `check-branch-name.sh` | PreToolUse(Bash) | `git checkout -b` сверяется с `branch_pattern` из `project.json` |
| `check-worktree-branch-name.sh` | PreToolUse(EnterWorktree) | То же для имени worktree |
| `require-qa-gate.sh` | PreToolUse(Bash) | Блокирует `gh pr create`, пока `qa-gate` не прошёл в этой сессии |
| `mark-qa-gate-passed.sh` | PostToolUse(Bash) | Ставит маркер при успехе последней команды из `qa_gate_commands` |
| `block-dangerous-git.sh` | PreToolUse(Bash) | Блокирует `reset --hard`/`clean -f`/`branch -D`/`checkout .`/`restore .` (адаптировано из апстримного `git-guardrails-claude-code`, не входит в `mattpocock-suite`) |
| `block-scratch-outside-docs-tasks.sh` | PreToolUse(Write\|Edit) | Запрещает писать спеки/скретчпады в системный temp — только `docs/tasks/` |
| `count-skill-usage.sh` | PreToolUse(Skill) | Счётчик вызовов скиллов, аналитика, никогда не блокирует |

## 4. Структура репозитория

```
harness/bin/harness          # CLI: init/diff/update/adopt/health
harness/CAPABILITIES.json    # mattpocock-suite (vendor) + pvmalove-suite (extends/overrides/additions)
harness/project/             # шаблоны, которые harness init разворачивает в целевой проект
  AGENTS.md.tmpl
  project.json.tmpl
  settings.local.json.tmpl
  docs-agents/*.md           # -> docs/agents/*.md целевого проекта
  hooks/*.sh                 # -> .claude/hooks/
  rules/karpathy-guidelines.md
  agents/pr-composer.md      # -> .claude/agents/
skills/vendor/mattpocock/    # апстрим, байт-в-байт, не редактируется
skills/first-party/pvmalove/ # личные переопределения и новые скиллы
third_party/mattpocock-skills/  # provenance апстрима (UPSTREAM.lock, SHA256SUMS, лицензия Matt Pocock)
```
