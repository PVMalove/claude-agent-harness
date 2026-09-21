.DEFAULT_GOAL := help

# На Windows заставляем make всегда идти через cmd.exe
ifeq ($(OS),Windows_NT)
SHELL := cmd.exe
.SHELLFLAGS := /C
endif

.PHONY: help verify test coverage typecheck clean registry test-clean-room format lint

help: ## Показать список команд с описанием
	@python -c "import re, sys; print('Доступные команды:'); lines = open(sys.argv[1], encoding='utf-8').readlines(); matches = [re.match(r'^([a-zA-Z0-9_-]+):.*?## (.*)$$', line) for line in lines]; [print(f'  {m.group(1):<16} - {m.group(2)}') for m in matches if m]" $(MAKEFILE_LIST)

format: ## Автоформатирование кода (ruff format + ruff check --fix-only)
	python -m ruff format .
	python -m ruff check --fix-only .

lint: typecheck ## Линтинг кода (ruff check + mypy)
	python -m ruff check .

verify: ## Запустить полный набор проверок проекта (sanity checks, mypy, tests, clean-room)
	python scripts/verify.py

test: ## Запустить только unit-тесты (pytest)
	set PYTHONPATH=. && python -m pytest -n 4 tests

coverage: ## Запустить тесты с проверкой покрытия (pytest-cov)
	set PYTHONPATH=. && pytest --cov=harness --cov-report=term --cov-report=html tests

typecheck: ## Запустить mypy (проверка типов)
	python -m mypy

registry: ## Пересобрать skills/REGISTRY.md (после изменения/добавления скиллов)
	python scripts/build-registry.py

test-clean-room: ## Запустить clean-room тесты
	python scripts/test-clean-room.py

clean: ## Удалить временные файлы, стейт оркестратора, кэши и .pyc
	python -c "import shutil, os, glob; [shutil.rmtree(p, ignore_errors=True) for p in ['.mypy_cache', '.pytest_cache', '.harness/orchestration/state', '.harness/scratch', '.claude/worktrees', 'htmlcov'] if os.path.exists(p)]; [os.remove(f) for f in glob.glob('**/*.pyc', recursive=True)]"
