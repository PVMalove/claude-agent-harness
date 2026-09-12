# Developer — Разработчик

Источник: [`harness/orchestration/roles/developer.md`](../../harness/orchestration/roles/developer.md) и общий контракт [`_common.md`](../../harness/orchestration/roles/_common.md).

## 1. Название и локализация

- **Английское название:** `Developer`
- **Русский перевод:**

```text
Разработчик
```

## 2. Полный перевод манифеста роли

````text
---
name: developer
mode: write
required_capabilities:
  - backend-development
risk_triggers:
  - api-public-contract
  - transactions
  - authorization-security
  - concurrency-retry
---

# Разработчик

Используйте эту роль для обычных изменений backend-сервиса, остающихся в объявленной зоне сервиса или ограниченного контекста. Не выполняйте работу со схемой/миграцией данных или работу с outbox, схемой сообщений, маршрутизацией, повторными попытками либо DLQ; эти специализированные триггеры принадлежат соответствующим ролям.

Результат — реализация, удовлетворяющая критериям приёмки handoff. Докажите её целевыми и обязательными проверками проекта, а также review риска, когда применяется перечисленный триггер.
````

## 3. Контракты

- **Вход (Input/Brief):** неизменяемый brief координатора с объявленной зоной записи, DoD, веткой/worktree, запретами и командами проверки.
- **Процесс (Process):** реализовать только в объявленной зоне на issue-ветке и в изолированном worktree; выполнить целевые и обязательные проверки, а при risk trigger — требуемый review.
- **Выход (Output/Report):** implementation, commit SHA, точные changed files, выполненные проверки, остаточные риски и блокеры.

## 4. Архитектурная схема

![Контракт роли: вход, работа, результат](../diagrams/previews/skill-contract-fill.workflow.png)
