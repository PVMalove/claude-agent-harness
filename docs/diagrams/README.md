# Диаграммы backend-оркестрации

- [Lifecycle backend-batch](./backend-batch.lifecycle.html) — планирование, явное approval,
  выполнение, evidence и выходы для block/failure.
- [Выбор runtime и dispatch](./backend-runtime.workflow.html) — как coordinator утверждает
  dispatch, конфигурация выбирает Codex или Claude, а adapter передаёт immutable brief worker-у.

Каждая HTML-диаграмма автономна и интерактивна. Файлы `*.json` рядом с ней — редактируемые
спецификации Archify; после изменения их нужно снова валидировать и сгенерировать HTML.
