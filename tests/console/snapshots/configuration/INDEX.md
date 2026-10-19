# Снимки #724 (Pilot SVG)

Каждое состояние снято на 160x52 и 80x24: файл `<состояние>-160x52.svg` и `<состояние>-80x24.svg`.
Снимки не коммитятся (каталог в .gitignore) и прикладываются к PR.

| Состояние | Макет | Раздел макета |
| --- | --- | --- |
| `configuration-start` | `docs/design/console-mockups/configuration.html` | 8 · Настройка — стартовая страница (160×52) / 8 · Настройка — узкий терминал (80×24) |
| `configuration-modal-confirmable` | `docs/design/console-mockups/configuration.html` | Просмотр установки и обновления — окно «Можно подтверждать» |
| `configuration-modal-blocked-capability` | `docs/design/console-mockups/configuration.html` | Просмотр установки и обновления — окно «Нельзя подтвердить: capability не выбрана» |
| `configuration-adapters-both-current` | `docs/design/console-mockups/configuration.html` | Состояния блока «Адаптеры runtime» — Оба актуальны |
| `configuration-adapter-missing` | `docs/design/console-mockups/configuration.html` | Состояния блока «Адаптеры runtime» — Адаптер не установлен |
| `configuration-adapter-local-edits` | `docs/design/console-mockups/configuration.html` | Состояния блока «Адаптеры runtime» — Есть локальные правки |
| `configuration-capability-not-selected` | `docs/design/console-mockups/configuration.html` | Состояния блока «Адаптеры runtime» — backend-orchestration не выбрана |
| `configuration-harness-not-installed` | `docs/design/console-mockups/configuration.html` | Состояния блока «Адаптеры runtime» — Harness не установлен |
| `configuration-plan-stale` | `docs/design/console-mockups/configuration.html` | Состояния блока «Адаптеры runtime» — План устарел |
| `configuration-update-done` | `docs/design/console-mockups/configuration.html` | Состояния блока «Адаптеры runtime» — Обновление прошло |
| `update-main` | `docs/design/console-mockups/update-harness.html` | 2 · Обновить Harness — широкий терминал (160×52) / узкий терминал (80×24), верх страницы |
| `update-main-scrolled` | `docs/design/console-mockups/update-harness.html` | 2 · Обновить Harness — те же размеры, низ страницы со всеми блоками раскрытыми (Diff, Результат) |
| `update-seed-ticked` | `docs/design/console-mockups/update-harness.html` | Режим «С перезаписью выбранных seed-файлов» — файлы отмечены |
| `update-seed-none` | `docs/design/console-mockups/update-harness.html` | Режим «С перезаписью выбранных seed-файлов» — ничего не отмечено, «Обновить» выключена |
| `update-drift-loading` | `docs/design/console-mockups/update-harness.html` | Состояния экрана — Дрейф проверяется |
| `update-not-installed` | `docs/design/console-mockups/update-harness.html` | Состояния экрана — Harness не установлен |
| `update-clean` | `docs/design/console-mockups/update-harness.html` | Состояния экрана — Расхождений нет |
| `update-drift-failed` | `docs/design/console-mockups/update-harness.html` | Состояния экрана — Дрейф не проверен |
| `update-running` | `docs/design/console-mockups/update-harness.html` | Состояния экрана — Идёт обновление |
| `update-done` | `docs/design/console-mockups/update-harness.html` | Состояния экрана — Обновление прошло |
| `update-failed` | `docs/design/console-mockups/update-harness.html` | Состояния экрана — Обновление не прошло |

Снимки пересобраны на коммите a983053bfda64c689fcfd019b119a0aa204aec9e (после fix: крошка Главная спрашивает подтверждение один раз).
