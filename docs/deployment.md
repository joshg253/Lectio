# Deployment & Operations

## Running locally

Use `uv` to run the app and scripts.

## Deployment

A `Dockerfile` and `docker-compose.yml` are included for deployment behind a TLS-terminating reverse proxy (e.g. Traefik on an existing `proxy` network).

1. `cp .env.example .env` and fill in `BASE_URL`, `TZ`, `LECTIO_USERNAME`, `LECTIO_PASSWORD`, `LECTIO_SECRET_KEY`.
2. `mkdir -p data && sudo chown -R 1000:1000 data` (the container runs as uid 1000).
3. `docker compose up -d --build`.

The compose file sets `LECTIO_HTTPS_ONLY=1` and routes `lectio.${BASE_URL}` through Traefik with HSTS/frameDeny/compress middleware.

## Backups

`scripts/backup_databases.py` uses SQLite `VACUUM INTO` for online-safe snapshots and honors `LECTIO_DATA_DIR`, so the same script works locally and in the container.

Schedule it on the VPS host by dropping the following into `/etc/cron.d/lectio-backup`:

```cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
17 3 * * * root docker exec lectio uv run scripts/backup_databases.py --keep 14 >> /opt/lectio/data/logs/backup.log 2>&1
```

Daily at 03:17, keeps 14 days, lands in `/opt/lectio/data/backups/` via the bind mount. Restoring: stop the app, replace the three `lectio_*.sqlite*` files in the data dir with the backup copies (renamed back to their original filenames), restart.

## Notes

- Saved/starred content may be archived for durability.
- Some debug features are intended for development only.
