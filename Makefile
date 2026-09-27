.PHONY: audit check lint test sync

sync:
	uv sync

lint:
	uv lock --check
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts
	uv run basedpyright

test:
	uv run pytest -q

check: lint test

audit:
	uv run --frozen python scripts/audit.py
