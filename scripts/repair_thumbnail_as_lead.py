"""Fix cached lead images that are really a plugin's *thumbnail* variant.

A webcomic plugin can want two different images from one page: the whole strip
in the article, and a single readable panel in the list (see ARCHITECTURE, "A
webcomic wants a different image in the list than in the article"). Penny Arcade
is the case in hand — `/comics/<slug>.jpg` versus `/comics/panels/<slug>-p1.jpg`.

Before that split existed, the page scan stored panel 1 as the *lead*, so the
article rendered a single pane where the strip should be. The code no longer
does that, but the rows it already wrote are still cached, and a cached lead is
not re-derived on read — which is why it showed as "this one and older".

Detection is exact rather than pattern-matched: for each cached row on a webcomic
feed, ask the plugin what the lead should be, then ask it for the thumbnail
variant OF THAT LEAD. Only when the cached value equals that variant is the row
provably a thumbnail sitting in the lead's place. A row that merely looks like a
panel URL is left alone.

On the live library this matched 6 rows out of 924 webcomic feeds.

Usage:
    LECTIO_DATA_DIR=/data uv run scripts/repair_thumbnail_as_lead.py \\
        --user <user_id> [--apply]

Defaults to a dry run; --apply rewrites the rows.
"""
from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "/app")

import main  # noqa: E402


def find_bad() -> list[tuple[str, str, str, str]]:
    svc = main.lead_image_service
    with main.get_meta_connection() as conn:
        feeds = [
            str(r[0]) for r in conn.execute(
                "SELECT feed_url FROM feed_lead_image_strategy WHERE strategy = 'webcomic'"
            )
        ]

    bad: list[tuple[str, str, str, str]] = []
    with main.get_reader() as reader:
        for feed in feeds:
            with main.get_meta_connection() as conn:
                rows = conn.execute(
                    "SELECT entry_id, image_url FROM entry_lead_images"
                    " WHERE feed_url = ? AND image_url IS NOT NULL", (feed,)
                ).fetchall()
            for row in rows:
                entry_id, cached = str(row["entry_id"]), str(row["image_url"])
                entry = reader.get_entry((feed, entry_id), None)
                link = str(getattr(entry, "link", "") or "") if entry else entry_id
                if not link.startswith(("http://", "https://")):
                    continue
                lead = svc._plugin_fallback_lead_image_url(
                    entry_link=link, content_html=None, summary=None
                )
                if not lead or lead == cached:
                    continue
                variant = svc._plugin_thumbnail_variant(entry_link=link, lead_url=lead)
                if variant and variant == cached:
                    bad.append((feed, entry_id, cached, lead))
    return bad


def run(apply: bool) -> int:
    bad = find_bad()
    print(f"rows whose cached lead is really a thumbnail: {len(bad)}")
    for _feed, entry_id, cached, lead in bad:
        print(f"   {entry_id}")
        print(f"      cached {cached}")
        print(f"      lead   {lead}")

    if not apply:
        print("\nDRY RUN — pass --apply to rewrite.")
        return 0

    for feed, entry_id, _cached, lead in bad:
        main.lead_image_service.store_entry_lead_image(feed, entry_id, lead)
    print(f"\nrewrote {len(bad)} row(s).")
    return len(bad)


def main_cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    main._run_in_user_context(args.user, lambda: run(args.apply))


if __name__ == "__main__":
    main_cli()
