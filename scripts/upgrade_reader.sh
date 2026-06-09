#!/usr/bin/env bash
# Upgrade reader: back up DBs, update lock file, rebuild Docker.
#
# Usage:
#   1. Edit pyproject.toml to the desired reader version/ref.
#   2. Run this script from the project root:
#        ./scripts/upgrade_reader.sh
#
# The backup is taken *before* the new image starts, so you always have
# a copy of the DB at the old schema version if the migration goes wrong.
# Restore by stopping the container and replacing the live DB files in ./data/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$ROOT"

echo "==> Backing up databases..."
LECTIO_DATA_DIR="$ROOT/data" uv run scripts/backup_databases.py --keep 10
echo ""

echo "==> Updating uv.lock..."
uv lock
echo ""

echo "==> Building Docker image..."
docker compose build
echo ""

echo "==> Restarting container..."
docker compose up -d
echo ""

echo "Done. Backup is in ./data/backups/ — restore by stopping the container"
echo "and replacing the .sqlite files in ./data/ before starting again."
