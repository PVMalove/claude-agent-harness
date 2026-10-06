# Продолжение PR: следующий шаг и маршрутизация провала проверки

## Контекст системы

Операции актуализации готовы по отдельности: `integration refresh` (ADR 0014), resolver (ADR 0015),
`collect-ci` (ADR 0016) и запасной `local-qa`. `/to-pull-requests` вызывал только `status` и
`refresh`: не было ни ветви resolver, CI и запасного пути, ни привязки подтверждения к паре SHA, ни
сводки для ручного merge. Классификацию «куда идти дальше» нельзя оставлять прозой скила: её нужно
проверять публично и одинаково для любого runtime. Новой персистентной машины состояний в ledger
это не оправдывает: счётчик бюджета resolver уже выводится из append-only событий, и дублировать его
значит сломать правило «счётчик не хранится, а выводится».

## Действующий контракт

- **Команда `integration next`.** `integration next --ticket T --branch B [--pull-request N]` —
  строго read-only: не пишет Git, ledger, dispatch и PR и не обращается к трекеру. Она строится из
  `integration status`, цепочки refresh, evidence-ссылок, открытых resolver batch и событий бюджета.
  Шаги: `unavailable` (integration ref не читается — операционная остановка), `resolver-open`
  (открыт resolver batch: ждать, цикл не тратится), `refresh`, `route-failure` или `human-decision`
  (провал проверки текущей пары), `confirm-pr` (до PR; старое evidence допускает вход в подготовку
  PR и не считается QA нового candidate), `verify` (с `--pull-request`: обновлённая пара ждёт CI
  либо local-QA), `handoff` (пара актуальна и проверена: `candidate_sha`, `target_sha`, `qa_source`
  `original-qa|ci|local-qa`, ссылка на evidence, признак refresh). Устаревшая пара всегда ведёт к
  refresh, а не к handoff.
- **Правило маршрута провала.** Провал считается только записанным collector-ом CI
  (`collector-failed`) или сгенерированным gate-ом local-QA (`state: failed`) именно текущей пары, без
  позднего passed той же пары. Если пара прошла refresh или resolver (`refreshes` непусты),
  провал — следствие сочетания с target или правки resolver: маршрут `resolver`, в пределах бюджета;
  при исчерпании шаг `human-decision` (`integration resolver-event --extends-budget`). Если пара
  исходная (`refreshes` пусты), провал — собственный дефект задачи: маршрут `developer` с review и QA
  (ADR 0012). Завершённый исходный batch терминален (`batch decide` на нём отказывает: нет отчёта,
  ожидающего решения), поэтому исполнимый путь — новый batch того же тикета и issue-ветки обычным
  маршрутом `/implement`: `batch create --ticket T --branch B --worktree W --integration-ref I`, затем
  architect, developer, code-review, QA и publish; завершённый batch новый не блокирует
  (`_reject_duplicate_work` пропускает завершённые batch). База нового batch — integration tip, поэтому
  developer-отчёт отображает в `commit_map` и уже опубликованные коммиты ветки (с `dod_coverage` и
  `divergence_justification`, как при неоднозначном отображении). После принятого publish
  `integration prepare --ticket T --branch B --batch <новый batch>` создаёт новую запись, а
  `integration next --record <новая запись>` продолжает PR; упавшее evidence прежней записи остаётся историей. Операционный сбой (fallback CI, `unavailable` и `exhausted` local-QA) failed-evidence не
  создаёт, поэтому не может стать «провалом кода»: инвариант структурный, не прозой.
- **Расширение `integration resolve`.** Прежний отказ «ветка уже на tip» сохраняется, если нет
  провалившейся проверки обновлённой пары. При такой проверке создаётся resolver batch с
  `resolver.trigger: verification-failure` (`failed_evidence_ids`, пустые `conflicting_files`,
  scope без rebase-пробы); у конфликта `trigger: conflict`. У обновлённой пары merge-base равен tip, поэтому
  сторона `target` берётся не из `merge_base..tip` (она пуста), а из коммитов, которые легли в target
  после исходного target записи (`identity.target_sha..tip`), тем же источником, что и у конфликта:
  план тикета из `(#N)` в теме, иначе тема и тело коммита. Scope не расширяется: это собственные файлы
  задачи; правка файла, который изменил target вне диффа задачи, — новый утверждённый dispatch
  (`scope-change`), как у конфликта. Событийная модель бюджета не меняется:
  первый отчёт resolver на target тратит цикл, следующие правки того же target — `same-target-fix` в
  пределах `retry_policy.max_developer_retries`. Чистый rebase, ожидание CI и ответ человека цикл не
  тратят, а бюджет переживает продолжения PR, потому что выводится из событий.
- **Подсказка `collect-ci`.** Результат несёт `next: {action, ci_condition}` по одной таблице:
  `pending_check` — `wait`; `not_configured`, `unsupported_tracker` — `local-qa` с `absent`;
  `unavailable` — `local-qa` с `unavailable`; остальные fallback — `local-qa` с `unusable`; `failed` —
  `route`. У `accepted` подсказки нет: полный local QA пропускается.
- **Скил.** `/to-pull-requests` оркестрирует: до PR отдельное подтверждение человека называет точную
  пару `candidate_sha`/`target_sha`; перед открытием PR `integration next` повторяется, и смена пары
  делает подтверждение недействительным. PR открывается через CLI трекера с `--base` целью и файлом
  тела, completion report публикуется при `task-report::required`, метаданные только проектные.
  После открытия или обновления PR идёт ожидание CI и проверка комбинированного результата, затем
  запасной local-QA, затем сводка merge-handoff. Resolver PR не открывает и не сливает; merge ручной,
  server branch protection и merge queue не включаются.
- **Маркер hook.** `record-qa-gate-pass.sh` для обновлённого SHA означает лишь разрешение открыть PR;
  проверка новой пары идёт после открытия PR.
- **Граница обещания.** Handoff не обещает неизменность между проверкой и merge: перед merge шаг
  повторяется, новый target запускает refresh и новую проверку.

## Операционные последствия

- Ledger schema не меняется, `ledger migrate` не нужен; новая запись и новая персистентность не
  вводятся. Номер PR не хранится: его передаёт `--pull-request`.
- `integration next` не предсказывает текстовый конфликт: refresh пишет в Git, поэтому конфликт
  остаётся результатом `integration refresh`.
- Решение трудно откатывать частично: правило маршрута и расширение `resolve` связаны с бюджетом
  ADR 0015 и таблицей причин ADR 0016, поэтому их изменение требует нового ADR.
