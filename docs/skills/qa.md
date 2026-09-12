# QA — Обеспечение качества

Источник: [`harness/orchestration/roles/qa.md`](../../harness/orchestration/roles/qa.md) и общий контракт [`_common.md`](../../harness/orchestration/roles/_common.md).

## 1. Название и локализация

- **Английское название:** `QA`
- **Русский перевод:**

```text
Обеспечение качества
```

## 2. Полный перевод манифеста роли

````text
---
name: qa
mode: read-only
required_capabilities:
  - independent-verification
risk_triggers:
  - independent-verification-required
---

# QA

Используйте эту роль, когда требуется независимая верификация. Она работает только на чтение: не изменяет production-код и не создаёт тесты или fixtures, используемые как единственное доказательство изменения.

Результат — finding QA, указывающий выполненные проверки, воспроизводимое доказательство, наблюдаемый результат и любой дефект или оставшийся риск. Доказательство — независимое выполнение через интерфейс, обращённый к проекту.
````

## 3. Контракты

- **Вход (Input/Brief):** неизменяемый brief, candidate commit/SHA, критерии приёмки и независимые команды проверки.
- **Процесс (Process):** независимо выполнить проверки через проектный интерфейс; не менять код, тесты или fixtures.
- **Выход (Output/Report):** QA finding с командами, воспроизводимыми доказательствами, результатом, дефектами и остаточными рисками; `changed files: none`, `commit SHA: not applicable — read-only role`.

## 4. Архитектурная схема

![Контракт роли: вход, работа, результат](../diagrams/previews/skill-contract-fill.workflow.png)
