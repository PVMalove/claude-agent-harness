# Coordinator-driven `/implement` как путь по умолчанию

## Контекст

Тикет `#366` в проекте `ProductsFlow_AI` прошёл через уже существующую `backend-orchestration` +
Orca: brief был корректно approved, `required_gates` уже включали `code-review` и `qa`. Но
`resolved_model` дошёл до Orca-воркера как отображаемое имя «Sonnet 5» вместо CLI-алиаса `sonnet`,
которым он верно задан в `.harness/orchestration.json`. Воркер не смог стартовать, не написал ни
строчки кода, а watchdog на стороне coordinator отсутствовал — `playbook.md` определяет
heartbeat-конвенцию только как обязанность самого Orca-воркера, без проверки на стороне coordinator.
В результате ушло около 30% пятичасового окна впустую. Отдельно, сам `/implement` (без
`backend-orchestration`) никогда не имел architect-шага и гейтов вовсе — путь, который пользователь
интуитивно ожидал от команды `implement <issue>`, никогда не существовал.

## Решение

`/implement` становится coordinator-driven конвейером по умолчанию для любого тикета: сессия сама
ведёт `coordinator.py` (batch create/approve, dispatch create/send, report submit, batch decide) и
паузится на пяти существующих точках approval — architect, developer, code-review, qa, publish; PR
остаётся отдельной явной командой `/to-pull-requests`. `coordinator.py` жёстко запрещает создавать
`developer`-dispatch без принятого `architect`-отчёта для того же batch, независимо от точки входа
(ручной CLI или скрипт `/implement`). Каждый dispatch — независимо от транспорта — обязан пройти
model self-report (сверка фактически активной модели с `resolved_model` brief) и подчиняется
dispatch watchdog на стороне coordinator-сессии, обобщающему уже существующий QA-lease-expiry.
Транспорт (Orca-dispatched isolated worker или in-process субагент) становится per-role выбором в
assignment plan, а не жёстко зашитым механизмом. Проект без `.harness/orchestration.json`
по-прежнему получает рабочий дефолт: zone = весь репозиторий, model/effort роли = текущая сессия.
Прежний однопроходный upstream-флоу переезжает в новый skill `fast-implement`.

## Последствия

Пересматривает строку ADR 0003 «обычный `/implement` должен сохранять стандартный workflow» — этот
workflow теперь живёт в `fast-implement`, а не в `/implement`. Batch, запущенный через `/implement`,
не использует разрешённый playbook-ом кросс-batch параллелизм: один тикет проходит весь конвейер
целиком, прежде чем coordinator-сессия берётся за следующий.
