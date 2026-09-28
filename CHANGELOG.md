# Changelog

Изменения выпусков записываются в формате [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/) и версионируются по SemVer.

## [1.0.0] - 2026-09-28

### Added

- Первый стабильный выпуск переносимого Agent Harness с capability-снимками, CLI установки, оркестрацией и Repo Map.
- GitHub Release с архивом установки, SHA-256 и заметками из этого раздела.
- Управляемый пример `.harness/orchestration.example.json` для `backend-orchestration`: обновляется при `init`, `adopt` и `update`; `init` один раз создаёт из него `.harness/orchestration.json`.
- Справочник оркестрации `harness/orchestration/README.md`: устройство coordinator и все поля `orchestration.json` с дефолтами.
- README: каталог всех скилов с описаниями на русском, инструкции `harness health` и `harness console`.
- Русские описания проектных агентов в `docs/agents/` с полным переводом манифестов и `docs/agents/README.md`.

### Changed

- Установщик глобального слоя переименован в `bin/install-global.py`: запуск `python3 bin/install-global.py ...`.
