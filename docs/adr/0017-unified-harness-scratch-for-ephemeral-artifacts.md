# Единый харнесс-скретч для одноразовых артефактов, docs/tasks/ как постоянный локальный архив

## Контекст системы

Тело PR/комментария хранилось по разным путям в зависимости от активного runtime (`.claude/tmp/`
или `.agents/tmp/`), без технической защиты от случайного коммита — только текстовая дисциплина
«удали после публикации». Хук `block-scratch-outside-docs-tasks.sh` разошёлся с документацией:
его комментарий и текст ошибки требовали `.scratch/tmp/`, тогда как вся остальная документация
называла `.claude/tmp/`. Отдельно, черновики спек/тикетов в `docs/tasks/` и Live Artifact из
`/grilling` (одобренный человеком список путей Discovery Context) не имели зафиксированной
внутренней структуры и не были явно увязаны друг с другом.

## Действующий контракт

Два разных по природе места, не смешиваются:

**`.harness/scratch/tmp/pr-body-<issue>-<slug>.md`** — одноразовое тело PR/комментария. Путь один
и тот же независимо от активного runtime (Claude Code, Codex, Kimi, OpenCode) — прежнее
per-runtime разделение `.claude/tmp/` vs `.agents/tmp/` упраздняется. Удаляется сразу после
успешной публикации (`gh`/`glab`), сохраняется при сбое для повторной попытки. Защищён вложенным
`.gitignore` внутри `.harness/scratch/` (по образцу `.harness/orchestration/state/.gitignore`),
которую `harness init`/`update` создают и чинят в целевом проекте.

**`docs/tasks/issue-<N-или-slug>-<slug>/`** — не эфемерный черновик под удаление, а постоянный
локальный архив истории для человека-мейнтейнера (gitignored, не коммитится, но и не удаляется
инструментами):

```
docs/tasks/issue-<N-или-slug>-<slug>/
  issue-<N>-spec-<slug>.md   — спека эпика/задачи
  tickets/                   — дочерние тикеты, один файл на тикет (вложенная папка, не плоско)
  artifacts/                 — Live Artifact из /grilling: список одобренных путей (Discovery
                                Context); тот же список `/to-spec` дополнительно вписывает в текст
                                эпика под «## Relevant Files (Discovery Context)»
```

Папка эпика создаётся уже в момент `/grilling`, по описательному slug (реальный ID тикета ещё не
существует). Из-за общего пространства номеров issue/PR на GitHub (`issue-tracker.md`) номер
заранее не резервируется — папка переименовывается в `issue-<N>-<slug>` только после фактической
публикации через `/to-spec`, тем же приёмом, что уже описан в `artifacts.md` для случая
«ID неизвестен». `harness init`/`update` обеспечивают наличие `/docs/tasks/` в `.gitignore`
целевого проекта — раньше это не гарантировалось никак.

`.scratch/<feature-slug>/` (durable-запись локального markdown-трекера, актуальна только при
отсутствии GitHub/GitLab remote) и `.harness/orchestration/state/` (QA/orchestration evidence)
остаются отдельными от обоих корней выше и не смешиваются с ними.

Хук `block-scratch-outside-docs-tasks.sh` сохраняет имя — его task-scratch-правило (писать
черновики только в `docs/tasks/`, не в системный temp) осталось верным как есть; меняется только
его pr-body-правило (цель — `.harness/scratch/tmp/`, а не `.claude/tmp/`/`.agents/tmp/`/
`.scratch/tmp/`).

## Операционные последствия

Документация (`artifacts.md`, `git-workflow.md` §1, `harness-guide.md`, `pr-composer.md`, SKILL
`to-pull-requests` и их шаблонные копии в `harness/project/`) обновляется на новую pr-body-цель и
на структуру `spec + tickets/ + artifacts/` внутри `docs/tasks/`. Хук и его shipped-копия
синхронизируются по pr-body-логике. `harness init`/`update` получают шаг записи/проверки
`.gitignore` для `/docs/tasks/` и вложенного `.gitignore` для `.harness/scratch/`. Существующий
контент в `.claude/tmp/`/`.agents/tmp/` не мигрируется автоматически.
