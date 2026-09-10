# Текущее состояние Agent Harness

Agent Harness — переносимый runtime-native набор skills, правил, hooks и документации для coding
agents. Он устанавливается в целевой Git-репозиторий как самостоятельный снимок и работает без
доступа к исходному клону харнесса. Этот документ описывает действующий архитектурный контракт;
пошаговые процедуры находятся в специализированных руководствах этого каталога.

## 1. Система и основные понятия

Система состоит из трёх уровней: runtime предоставляет модель, инструменты, права и сессии;
глобальный профиль предоставляет короткий межпроектный контракт и entry skills; харнесс проекта
содержит выбранные capability, инструкции, проверки и lock-файлы. Глобальный слой устанавливается
отдельно, а `harness/bin/harness` материализует проектный слой в конкретном репозитории.

`harness/CAPABILITIES.json` является каталогом поставки. `init`, `adopt` и `update` разрешают
выбранные capability, копируют их в проект и фиксируют состав в lock-файле. `diff` показывает
локальный drift, а `update` не перезаписывает изменённые managed-файлы без явного `--force`.

Система использует следующие capability:

- `project-foundation` — минимальный доменно-нейтральный набор для проектирования, исследования,
  общего языка и agent-facing инструкций;
- `mattpocock-suite` — закреплённый byte-for-byte upstream snapshot skills;
- `pvmalove-suite` — инженерная надстройка над upstream-набором с project workflow, hooks,
  `qa-gate` и проектным контрактом;
- `backend-orchestration` — opt-in расширение `pvmalove-suite` для координации backend-работы
  несколькими изолированными ролями.

Проектный контракт `.harness/project.json` задаёт язык вывода, `base_branch`, шаблон issue-ветки и
команды QA. Локальные project-owned skills и runtime integrations фиксируются отдельными lock- и
inventory-файлами; секреты в них не записываются.

## 2. Интерактивный workflow

Для нового проекта entry skill `start-project` формирует durable-артефакт и выбирает capability.
Для существующего репозитория `integrate-project` сначала читает фактический стек, workflow и
действующие инструкции, затем предлагает совместимую установку. Обычный инженерный маршрут
`pvmalove-suite` начинается с тикета и использует зафиксированную integration-ветку эпика либо
`base_branch` для задачи без эпика.

Разработка идёт в изолированной issue-ветке, соответствующей `branch_pattern`. Агент выполняет
проверки из проектного контракта, коммитит и публикует только issue-ветку. Создание PR требует
отдельного явного подтверждения разработчика; merge всегда выполняет разработчик вручную. Правила
веток, тикетов, PR и закрытия задач определяет [git-workflow.md](./git-workflow.md), а команды и
полный skill workflow — [harness-guide.md](./harness-guide.md).

## 3. Backend orchestration и coordinator

`backend-orchestration` включается только явным выбором capability и валидной
`.harness/orchestration.json`. Без opt-in обычный `/implement` не меняется. Coordinator является
runtime-neutral локальным CLI: он планирует batch, требует явное человеческое approval для каждого
dispatch и принимает evidence. Он не является автономным scheduler.

Batch принадлежит одному тикету, issue-ветке, worktree и backend-зоне. Его lifecycle:
`planned → awaiting-approval ↔ active → completed | blocked | failed`. Каждый dispatch получает
новый immutable brief, а completion report переводит его в `reported`; следующий переход возможен
только после отдельного решения coordinator. Изменение scope, зоны, DoD, назначения или proof
закрывает текущий dispatch и требует нового brief. Retry после `blocked` или `failed` также создаёт
новый dispatch.

Для включения и проверки capability используются следующие команды:

```bash
python3 harness/bin/harness init /path/to/repository --capability backend-orchestration ...
python3 harness/bin/harness update /path/to/repository --capability backend-orchestration
python3 harness/bin/harness health /path/to/repository
```

Ключевой lifecycle coordinator использует `batch create`, `batch approve`, `dispatch create`,
`dispatch send`, `report submit` и `batch decide`:

