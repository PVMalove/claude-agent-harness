# harness/ становится обычным импортируемым Python-пакетом, без pip-установки

Кросс-папочные импорты в `harness/*.py` держатся на ручных `sys.path.insert` (в `coordinator.py`,
`orca_adapter.py`, `harness/bin/harness`) — ни `harness/`, ни его домен-подпапки
(`context_builder/`, `gate_runner/`, `orchestration/`, `reporting/`) не имеют `__init__.py`. Это
мешает mypy резолвить модули между папками и держит граф импортов неявным.

Решили: добавить `__init__.py` в `harness/` и четыре домен-подпапки, переписать кросс-папочные
импорты на относительные — но не добавлять `[build-system]` и не делать пакет pip-устанавливаемым.
`harness/` несёт не только Python-код, а ещё packager CLI, capability catalog и target-project
templates (`.md`, `.schema.json`, `roles/`, `state/` лежат в тех же папках, что и `.py`) — упаковка
этого в дистрибутируемый wheel не имеет смысла, а готовой pip-инфраструктуры в репозитории
изначально нет. Точки входа (`harness/bin/harness`, `scripts/verify`, тесты) выставляют
`PYTHONPATH` на корень репозитория вместо `pip install -e .`; mypy резолвит пакет через
`explicit_package_bases`/`mypy_path`, а не через установку.
