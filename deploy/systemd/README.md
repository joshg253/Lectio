# Backup timer

Runs `scripts/backup_databases.py` then `scripts/ship_backups_to_b2.py` daily, moving
backups off-host to Backblaze B2 and clearing them locally on success (see the docstring
in `ship_backups_to_b2.py` for why no local generations are kept).

## Install

Requires `rclone` on the host (`sudo apt install rclone`, or the [official install
script](https://rclone.org/install.sh) for a newer version than Ubuntu's repo carries)
and `LECTIO_B2_BUCKET` / `LECTIO_B2_KEY_ID` / `LECTIO_B2_APPLICATION_KEY` set in
`/opt/lectio/.env` (see `.env.example`).

```
sudo cp deploy/systemd/lectio-backup.service deploy/systemd/lectio-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lectio-backup.timer
```

## Verify

```
sudo systemctl start lectio-backup.service   # run once, immediately
journalctl -u lectio-backup.service -f       # watch it
systemctl list-timers lectio-backup.timer    # confirm next scheduled run
```

`Persistent=true` on the timer means a run missed while the VPS was down fires once at
next boot instead of silently skipping — this is what let the previous unscheduled setup
go quiet for weeks without anyone noticing.

The `User=ubuntu` in the service file assumes the standard deploy user; change it if a
different account owns `/opt/lectio`.
