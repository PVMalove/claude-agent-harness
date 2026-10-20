# Базовый замер производительности 1.x

Замер «до» для эпика Harness Next (#784, тикет #795). С ним сравнивается замер «после» по ADR
0019, 0020 и 0023. Здесь приведены только числа из файлов `raw/` — вывода
`scripts/perf/baseline.py`. Метрика, которую не удалось измерить, помечена `нет данных` и не
заменяется оценкой.

## Что измерено

- **Код:** чистый detached checkout коммита `a4f485b` ветки
  `integration/harness-runtime-reliability` (`raw/environment.json`: `worktree_clean: true`). После
  переписывания истории integration то же дерево (`50f06b6`) носит коммит `9ede593`. Это
  Python-код 1.x после импорта `integration/orchestration-ledger` и
  `integration/container-executor`, без Rust runtime и кода Harness Next. Coordinator, pytest и
  `make verify` запускались из этого checkout, а инструмент замера — из issue-ветки.
- **Окружение:** Linux 6.8 x86_64, 8 CPU, Python 3.12.3. Замер начат 2026-10-20 в 11:34 UTC и
  занял 2561 с (`raw/run.json`).
- **Данные ledger:** копия `.harness/orchestration/state` этого репозитория без
  `.coordinator.lock`, положенная в `.harness/orchestration/state` измеряемого checkout. В
  поколении `generation-7e05daec` 44 batch, 267 dispatch и 5044 audit-записи
  (`raw/ledger-size.json`). Живой ledger в это время использовала другая сессия, поэтому все
  команды выполнялись только на копии, а после замера копия удалена.

## Как воспроизвести

```bash
git worktree add --detach <checkout> <commit>
# в <checkout>: .harness/.venv и make bootstrap
python .harness/skills/qa-gate/scripts/test_summary.py -- bash -lc \
  '.harness/.venv/bin/python -m scripts.perf.baseline --repo <checkout> \
     --state-dir <main-checkout>/.harness/orchestration/state \
     --out docs/perf/1.x-baseline/raw \
     --repeats 30 --slow-repeats 5 --test-repeats 3 --full-verify'
```

`--repo` должен быть одноразовым checkout без `.harness/orchestration/state`: инструмент кладёт туда
копию state и удаляет её после замера команд. QA lane не принимает `--state-dir`, поэтому state
копируется именно в checkout.

## Методика

- **Перцентили** считаются методом nearest-rank: p-й перцентиль — значение с рангом ⌈p·n/100⌉ в
  упорядоченной выборке. В расчёт входят только прогоны с кодом 0. При n < 20 p95, а при
  n < 100 p99 совпадают с максимумом.
- **Длительность `/implement`** — время от `created_at` batch до последнего `updated_at` статусов
  его dispatch. Это время по часам: оно включает ожидание решений разработчика и паузы между
  сессиями. Публикация, PR и merge после последнего dispatch в него не входят: по ledger их конец
  не определить. Batch без статусов dispatch получают `null`, а не 0.
- **Полный прогон** — команда из `checks_run` отчёта dispatch: `scripts/verify.py` без `--only`,
  `make verify` или pytest по всему каталогу `tests` (или без путей) без опций отбора `-k`, `-m`,
  `--lf`. Команда делится по `&&`, `||`, `;`, `|`, а `bash -c`/`bash -lc` раскрывается. Источник —
  отчёт роли, то есть то, что роль записала о своих прогонах; coordinator этот список не
  перепроверяет.
- **Команды coordinator** выполняются подпроцессом с `--repo` измеряемого checkout. Если первый
  прогон длился дольше 10 с, команда получает `--slow-repeats` повторов, иначе `--repeats`. После
  первого отказа повторы прекращаются.
- **`dispatch heartbeat`** принимает только живой dispatch. В копии state такого не было, поэтому
  инструмент перевёл в копии последний `reported`-статус в `working`
  (`raw/coordinator.json`: `live_state: synthetic`). Путь записи heartbeat (lock, перезапись
  статуса, audit-запись) от исходного состояния не зависит. Каждый прогон добавляет в копию одну
  audit-запись.
- **Стоимость проверки audit** измерена двумя числами. Первое — `LifecycleLedger._validate_audit`
  измеряемого кода на audit-каталоге копии: пересчёт sha256 каждой записи (`raw/audit.json`). Время
  включает запуск интерпретатора и импорт `lifecycle`. Второе — `ledger validate` целиком: кроме
  audit, он разбирает все записи, проверяет планы batch и граф ссылок.
- **Focused-прогон** — pytest по набору из ADR 0023: `test_verification_profiles.py`,
  `test_retry_verification.py`, `tests/core/test_gate_runner.py`, `test_qa_lane.py`,
  `tests/scripts/test_verify_temp.py`. Итоговая строка pytest каждого прогона записана в
  `summary_line`.
- **Failed-only-прогон** — `pytest --lf --lfnf=none tests` с чистым `cache_dir`. В его
  `lastfailed` записан один node id — тест из `qa_gate_commands`. Этот тест проходит: реального
  падения нет, поэтому замер показывает нижнюю границу повторного прогона одного упавшего теста.
  В 1.x нет failed-only-режима для `/implement`: `verify.py --failed-first` переупорядочивает тесты,
  но прогоняет весь набор.
- **Полный `make verify`** — один прогон на чистом `a4f485b`. Время стадий взято из строк
  `[verify] <stage>: passed in X.XXs`.
- **История QA** — результаты integration local QA (`raw/local-qa.json`) и стадии
  `scripts/verify.py` из 34 QA-логов поколения (`raw/qa-artifact-stages.json`). Перцентили стадий
  считаются только по стадиям со статусом `passed`; упавших стадий в этих логах нет.
- **Редакция:** инструмент заменяет в `raw/make-verify.log` пути checkout, основного checkout и
  домашнего каталога на `<repo>`, `<main-checkout>` и `<home>` и удаляет ANSI-коды.

## Результаты

### `/implement` по истории ledger

Batch с 2026-10-01 по 2026-10-20: 30 `completed`, 10 `abandoned`, 3 `failed`, 1
`awaiting-approval` (`raw/implement-runs.json`).

| Метрика (30 batch `completed`) | min | p50 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- |
| Длительность по часам, мин | 26,5 | 129,7 | 304,0 | 920,7 | 920,7 |
| Полных прогонов на batch | 0 | 6 | 10 | 10 | 10 |
| Из них у developer | 0 | 3 | 5 | 5 | 5 |
| Из них у code-review | 0 | 2 | 4 | 4 | 4 |
| Из них у qa | 0 | 1 | 1 | 1 | 1 |

Всего у 30 завершённых batch 171 полный прогон: developer — 87, code-review — 55, qa — 28,
conflict-resolver — 1.

### Полный прогон

| Источник | n | p50, с | p95, с | max, с |
| --- | --- | --- | --- | --- |
| `make verify` на чистом `a4f485b` | 1 | 601,5 | — | — |
| Integration local QA с `scripts/verify.py` | 5 | 732,5 | 878,2 | 878,2 |
| Сумма mypy + pytest + clean-room по QA-логам | 34 | 438,6 | 895,4 | 979,6 |

Стадии `make verify` на `a4f485b`: mypy — 11,39 с, pytest — 337,59 с, clean-room — 250,22 с. По 34
QA-логам p50 pytest — 287,8 с, clean-room — 140,2 с, mypy — 8,4 с. Integration local QA с полным
прогоном выполнялся 2026-10-07 (470,6 с), 2026-10-08 (462,1 с), 2026-10-13 (732,5 с), 2026-10-14
(764,7 с) и 2026-10-16 (878,2 с). Шестой результат, от 2026-10-20 (17,8 с), запускал не полный
прогон и в строку не входит.

### Focused и failed-only

| Прогон | n | Тестов | p50, с | max, с |
| --- | --- | --- | --- | --- |
| Focused-набор ADR 0023 | 3 | 136 | 16,86 | 16,86 |
| Failed-only, 1 node id | 3 | 1 из 3726 | 1,51 | 1,52 |

### Команды coordinator

| Команда | n | p50, с | p95, с | p99, с |
| --- | --- | --- | --- | --- |
| `dispatch status` | 5 | 269,06 | 270,24 | 270,24 |
| `ledger validate` | 5 | 69,93 | 70,91 | 70,91 |
| `dispatch heartbeat` | 30 | 4,44 | 4,53 | 4,53 |
| `qa status` | 30 | 1,16 | 1,18 | 1,19 |
| `ledger status` | 30 | 0,67 | 0,69 | 0,70 |
| `batch list` | 30 | 0,66 | 0,69 | 0,69 |
| `ledger lock-status` | 30 | 0,14 | 0,17 | 0,18 |

### Стоимость проверки audit

| Проверка | Записей audit | n | p50, с | max, с |
| --- | --- | --- | --- | --- |
| `_validate_audit` | 5044 | 5 | 0,31 | 0,33 |
| `ledger validate` целиком | 5044 | 5 | 69,93 | 70,91 |

Пересчёт sha256 audit вместе с запуском интерпретатора занимает меньше 1 % времени
`ledger validate`. Остальное время уходит на другие проверки поколения.

## Оговорки

- Длительность batch включает ожидание человека и не равна машинному времени `/implement`.
  p99 и max — один batch длительностью 920,7 мин (15,3 ч) по часам.
- Число полных прогонов — оценка снизу. Прогон, не записанный ролью в `checks_run`, не учтён, а
  команда, которую нельзя разобрать (`shlex`), полным прогоном не считается.
- QA-логи и результаты local QA сняты на разных коммитах-кандидатах. Код и набор тестов между
  ними различаются, поэтому их разброс — не только шум измерения.
- `dispatch heartbeat` измерен на синтетически живом dispatch копии (см. «Методика»).
- Инструмент пишет `raw/` по шагам. Если он упадёт, часть файлов останется от незавершённого
  прогона; этот набор записан прогоном, который завершился с кодом 0.
