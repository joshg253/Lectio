"""Regenerate the README/docs screenshots from synthetic demo data.

    uv sync --extra screenshots && uv run playwright install chromium
    uv run python scripts/refresh_screenshots.py

The whole run is hermetic and privacy-safe: it spins up a throwaway Lectio
instance over a temp data directory seeded with fully-synthetic demo feeds (no
network fetch of any real feed), captures the shots with Playwright, and writes
them into ``docs/screenshots/``. Nothing private can leak into a committed image
because no real feed is ever loaded.

Run with ``--keep-data`` to leave the temp instance in place for debugging.
"""
from __future__ import annotations

import argparse
import contextlib
import http.server
import os
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.screenshots import demo  # noqa: E402

# Stable default port for the demo feed server so the feed URL shown in the Feed
# Properties screenshot is the same every run (falls back to a free port if busy).
_DEMO_FEED_PORT = 8765


def _free_port(preferred: int | None = None) -> int:
    if preferred is not None:
        with contextlib.suppress(OSError):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", preferred))
                return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_demo_feeds(feeds_dir: Path) -> None:
    feeds_dir.mkdir(parents=True, exist_ok=True)
    for _folder, _title, slug, rss in demo.feeds():
        (feeds_dir / f"{slug}.xml").write_text(rss, encoding="utf-8")


@contextlib.contextmanager
def _static_server(directory: Path, port: int):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(directory), **k
    )
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        httpd.shutdown()


def _wait_healthy(url: str, proc: subprocess.Popen, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early (code {proc.returncode})")
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    raise TimeoutError(f"server did not become healthy at {url}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    ap.add_argument("--keep-data", action="store_true",
                    help="leave the temp demo instance on disk for debugging")
    args = ap.parse_args()

    data_dir = Path(tempfile.mkdtemp(prefix="lectio-shots-"))
    admin_data_dir = Path(tempfile.mkdtemp(prefix="lectio-shots-admin-"))
    feeds_dir = data_dir / "feeds"
    feed_port = _free_port(_DEMO_FEED_PORT)
    app_port = _free_port()
    base_app_url = f"http://127.0.0.1:{app_port}"

    procs: list[subprocess.Popen] = []

    def _stop(proc: subprocess.Popen) -> None:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)

    try:
        print(f"Demo data dir: {data_dir}")
        _write_demo_feeds(feeds_dir)

        env = dict(os.environ)
        env.update({
            "LECTIO_DATA_DIR": str(data_dir),
            "PYTHONPATH": str(ROOT),
            # Force a clean, unauthenticated single-user demo regardless of any
            # auth configured in the developer's .env (which is auto-loaded). The
            # explicit values win because .env only fills keys not already set.
            "LECTIO_SECURITY_MODE": "single",
            "LECTIO_USERNAME": "",
            "LECTIO_PASSWORD": "",
            # Blank out any real instance config from the developer's .env so it
            # can never land in a committed screenshot.
            "RESEND_API_KEY": "",
            "LECTIO_EMAIL_FROM": "lectio@demo.example",
            "LECTIO_EMAIL_TO": "you@demo.example",
            "YOUTUBE_API_KEY": "",
            "YOUTUBE_CHANNEL_ID": "",
        })

        # Seed the library while the demo feeds are being served locally.
        # LECTIO_DEBUG=1 only while seeding: it disables the SSRF guard so the
        # localhost demo feed server is reachable. The serve phase runs WITHOUT
        # debug, since debug mode also auto-subscribes (failing) dev feeds.
        with _static_server(feeds_dir, feed_port):
            seed_env = dict(env, LECTIO_DEBUG="1",
                            DEMO_BASE_URL=f"http://127.0.0.1:{feed_port}")
            print("Seeding demo library…")
            subprocess.run(
                [sys.executable, "-m", "scripts.screenshots.seed"],
                env=seed_env, cwd=str(ROOT), check=True,
            )

        # Serve the seeded instance (no background refresh — feeds are gone now).
        serve_env = dict(env, LECTIO_DEBUG="0", LECTIO_DISABLE_STARTUP_BACKFILL="1")
        print(f"Starting Lectio on {base_app_url}…")
        server_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(app_port), "--log-level", "warning"],
            env=serve_env, cwd=str(ROOT),
        )
        procs.append(server_proc)
        _wait_healthy(base_app_url + "/", server_proc)

        print(f"Capturing screenshots → {args.out}")
        from scripts.screenshots import capture  # deferred: needs Playwright
        capture.capture(base_app_url, args.out)
        _stop(server_proc)

        # Second pass: the multi-user Administration page (needs `multi` mode, a
        # fresh data dir, and a logged-in admin session).
        admin_user, admin_pw = "demoadmin", "demo-admin-pw"
        admin_port = _free_port()
        admin_url = f"http://127.0.0.1:{admin_port}"
        admin_env = dict(os.environ, **{
            "LECTIO_DATA_DIR": str(admin_data_dir),
            "PYTHONPATH": str(ROOT),
            "LECTIO_SECURITY_MODE": "multi",
            "LECTIO_ADMIN_USERNAME": admin_user,
            "LECTIO_ADMIN_PASSWORD": admin_pw,
            "LECTIO_SECRET_KEY": secrets.token_hex(32),
            "LECTIO_HTTPS_ONLY": "0",
            "LECTIO_DEBUG": "0",
            "LECTIO_DISABLE_STARTUP_BACKFILL": "1",
            # Blank out any real instance config from the developer's .env so it
            # can't land in the committed Administration screenshot.
            "RESEND_API_KEY": "",
            "LECTIO_EMAIL_FROM": "lectio@demo.example",
            "LECTIO_EMAIL_TO": "you@demo.example",
            "YOUTUBE_API_KEY": "",
            "YOUTUBE_CHANNEL_ID": "",
        })
        print(f"Starting multi-user Lectio on {admin_url}…")
        admin_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app",
             "--host", "127.0.0.1", "--port", str(admin_port), "--log-level", "warning"],
            env=admin_env, cwd=str(ROOT),
        )
        procs.append(admin_proc)
        _wait_healthy(admin_url + "/login", admin_proc)
        capture.capture_admin(admin_url, args.out, admin_user, admin_pw)
        print("Done.")
        return 0
    finally:
        for proc in procs:
            _stop(proc)
        for d in (data_dir, admin_data_dir):
            if args.keep_data:
                print(f"Kept demo data dir: {d}")
            else:
                shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
