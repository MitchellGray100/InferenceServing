.PHONY: tests compile

tests:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest

compile:
	python -m compileall app tests
