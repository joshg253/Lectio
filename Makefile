# Development helpers that use `uv` as the preferred runner.
UV_CACHE_DIR=.uvcache

.PHONY: lint types run test audit

lint:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff check .

fix:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff check --fix .

types:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ty check .

run:
	LECTIO_REFRESH_DEBUG=1 LECTIO_DEBUG=1 UV_CACHE_DIR=$(UV_CACHE_DIR) uv run uvicorn main:app --reload --reload-exclude .venv --host 0.0.0.0 --port 8000

test:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest -q

# OSV-backed dependency scan. Mirrors the CI step; preview feature, so kept
# separate from `test` (a preview-tool change shouldn't break local test runs).
audit:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv audit --preview-features audit --frozen
