# Migrating an existing single-user Lectio to multi-user

When you turn on `LECTIO_SECURITY_MODE=multi`, Lectio stores each user's data
under `DATA_DIR/users/<user_id>/`, where `user_id` is a **stable, opaque id**
generated when the account is created (so a username can be renamed later without
moving any data). Your existing single-user data lives in the top-level
`DATA_DIR/lectio_*.sqlite` files. This one-time migration copies that data into
your account's per-user directory.

What moves: `lectio_reader.sqlite` (+ its `.search` FTS sidecar),
`lectio_meta.sqlite3`, `lectio_starred_archive.sqlite`.
What stays put: the global thumb cache, the auth DB (`lectio_auth.sqlite`), and
`scraped-feeds/` (referenced by absolute `file://` URLs).

The migration **copies** (does not move) by default, so it is reversible — the
originals stay until you pass `--remove-source`.

## Steps

Run on the host with the real data directory (examples assume Docker's `/data`).

1. **Back up** (always):

   ```
   LECTIO_DATA_DIR=/data uv run scripts/backup_databases.py --dest /data/backups
   ```

2. **Turn on multi mode and start once** so bootstrap creates your account (and
   its stable `user_id`). In `.env`:

   ```
   LECTIO_SECURITY_MODE=multi
   LECTIO_ADMIN_USERNAME=joshg253
   LECTIO_ADMIN_PASSWORD=<a real password>
   LECTIO_PASSWORD_HASH_SCHEME=scrypt    # or argon2 / pbkdf2_sha256
   ```

   Start the app. Bootstrap creates the `joshg253` account with a fresh (empty)
   set of databases. **Don't use the account yet** — you're about to overwrite
   those empty DBs with your real data. Then **stop the app**.

3. **Dry run** — resolves your `user_id` from the auth DB, prints the plan,
   integrity-checks the sources, writes nothing:

   ```
   LECTIO_DATA_DIR=/data uv run scripts/migrate_to_multiuser.py --user joshg253
   ```

4. **Apply.** Because step 2 already created empty DBs in the destination, pass
   `--force` to overwrite them with your real data:

   ```
   LECTIO_DATA_DIR=/data uv run scripts/migrate_to_multiuser.py --user joshg253 --apply --force
   ```

   Each copied DB is integrity-checked.

5. **Start the app.** Log in as `joshg253`; your feeds, folders, read state, and
   starred archive are all there.

6. **Reconfigure RSS clients** (Capy, etc.): in multi mode each user authenticates
   with their **username + API token** from the `/account` page — not
   `LECTIO_FEVER_PASSWORD`.

7. Once you've confirmed everything is intact, reclaim the duplicated disk by
   re-running step 4 with `--remove-source` (instead of `--force`), or delete the
   top-level `lectio_reader.sqlite` / `lectio_meta.sqlite3` /
   `lectio_starred_archive.sqlite` by hand. Keep your step-1 backup regardless.

## Renaming later

Because the per-user directory is keyed by the immutable `user_id`, you can rename
a username any time (admin → `/account` → Rename a user) and **no data moves** —
login name changes, everything else stays put.

## Rolling back

The migration copies rather than moves, so rolling back is: stop the app, set
`LECTIO_SECURITY_MODE=single`, start. The original top-level DBs are untouched and
used again. (Delete `users/<user_id>/` to redo the migration cleanly.)
