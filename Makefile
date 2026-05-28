PYTHON ?= python
POETRY ?= $(PYTHON) -m poetry

.PHONY: install tests lint compile clean

install:
	$(PYTHON) -m pip install --upgrade pip poetry
	$(POETRY) install

tests:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(POETRY) run pytest

lint:
	$(POETRY) run ruff check app tests

compile:
	$(POETRY) run python -m compileall app tests

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
