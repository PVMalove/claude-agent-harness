---
name: delivery-stats
description: "Collect delivery statistics for a finished epic and every ticket under it: tokens by model, cache efficiency, estimated cost, subscription window and code volume."
disable-model-invocation: true
---

# Delivery stats

**Objective:** After an epic and its child tickets are done, report what the delivery actually cost —
as a standalone HTML dashboard plus a short summary in this session.

Read-only. It reads local session transcripts, this repository's git history and the issue tracker,
writes one HTML file, and sends nothing anywhere.

## Route

The tool ships with the harness at `.harness/reporting/delivery_stats.py`. If it is absent, this
project did not install the capability that provides it — say so instead of reimplementing it here.

```bash
python .harness/reporting/delivery_stats.py --repo . --epic <номер> \
  --html docs/reports/epic-<номер>.html
```

Add `--json` when the developer wants the raw numbers rather than the summary. `--rates <file>`
overrides the rate card; `--base <ref>` overrides the diff base.

## Procedure

1. **Resolve the epic.** The developer names it. If they name a child ticket instead, run it on that
   ticket anyway — the tool treats a ticket with no sub-issues as a scope of one — and say that is
   what you did.
2. **Check the rate card before promising money.** Cost appears only when
   `.harness/reporting/rates.json` exists and prices the models that were used. There is no built-in
   price list, by design: prices depend on the plan and change over time. If it is missing, copy
   `.harness/reporting/rates.example.json`, tell the developer to fill in their own rates, and report
   everything else meanwhile — do not invent a number, and do not quote a price you did not read from
   that file.
3. **Run the tool** and write the dashboard under `docs/reports/`.
4. **Report the summary** in this session: tickets closed, code volume, tokens by model, cache split,
   cost if priced, and the path to the HTML.
5. **Carry the caveats through, do not smooth them over.** They are the point of the report:
    - Claude Code work is attributed **exactly** — every transcript record carries its branch.
    - Codex work is attributed **approximately** — its logs carry no branch, only a working directory
      and a timestamp, so its totals cover this repository inside the epic's activity window and can
      include unrelated work from the same period. Always say "оценка" when quoting it.
    - Anything the tool could not source reads `нет данных`. Repeat it as missing; never round it to
      zero and never fill it from memory.
    - An ADR counts as added only when the commit that first added the file belongs to one of the
      epic's pull requests, so a squash-merged pull request undercounts. Mention it only if the
      developer asks why a number looks low.
6. **Offer to attach it.** The dashboard is a local file; posting it to the epic is the developer's
   call, not an automatic step.

## Boundaries

Do not edit the transcripts, the rate card or the git history to make a number look better. Do not
add prices, models or tickets the tool did not report. If the tool exits non-zero, show its stderr
and stop — a partial statistic presented as complete is worse than none.
\n
<!--
Краткое описание (Summary): Этот навык предназначен для сбора статистики о доставке завершенных эпиков и тикетов, включая использование токенов, эффективность кэширования, оценку стоимости и объем кода. Отчет выводится в виде HTML-дашборда.

Перевод:
---
name: delivery-stats
description: "Сбор статистики доставки для завершенного эпика и каждого тикета в нем: токены по моделям, эффективность кэша, расчетная стоимость, окно подписки и объем кода."
disable-model-invocation: true
---

# Статистика доставки

**Цель:** После завершения эпика и его дочерних тикетов, сообщить фактическую стоимость доставки — в виде отдельного HTML-дашборда плюс краткого резюме в этой сессии.

Только для чтения. Он читает локальные транскрипты сессий, историю git этого репозитория и трекер задач, записывает один HTML-файл и ничего никуда не отправляет.

## Маршрут

Этот инструмент поставляется вместе с харнессом в `.harness/reporting/delivery_stats.py`. Если он отсутствует, значит, в этом проекте не установлена возможность, которая его предоставляет — скажите об этом вместо того, чтобы переопределять его здесь.

```bash
python .harness/reporting/delivery_stats.py --repo . --epic <номер> \
  --html docs/reports/epic-<номер>.html
```

Добавьте `--json`, когда разработчику нужны сырые числа, а не сводка. `--rates <file>` переопределяет тарифную сетку; `--base <ref>` переопределяет базу для diff.

## Процедура

1. **Разрешить эпик.** Разработчик называет его. Если вместо этого он называет дочерний тикет, запустите его на этом тикете в любом случае — инструмент рассматривает тикет без подзадач как область из одной задачи — и скажите, что вы это сделали.
2. **Проверьте тарифную сетку, прежде чем обещать деньги.** Стоимость появляется только тогда, когда существует файл `.harness/reporting/rates.json` и в нем указаны цены на использованные модели. Встроенного прайс-листа нет, это сделано намеренно: цены зависят от тарифного плана и со временем меняются. Если он отсутствует, скопируйте `.harness/reporting/rates.example.json`, скажите разработчику заполнить свои собственные тарифы и сообщите обо всем остальном тем временем — не придумывайте цифры и не указывайте цену, которую вы не прочитали из этого файла.
3. **Запустите инструмент** и запишите дашборд в `docs/reports/`.
4. **Сообщите сводку** в этой сессии: закрытые тикеты, объем кода, токены по моделям, разделение кэша, стоимость, если она оценена, и путь к HTML.
5. **Пронесите оговорки через весь текст, не сглаживайте их.** В них заключается смысл отчета:
    - Работа Claude Code атрибутируется **точно** — каждая запись транскрипта несет свою ветку.
    - Работа Codex атрибутируется **приблизительно** — его логи не содержат ветки, только рабочую директорию и временную метку, поэтому его итоги охватывают этот репозиторий в окне активности эпика и могут включать несвязанную работу из того же периода. Всегда говорите "оценка", когда цитируете это.
    - Все, что инструмент не смог найти, читается как `нет данных`. Повторите это как отсутствующее; никогда не округляйте до нуля и никогда не заполняйте по памяти.
    - ADR считается добавленным только тогда, когда коммит, впервые добавивший файл, принадлежит одному из pull-запросов эпика, поэтому pull-запрос со squash-merge занижает показатель. Упоминайте об этом только если разработчик спросит, почему число выглядит заниженным.
6. **Предложите прикрепить это.** Дашборд — это локальный файл; его публикация в эпике — решение разработчика, а не автоматический шаг.

## Границы

Не редактируйте транскрипты, тарифную сетку или историю git, чтобы цифры выглядели лучше. Не добавляйте цены, модели или тикеты, о которых инструмент не сообщил. Если инструмент завершается с ненулевым кодом, покажите его stderr и остановитесь — частичная статистика, представленная как полная, хуже, чем ее отсутствие.
-->
\n