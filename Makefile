.DEFAULT_GOAL := help

# На Windows заставляем make всегда идти через cmd.exe
ifeq ($(OS),Windows_NT)
SHELL := cmd.exe
.SHELLFLAGS := /C
endif

HARNESS_VENV := .harness/.venv
HARNESS_ENV_STAMP := $(HARNESS_VENV)/.requirements-installed

ifeq ($(OS),Windows_NT)
PYTHON_BOOTSTRAP ?= python
HARNESS_PYTHON := $(HARNESS_VENV)\Scripts\python.exe
else
PYTHON_BOOTSTRAP ?= python3
HARNESS_PYTHON := $(HARNESS_VENV)/bin/python
endif

.PHONY: help bootstrap verify test coverage typecheck clean registry test-clean-room format lint

help: ## Показать список команд с описанием
	@$(PYTHON_BOOTSTRAP) -c "import re, sys; print('Доступные команды:'); lines = open(sys.argv[1], encoding='utf-8').readlines(); matches = [re.match(r'^([a-zA-Z0-9_-]+):.*?## (.*)$$', line) for line in lines]; [print(f'  {m.group(1):<16} - {m.group(2)}') for m in matches if m]" $(MAKEFILE_LIST)

$(HARNESS_ENV_STAMP): requirements-dev.txt
	$(PYTHON_BOOTSTRAP) -m venv $(HARNESS_VENV)
	$(HARNESS_PYTHON) -m pip install --disable-pip-version-check -r requirements-dev.txt
	$(HARNESS_PYTHON) -c "from pathlib import Path; Path(r'$(HARNESS_ENV_STAMP)').touch()"

bootstrap: $(HARNESS_ENV_STAMP) ## Создать .harness/.venv и установить Python-зависимости

format: $(HARNESS_ENV_STAMP) ## Автоформатирование кода (ruff format + ruff check --fix-only)
	$(HARNESS_PYTHON) -m ruff format .
	$(HARNESS_PYTHON) -m ruff check --fix-only .

lint: typecheck $(HARNESS_ENV_STAMP) ## Линтинг кода (ruff check + mypy)
	$(HARNESS_PYTHON) -m ruff check .

verify: $(HARNESS_ENV_STAMP) ## Запустить полный набор проверок проекта (sanity checks, mypy, tests, clean-room)
	$(HARNESS_PYTHON) scripts/verify.py

test: $(HARNESS_ENV_STAMP) ## Запустить только unit-тесты (pytest)
	set PYTHONPATH=. && $(HARNESS_PYTHON) -m pytest -n 4 tests

coverage: $(HARNESS_ENV_STAMP) ## Запустить тесты с проверкой покрытия (pytest-cov)
	set PYTHONPATH=. && $(HARNESS_PYTHON) -m pytest --cov=harness --cov-report=term --cov-report=html tests

typecheck: $(HARNESS_ENV_STAMP) ## Запустить mypy (проверка типов)
	$(HARNESS_PYTHON) -m mypy

registry: $(HARNESS_ENV_STAMP) ## Пересобрать skills/REGISTRY.md (после изменения/добавления скиллов)
	$(HARNESS_PYTHON) scripts/build-registry.py

test-clean-room: $(HARNESS_ENV_STAMP) ## Запустить clean-room тесты
	$(HARNESS_PYTHON) scripts/test-clean-room.py

clean: ## Удалить временные файлы, стейт оркестратора, кэши и .pyc
	python -c "import shutil, os, glob; [shutil.rmtree(p, ignore_errors=True) for p in ['.mypy_cache', '.pytest_cache', '.harness/orchestration/state', '.harness/scratch', '.claude/worktrees', 'htmlcov'] if os.path.exists(p)]; [os.remove(f) for f in glob.glob('**/*.pyc', recursive=True)]"
