.PHONY: check lint test sync standards-check

sync:
	uv sync

lint:
	uv lock --check
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run basedpyright

test:
	uv run pytest -q

standards-check:
	sh docs/standards/check.sh

check: standards-check lint test
