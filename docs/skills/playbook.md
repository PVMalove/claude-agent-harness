# Playbook — Руководство по оркестрации

Источник: [`harness/orchestration/playbook.md`](../../harness/orchestration/playbook.md).

## 1. Название и локализация

- **Английское название:** `Backend Batch Orchestration Playbook`
- **Русский перевод:**

```text
Руководство по оркестрации backend-batch
```

## 2. Полное описание

Playbook — runtime-neutral контракт координации для необязательной возможности `backend-orchestration`. Он задаёт ручной протокол координатора, а не механизм dispatch, выбор провайдера или обязательный Orca adapter. Первичный источник определяет полномочия координатора, неизменяемые brief и reports, конечный автомат batch, порядок ролей, правила liveness, условия параллелизма, quality-gates и базовые метрики.

Координатор единолично принимает completion report, управляет lifecycle, approval dispatch и изменениями scope. Каждый batch имеет один тикет, issue-ветку и изолированный worktree; один write-role активен в момент времени. Изменение scope, зоны, DoD, назначения либо необходимого доказательства завершает dispatch и требует нового immutable brief.

Context Package — immutable ledger-owned результат детерминированного `context_builder.py`. Он
переиспользуется ролями одного batch, а его freshness перед каждым dispatch записывается в shadow
режиме. Checkpoint — отдельная non-terminal запись только для write-роли; она позволяет начать новую
worker session под тем же dispatch ID без переноса истории чата и traceback. Recognized rate-limit
авторизует resume автоматически, planned trigger требует существующего coordinator decision.

Перед review и publish coordinator выполняет base-commit gate: после `git fetch origin <integration_ref>`
актуальная вершина должна совпадать с batch base. Drift устраняется новым developer/rebase dispatch.
Delta-review после Warning разрешён только для test-only diff и всегда является новым независимым
review dispatch. Advisory tool call остаётся эфемерным non-role выводом и не может авторизовать работу.

## 3. Контракты

- **Вход (Input/Brief):** тикет, зона, issue-ветка/worktree, DoD, запреты, команды проверки, назначение провайдера/модели и явное одобрение человека.
- **Процесс (Process):** `planned → awaiting-approval → active → awaiting-approval`, затем `completed`, `blocked` или `failed`; Architect предшествует Developer; reports принимаются только при достаточном независимом доказательстве.
- **Выход (Output/Report):** Context Package, checkpoints, неизменяемые dispatch briefs, completion reports, принятый конечный статус batch и наблюдаемая базовая линия метрик.

## 4. Архитектурная схема

![Контракт playbook: вход, координация, результат](../diagrams/previews/skill-contract-fill.workflow.png)
