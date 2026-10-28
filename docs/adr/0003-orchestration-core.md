# Переносимое ядро оркестрации

> Harness Next (ADR 0020, ADR 0021): после cutover в релизе 2.0.0 operational state пишет только
> Rust runtime store. Python-ядро остаётся владельцем решений lifecycle, approval и публикации.

## Контекст системы

Backend-работа несколькими ролями нуждается в едином lifecycle, approval и доказательствах,
одинаковых для разных coding runtime. Transport умеет запустить роль, но не должен решать,
достаточен ли её отчёт, какую роль запускать дальше и можно ли публиковать candidate.

## Действующий контракт

`backend-orchestration` — явная capability поверх `engineering-suite`. Она поставляет роли,
конфигурацию, coordinator, ledger, Context Builder и clean-room QA. Ядро в
`harness/orchestration/` владеет batch, dispatch, approval, immutable audit, проверкой policy,
очередью QA и решением о публикации. Ledger хранит versioned lifecycle и не переписывает
evidence задним числом. `contract.py` валидирует project config, назначения, briefs и reports.
`coordinator.py` и `workflow/` проводят переходы; `harness/gate_runner/` исполняет
проверочные команды по выбранной execution policy и возвращает структурированное evidence,
не меняя batch state.

Batch проходит `planned → awaiting-approval ↔ active`. `paused`, `blocked` и `failed`
сохраняют работу и допускают явное ручное восстановление; `completed` и `abandoned` терминальны.
После остановки auto не возвращается: `resume-stop` и `rewind` оставляют исходный stop и
включают эффективный `manual_all`. Sealed recovery event выбирает активную цепочку evidence,
сохраняет исторические записи и связывает ручное решение с переходом в audit. Rewind не меняет
Git history и не обнуляет бюджеты. Новый writer стартует от известного сохранённого SHA.
Переход control runtime разрешён только audited recovery-событием; исходный pin plan сохраняется.
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

## Наблюдение worker и native permissions (#716)

Ядро сохраняет receipt и startup evidence, а adapter владеет запуском и исходным native-каналом.
Handoff резервируется под ledger lock; вызов внешнего или native runtime выполняется после его
освобождения. Self-report не теряется при возврате адаптера. Наблюдение — локальное чтение
существующих событий, без дополнительных LLM-вызовов и сообщений в контекст агентов.
Native permission request передаётся человеку через control protocol того же runtime; ответ
разрешает только исходный запрос один раз и не заменяет dispatch approval. Консоль использует
существующие тему и навигацию, не создаёт вторую lifecycle-модель. Exit code, ledger, готовность
отчёта и его приёмка отображаются отдельно.
