# Runtime-neutral ядро оркестрации

## Контекст системы

Харнесс поддерживает несколько runtime и нуждается в общем способе координации ролей, назначений,
batches и handoff без передачи authority runtime-specific transport.

## Действующий контракт

Portable orchestration core определяет роли, assignments, batches, dispatches и handoff. Orca
подключается только как optional runtime adapter. Человек утверждает каждый dispatch; проектная
конфигурация разрешает роль, agent, model, fallback и допустимый one-run override.

## Операционные последствия

Контроль стоимости, безопасности, параллелизма и следующего lifecycle-перехода остаётся у
разработчика и coordinator. Workflow rules одинаково применимы с adapter и без него.
