# HTML-макеты консоли

Принятые макеты Harness console — дизайн-источник эпика #720. Исходные страницы приватны, поэтому
здесь лежат их локальные копии. Файлы сохранены без изменений. Данные и действия в макетах
условные: они не запускают команды и не показывают состояние реального проекта. Пути вида
`/home/…/claude-agent-harness` в примерах — демонстрационные значения.

Большинство страниц подключает общий [console.css](console.css); `home.html` сохраняет
встроенные исторические стили. Поэтому файлы лежат в одном каталоге.

Принятые 27 октября 2026 уточнения — прозрачные блоки и компактная компоновка варианта A —
описаны в [контракте дизайна #720](design-contract-720.md). Они имеют приоритет над
заливками и большими отступами исходных макетов.

| Экран | Макет |
| --- | --- |
| Главное окно консоли | [home.html](home.html) |
| Открыть проект | [open-project.html](open-project.html) |
| Диагностика | [diagnostics.html](diagnostics.html) |
| Harness | [harness.html](harness.html) |
| Отчёты | [reports.html](reports.html) |
| Repo Map | [repo-map.html](repo-map.html) |
| Настройка | [configuration.html](configuration.html) |
| Обновить Harness | [update-harness.html](update-harness.html) |
| Оркестрация | [orchestration.html](orchestration.html) |
| Восстановление | [recovery.html](recovery.html) |
| Контейнерный запуск | [container-route.html](container-route.html) |
| Справка | [help.html](help.html) |

Открывать файлы нужно локально в браузере; GitHub показывает их как исходный текст, например
`xdg-open docs/design/console-mockups/home.html`.

Наличие макета не означает, что функция уже реализована в консоли. Реализация идёт тикетами
эпика #720.