```bash
python .harness/orchestration/coordinator.py --repo . batch create ...
python .harness/orchestration/coordinator.py --repo . batch approve ...
python .harness/orchestration/coordinator.py --repo . dispatch create ...
python .harness/orchestration/coordinator.py --repo . dispatch send ...
python .harness/orchestration/coordinator.py --repo . report submit --file report.json
python .harness/orchestration/coordinator.py --repo . batch decide ...
```

После candidate commit coordinator детерминированно оценивает риск по DoD, изменённым файлам и
reported triggers. High-risk candidate проходит независимый двухосевой `code-review`, затем
обязательный clean-room QA для того же SHA. После accepted QA coordinator создаёт publish dispatch;
только developer publish отправляет этот SHA. PR остаётся отдельным ручным этапом
`/to-pull-requests`. Optional Orca adapter доставляет только уже одобренный brief: он не выбирает
scope, не принимает report, не запускает проверки или следующий dispatch и не создаёт и не мержит
PR. Полный операционный маршрут описан в [backend-orchestration.md](./backend-orchestration.md).

## 4. Роли и capability

Role manifest в `.harness/orchestration/roles/` — переносимый поведенческий контракт роли.
Минимальный YAML frontmatter фиксирует имя, режим, обязательные capability и risk triggers; общий
контракт определяет brief, completion report, commit proof и escalation. Проектная
`.harness/orchestration.json` сопоставляет роли provider profiles, зоны, budget и обязательные
role-level `model`/`effort`. Она не может ослабить write boundary, proof или risk gate manifest-а.

`developer`, `database-migrations` и `messaging-integration` — write-роли. Они изменяют только
разрешённые пути объявленной зоны, только в issue-ветке и её worktree. `architect`, `qa` и
`code-review` — read-only роли. В одном batch одновременно активен только один writer, а handoff
между ролями выполняется последовательно. Независимые batch не пересекают service, bounded context
или infrastructure zone.

`architect` возвращает decision brief, `qa` — воспроизводимые findings без изменения тестов и
fixtures. Для API/public contract, schema/data migration, outbox/queues, transactions,
authorization/security и concurrency/retry `code-review` обязателен и возвращает раздельные
Standards и Spec reports. Назначение разрешается из manifest-а, project mapping с role-level
`model`/`effort` и допустимого one-run override; выбранный provider profile обязан удовлетворять
capability и ограничениям роли.

## 5. Clean-room QA lane

QA dispatch выполняет coordinator, а не runtime adapter. Он создаёт временный detached Git
worktree ровно на `candidate_commit`, проверяет чистоту worktree и совпадение HEAD, затем запускает
дословный набор `verification_commands` из immutable brief:

```bash
python .harness/orchestration/coordinator.py --repo . qa run --dispatch <qa-dispatch-id>
python .harness/orchestration/coordinator.py --repo . qa status
```

Тяжёлые проверки используют одну FIFO quality-gate lane. Занятый dispatch получает очередь и
может стартовать только первым. Lease хранит dispatch ID, host, PID, время взятия и expiry;
истёкший lease снимается только после явной проверки owner и решения coordinator через
`qa clear-stale-lease`. Gate failure остаётся QA finding: runner не изменяет код и не перезапускает
проверку сам.

## 6. State и локальное evidence

Coordinator хранит batches, dispatches, immutable briefs, reports, решения и QA evidence в
`.harness/orchestration/state/`. Содержимое каталога gitignored: это локальное санитизированное
evidence, а не исходный код проекта. Роли и adapter не удаляют историю batch.

Полные санитизированные stdout/stderr QA хранятся вне Git в
`state/qa-artifacts/<sha256>.log`; completion report содержит путь к артефакту и checksum. Brief и
report не содержат значения секретов. State остаётся локальным до явного безопасного решения
coordinator об очистке.

Для правил задач и веток используйте [git-workflow.md](./git-workflow.md), для tracker и меток —
[issue-tracker.md](./issue-tracker.md) и [triage-labels.md](./triage-labels.md), для временных
артефактов — [artifacts.md](./artifacts.md), а для параллельных пользовательских worktree —
[worktrees.md](./worktrees.md).
