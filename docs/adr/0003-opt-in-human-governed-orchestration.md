# Opt-in оркестрация backend-работы с человеческим контролем

## Контекст системы

Некоторые backend-задачи требуют изолированных ролей, воспроизводимых handoff и независимого QA,
тогда как обычный `/implement` должен сохранять стандартный workflow.

> Пересмотрено в [ADR 0014](./0014-coordinator-driven-implement-pipeline.md): стандартный
> однопроходный workflow теперь живёт в `fast-implement`, а `/implement` стал coordinator-driven
> конвейером.

## Действующий контракт

`backend-orchestration` — необязательная capability поверх `pvmalove-suite`. Role manifests и
`.harness/orchestration.json` задают доступ, write zones, назначение и проверки. Runtime-neutral
coordinator ведёт локальный санитизированный state, immutable briefs и reports. Каждый batch и
dispatch требуют отдельного явного approval, а completion report требует отдельного решения
coordinator.

Для candidate SHA coordinator детерминированно оценивает риск, при trigger получает независимые
Standards и Spec reports и затем запускает обязательный clean-room QA. Runtime adapter доставляет
только уже одобренный dispatch; он не принимает reports, не планирует следующий шаг и не создаёт
и не мержит PR.

## Операционные последствия

Проект с этой capability поддерживает валидную `.harness/orchestration.json` и явно утверждает
dispatch и решения. Evidence остаётся локальным до решения coordinator; stale QA lease
восстанавливается только после явной проверки owner.
