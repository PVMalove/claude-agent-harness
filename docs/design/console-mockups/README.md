# HTML-макеты консоли

Принятые макеты Harness console — дизайн-источник эпика #720. Исходные страницы приватны, поэтому
здесь лежат их локальные копии. Файлы сохранены без изменений. Данные и действия в макетах
условные: они не запускают worker и не показывают состояние реального проекта.

| Макет | Файл | Происхождение |
| --- | --- | --- |
| Общая консоль и наблюдение за worker, три компоновки через `?variant=console\|workspace\|focus` | [issue-716-harness-console-prototype.html](issue-716-harness-console-prototype.html) | #716, #711 |
| Раздел консоли для восстановления, полная страница с локальными CSS и JS | [issue-684/site/index.html](issue-684/site/index.html#console) | #684, #681 |
| HTML-фрагмент раздела консоли | [issue-684/console-section.html](issue-684/console-section.html) | #684 |
| Ранняя версия страницы | [issue-684/earlier-site/index.html](issue-684/earlier-site/index.html) | #684 |

Открывать файлы нужно локально в браузере; GitHub показывает их как исходный текст. Ссылки на
`issue-716-guide-worker-startup-recovery.md` и `sandbox-and-observability-findings.md` внутри HTML
указывают на материалы задач, которые не отслеживаются Git, поэтому не открываются.

Наличие макета не означает, что функция уже реализована в консоли. Реализация идёт тикетами
эпика #720.
