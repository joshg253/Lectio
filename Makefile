# Development helpers that use `uv` as the preferred runner.
UV_CACHE_DIR=.uvcache

.PHONY: lint types run test

lint:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff check .

fix:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff check --fix .

types:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ty check .

run:
	LECTIO_REFRESH_DEBUG=1 UV_CACHE_DIR=$(UV_CACHE_DIR) uv run uvicorn main:app --reload --host 0.0.0.0

test:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest -q
