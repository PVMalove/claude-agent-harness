# Необязательная capability backend-оркестрации

## Контекст системы

Backend orchestration применяется только там, где проекту требуются роли, project-owned provider
profiles, контролируемые handoff и clean-room QA.

## Действующий контракт

`backend-orchestration` расширяет `pvmalove-suite` и поставляет шесть role manifests, schema и
template `.harness/orchestration.json`, health validation, coordinator lifecycle, handoff rules,
clean-room QA и optional Orca adapter. Конкретные provider определяет проект в provider profiles;
assignment plan каждой роли обязательно задаёт её model и effort.

Coordinator ведёт lifecycle `planned → awaiting-approval ↔ active → completed | blocked | failed`.
Каждый report переводит dispatch в `reported`, а повтор работы всегда создаёт новый dispatch с
новым immutable brief.

## Операционные последствия

Установки без явного выбора capability сохраняют обычный workflow. Проект с capability проходит
`harness health` после установки или изменения orchestration-конфигурации.
