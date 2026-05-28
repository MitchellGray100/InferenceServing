.PHONY: tests lint compile

tests:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest

lint:
	python -m ruff check app tests

compile:
	python -m compileall app tests
