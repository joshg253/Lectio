# Development

Day-to-day commands (`make test`, `make rebuild`, the lint hook) are in the README. The rest lives here.

- **Rebuilding the container** — `make rebuild` prunes the BuildKit cache to a ceiling (`BUILD_CACHE_MAX`, 2GB), builds, and restarts.
  Capping the cache evicts only the old tail, so recent layers still make builds fast. The target refuses to run while a `docker compose
  exec` is in flight, because recreating the container kills a session mid-write.
- **Scratch cleanup** — `make test` runs `make clear-scratch` first, and the local-verification workflow does the same before launching a
  dev server. On a host where `/tmp` is a small RAM-backed tmpfs, each full suite run leaves its per-test SQLite scratch behind; once
  `/tmp` fills, pytest reports mass failures that look like real regressions rather than an out-of-space error.
  `scripts/clear_dev_scratch.py` clears only known-disposable paths (never a bare `/tmp/*` sweep) and skips both the running session and
  anything under two days old, so a concurrent session is safe. `--dry-run` shows what would go.
- **CI** — GitHub Actions runs the suite on Python 3.15 for every pull request and push to `main`
  ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)). Dependencies install from the locked `uv.lock` (`uv sync --frozen`), and
  any `DeprecationWarning` is an error.
- **Dependency audit** — `uv audit` (OSV-backed) scans the locked dependencies for known vulnerabilities and deprecated packages. Run it
  with `make audit`; CI runs the same scan. It's a uv preview feature, so it's separate from `make test` and the CI step is informational
  (non-blocking).
- **CodeQL, locally** — `make codeql-install` fetches the CodeQL CLI (pinned to CI's version) into gitignored `.tools/`. Don't run it
  while iterating: `make codeql-db && make codeql-fast` (~10 min; our custom queries plus the stock stack-trace rule) once, just before
  pushing a change that touches redirects or error handling. CI runs the full suite on every PR, so the full local `make codeql` (~30 min)
  is only for changes to `.github/codeql/`.
- **Backups** — `scripts/backup_databases.py` takes a consistent `VACUUM INTO` snapshot of every database; `scripts/ship_backups_to_b2.py`
  ships it to a Backblaze B2 bucket via `rclone move` and clears it locally on success. `deploy/systemd/` has the daily timer and install
  steps; see its README.
