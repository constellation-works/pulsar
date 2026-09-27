.PHONY: audit check lint plugin test sync

plugin:
	python3 scripts/build_plugin.py

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
