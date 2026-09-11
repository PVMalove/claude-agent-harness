# Диаграммы харнесса

Пять автономных интерактивных HTML-диаграмм. Рядом с каждой лежит редактируемая спецификация
Archify (`*.json`), а в `previews/` — статичное PNG той же диаграммы для Markdown, который не умеет
рендерить HTML (например, README на GitHub).

| Диаграмма | О чём |
|---|---|
| [Пайплайн доставки](./delivery-pipeline.workflow.html) | Полный маршрут от идеи до merge: `/grill-with-docs` → `/to-spec` → `/to-tickets` → `/implement` → `/to-pull-requests`, с ветками `hitl` (`/to-guide`) и коротким `/fast-implement`. |
| [Конвейер `/implement`](./implement-pipeline.workflow.html) | Пять гейтов одного тикета: архитектор → approve → разработчик → code review → approve → QA (с циклом на исправления) → итоговый отчёт → публикация. |
| [Резолв runtime и dispatch](./backend-runtime.workflow.html) | Как назначение роли превращается в immutable brief, как выбирается транспорт (`orca` или `in-process`) и как dispatch подтверждает свою модель и живость. |
| [Жизненный цикл batch](./backend-batch.lifecycle.html) | Состояния batch: `planned → awaiting-approval ↔ active → completed`, плюс выходы `blocked` и `failed`. |
| [QA и создание PR](./qa-call-path.workflow.html) | Где `test_summary.py` вызывается в `/qa-gate`, какие QA-маршруты обходят обёртку и как явное подтверждение приводит к `gh`/`glab pr create`. |

## Как обновлять

Диаграммы собраны скиллом [archify](https://github.com/tt-a1i/archify). Правится только `*.json`,
после чего диаграмма перегенерируется и проверяется:

```bash
node bin/archify.mjs validate <type> <spec>.json --quality showcase --json
node bin/archify.mjs deliver  <type> <spec>.json <output>.html --quality showcase --json
node bin/archify.mjs visual-check <output>.html --json
```

`<type>` — `workflow` для трёх первых спецификаций и `lifecycle` для четвёртой. `deliver` обязан
завершиться нулевым кодом, `visual-check` — дать `containment: pass`. PNG в `previews/` — это светлый
снимок `visual-check` при 1440×900; после перегенерации HTML его нужно обновить, иначе README покажет
устаревшую картинку.

Содержимое диаграмм ведётся на русском. Интерфейс самого просмотрщика (`Light`/`Dark`, `Present`,
`Export`, `Legend`) и подписи легенды в lifecycle остаются английскими: это фиксированный UI
рендерера, он не переводится и на семантику диаграммы не влияет.
