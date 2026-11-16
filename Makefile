.PHONY: install test run dry-run eval status lint

install:
	python -m pip install -e .

test:
	python -m pytest -v

run:
	python -m agent run

dry-run:
	python -m agent run --dry-run

eval:
	python -m agent eval

status:
	python -m agent status
