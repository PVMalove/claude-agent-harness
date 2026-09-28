# Управляемый конвейер `/implement` и короткий маршрут `/fast-implement`

## Контекст системы

Один тикет должен пройти от принятого архитектурного решения до проверенного commit без потери
контекста между ролями. Изменение candidate, базы или условий проверки меняет смысл решения
оператора; разрешение должно быть привязано к конкретному переходу. Небольшая работа с заранее
ясным направлением может обходиться без полного ролевого конвейера.

## Действующий контракт

### Маршрут с coordinator

`/implement` обрабатывает один тикет и ведёт batch через `architect → developer → code-review →
qa → publish`. Перед `batch create` он проверяет тикет, блокеры, status, issue-ветку и
ограничения объёма; слишком крупный тикет возвращается на декомпозицию. Сессия coordinator не
пишет продуктовый код. `coordinator.py` запрещает developer-dispatch без принятого architect
report того же batch. Публикация завершённого batch не создаёт PR.

Перед handoff `dispatch preflight` проверяет проектный контракт. Context Builder детерминированно
собирает Context Package из закреплённых commit, отфильтрованных файлов и Repo Map. Пакет
регистрируется в ledger и повторно используется architect, developer и продолжениями того же
dispatch, пока candidate не изменился. Новый candidate для review получает новый общий пакет.
Immutable brief закрепляет package ID, scope, DoD, разрешённый model/effort, транспорт, зоны,
команды проверки и путь для отчёта. Writer brief также содержит упорядоченный commit plan;
completion report связывает каждый созданный commit с пунктом плана.

`dispatch propose` показывает decision packet и `transition_digest`. При явном approval
`dispatch create` сверяет digest с текущими данными ledger; изменённые candidate, scope,
Context Package или команды требуют нового предложения. Проект выбирает `manual_all`,
`low_risk` или `milestone` policy: автоматическое принятие чистого отчёта возможно только там,
где это допускает policy; риск, finding, сбой и publish сохраняют соответствующие ручные
решения. In-process и external transport исполняют один brief и возвращают одинаковое evidence.
Model self-report и watchdog выявляют mismatch или потерю живости, не меняя brief.

`batch create` закрепляет integration base. Перед review и publish coordinator сверяет её с
`origin/<integration_ref>`; устаревшая база требует нового developer-dispatch для rebase и нового
candidate assessment. Стандарты и спецификация проверяются независимыми отчётами. Delta-review
разрешён только для нового test-only исправления конкретного finding при выполнении условий
coordinator; иначе проводится полное review. QA запускается в clean-room worktree через
сериализованную lane и привязывается к точному candidate SHA.

Writer-dispatch может оставить checkpoint на зелёной границе с commit SHA, оставшимся DoD и
результатами проверок и продолжиться в новой worker session после повторной self-report.
Read-only роли используют новый dispatch. Продолжения и retry ограничены проектным бюджетом;
429 не даёт бесконечного повтора. Наблюдаемое `needs_attention` — флаг batch: он блокирует
новый dispatch, пока оператор не разрешит причину, но не создаёт отдельного lifecycle state.

### Короткий маршрут

`/fast-implement` используется для `afk`-тикета с явным `pipeline::fast` и малым радиусом
изменений. Он реализует задачу в одной сессии через TDD и проверки, без architect шага,
независимой QA-роли и approval gates coordinator. Перед code review и перед commit/push этот
маршрут запрашивает отдельные решения разработчика. Для `hitl`-тикета предназначен `/to-guide`.
Оба маршрута после публикации своей issue-ветки лишь предлагают `/to-pull-requests`; открытие PR
и merge остаются отдельными действиями разработчика.

## Операционные последствия

Оператор видит decision packet и утверждает фактический переход, а не абстрактное намерение.
Отчёт роли является evidence, а не разрешением на следующий шаг. Candidate, base, Context
Package, review и QA должны совпадать по закреплённым идентификаторам. Локальный workflow
после принятых проверок публикует issue-ветку; PR требует отдельного подтверждения.
