# Implement — Реализация

## 1. Название и локализация

- **Английское название:** `Implement`
- **Русский перевод:**

```text
Реализация
```

## 2. Полный перевод текста скила

````text
---
name: implement
description: Провести один backend-тикет через утверждённые handoff архитектора, разработчика, review, QA и publish.
disable-model-invocation: true
---

# Реализация

**Цель:** Провести ровно один тикет через `architect → developer → code-review → qa → publish`,
затем предложить `/to-pull-requests`. Эта сессия — coordinator: она создаёт и наблюдает dispatch,
но не реализует тикет сама.

## Маршрут

Это opt-in маршрут `backend-orchestration`. Подтвердите, что установленный coordinator доступен:

```bash
python .harness/orchestration/coordinator.py --repo . dispatch status
```

Если команда недоступна или не читает state, остановитесь и предложите `/fast-implement`. Не
исправляйте и не подразумевайте opt-in capability. Завершите один batch до старта следующего.

## Контракт coordinator-а

Разрешите тикет, integration-ветку и блокеры по tracker/Git guidance. Используйте isolated
issue-ветку и worktree; protected и `integration/*` — не write targets. Открытый batch — evidence,
которое показывают разработчику, а не запись для повторного использования или замены.

Предлагайте каждый handoff и останавливайтесь до явного approval разработчика перед созданием или
отправкой dispatch. Report — evidence, а не authority продвигать batch. Architect обязателен перед
developer dispatch; review хранит отдельные Standards и Spec evidence; независимый QA проверяет
candidate commit; publish отправляет только accepted SHA. Final report предшествует отдельно
одобренному publish dispatch.

Каждая dispatched role сначала пишет model self-report относительно immutable brief и посылает
heartbeat. Между send и report coordinator опрашивает watchdog. Mismatch или stale dispatch —
blocker; recovery создаёт новый approved dispatch, а не редактирует brief или state.

Для `in-process` transport send фиксирует immutable handoff, после чего coordinator немедленно
запускает role subagent в указанном worktree. External adapter является только transport и сохраняет
тот же handoff, liveness, approval и report contract.

## Авторитетные guidance

Это короткий coordinator contract, а не вторая orchestration-процедура. Полные правила —
module-owned guidance:

- `.harness/orchestration/playbook.md` владеет lifecycle, authority, immutable brief, completion
  evidence, parallelism и metric rules.
- `.harness/orchestration/roles/` владеет boundary, required proof и specialist trigger каждой роли.
- `docs/agents/backend-orchestration.md` владеет setup, project configuration, CLI procedure и
  operational recovery.
- `docs/agents/git-workflow.md` владеет issue-branch, commit, push и PR boundaries.

Следуйте этим файлам, не дублируя и не ослабляя их правила. Не придумывайте token metrics:
используйте только provider- или runtime-observed telemetry и сохраняйте missing-data notes.

## Завершение

После accepted publish предложите `/to-pull-requests <ticket>`. Не запускайте его автоматически,
не открывайте и не мержьте PR, не пишите в integration-ветку и не закрывайте тикет этим скилом.
````

## 3. Контракты

- **Вход:** AFK-тикет, установленная opt-in capability и явные approvals разработчика.
- **Процесс:** Coordinator проводит пять handoff, проверяет model self-report и liveness, принимая
  только доказанные reports.
- **Выход:** Accepted candidate SHA, QA evidence и предложение `/to-pull-requests`.

## 4. Архитектурная схема

![Контракт скила: вход, работа, результат](../diagrams/previews/skill-contract-fill.workflow.png)
