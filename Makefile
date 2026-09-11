.PHONY: check lint test sync

sync:
	uv sync

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

test:
	uv run pytest -q

check: lint test
