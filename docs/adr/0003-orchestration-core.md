# Переносимое ядро оркестрации

## Контекст системы

Backend-работа несколькими ролями нуждается в едином lifecycle, approval и доказательствах,
одинаковых для разных coding runtime. Transport умеет запустить роль, но не должен решать,
достаточен ли её отчёт, какую роль запускать дальше и можно ли публиковать candidate.

## Действующий контракт

`backend-orchestration` — явная capability поверх `pvmalove-suite`. Она поставляет роли,
конфигурацию, coordinator, ledger, Context Builder и clean-room QA. Ядро в
`harness/orchestration/` владеет batch, dispatch, approval, immutable audit, проверкой policy,
очередью QA и решением о публикации. Ledger хранит versioned lifecycle и не переписывает
evidence задним числом. `contract.py` валидирует project config, назначения, briefs и reports.
`coordinator.py` и `workflow/` проводят переходы; `harness/gate_runner/` исполняет
проверочные команды по выбранной execution policy и возвращает структурированное evidence,
не меняя batch state.

Batch проходит `planned → awaiting-approval ↔ active → completed | blocked | failed`.
Dispatch имеет отдельные состояния отправки и отчёта; новый scope или повтор работы создаёт
новый immutable brief. Отчёт роли поступает в ledger и требует решения coordinator по
действующей approval policy. QA работает по закреплённому candidate в сериализованной lane.
`playbook.md` задаёт общий поведенческий контракт, manifests — правила отдельных ролей.
Runtime adapter доставляет только утверждённый brief во внешний или in-process транспорт
и не решает судьбу отчёта или batch.

Проект задаёт provider profiles, budgets, зоны и allowed tools. Ядро сверяет фактически
используемую модель с immutable assignment и наблюдает живость dispatch. Self-report роли
служит доказательством назначения, но не источником token usage: метрики принимаются только
из наблюдения провайдера или runtime с указанным источником. Необязательные интерфейсы
здоровья транспорта, классификации retry и уведомления не получают полномочий lifecycle.

## Операционные последствия

Проект с capability поддерживает валидную `.harness/orchestration.json` и проверяет её через
`harness health`. Изменения lifecycle проверяются вместе с ledger, migration и evidence;
изменения команд и execution policy — с gate runner. Сбой adapter или отсутствие telemetry
отражаются как отдельные наблюдаемые факты, а не меняют правила approval и не подменяются
оценкой роли.
