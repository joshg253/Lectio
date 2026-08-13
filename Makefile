# Development helpers that use `uv` as the preferred runner.
UV_CACHE_DIR=.uvcache

# Ceiling for the BuildKit cache. Rebuilding after every commit grew it to 7.9GB
# across 57 entries on a 72GB disk; a cap evicts only the old tail, so recent
# layers still make rebuilds fast.
BUILD_CACHE_MAX=2GB

.PHONY: lint types run test audit screenshots clear-scratch rebuild

lint:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff check .

fix:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ruff check --fix .

types:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run ty check .

run:
	LECTIO_REFRESH_DEBUG=1 LECTIO_DEBUG=1 UV_CACHE_DIR=$(UV_CACHE_DIR) uv run uvicorn main:app --reload --reload-exclude .venv --host 0.0.0.0 --port 8000

# Clears first because /tmp is a 3.8G tmpfs and each full run leaves its
# per-test SQLite scratch behind. Once it fills, pytest reports mass failures
# that look like real regressions rather than an out-of-space error — so the
# cleanup is part of the run, not a thing to remember afterwards.
test: clear-scratch
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run pytest -q

clear-scratch:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run scripts/clear_dev_scratch.py --quiet

# Rebuild the image and restart the container — the step that follows a commit,
# since the image bakes the source in.
#
# The prune runs BEFORE the build, deliberately: pruning afterwards would evict
# the layers the build just produced, which are the ones the next build wants.
#
# The exec guard is a real lesson, not caution: a `docker compose exec` doing DB
# work is killed by the container being recreated underneath it, and the write
# it was in the middle of goes with it.
rebuild:
	@# Two self-match traps here, both of which made this guard fire on every
	@# run until fixed: the recipe's shell has the whole line as its command
	@# line, so BOTH the pgrep pattern and the message below would match
	@# themselves. Hence the bracket in the pattern, and the quote breaking up
	@# the phrase in the message. Don't "tidy" either one away.
	@if pgrep -f "docker[ ]compose exec" >/dev/null; then \
		echo "make rebuild: a 'docker compose' exec session is running — wait for it or kill it first"; \
		exit 1; \
	fi
	docker builder prune --max-used-space $(BUILD_CACHE_MAX) -f
	docker compose build
	docker compose up -d

# OSV-backed dependency scan. Mirrors the CI step; preview feature, so kept
# separate from `test` (a preview-tool change shouldn't break local test runs).
audit:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv audit --preview-features audit --frozen

# Regenerate docs/screenshots from synthetic demo data (no live feeds). Needs the
# screenshots extra: `uv sync --extra screenshots && uv run playwright install chromium`.
screenshots:
	UV_CACHE_DIR=$(UV_CACHE_DIR) uv run python scripts/refresh_screenshots.py
