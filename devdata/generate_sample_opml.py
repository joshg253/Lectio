from __future__ import annotations

import random
import xml.etree.ElementTree as ET
from pathlib import Path

SEED = 20260410
OUT = Path(__file__).with_name("sample_test_set.opml")

FEEDS = [
    ("Tech", "https://hnrss.org/frontpage"),
    ("Tech", "https://planetpython.org/rss20.xml"),
    ("Tech", "https://www.reddit.com/r/python/.rss"),
    ("News", "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml"),
    ("News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Science", "https://www.sciencedaily.com/rss/top/science.xml"),
    ("Blogs", "https://death.andgravity.com/_feed/index.xml"),
    ("Blogs", "https://realpython.com/atom.xml"),
    ("Podcasts", "https://feeds.simplecast.com/54nAGcIl"),
    ("Gaming", "https://www.pcgamer.com/rss/"),
]


def build_opml() -> ET.Element:
    random.seed(SEED)
    root = ET.Element("opml", version="1.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "Lectio Sample Test Set"
    body = ET.SubElement(root, "body")

    grouped: dict[str, list[str]] = {}
    for folder, url in FEEDS:
        grouped.setdefault(folder, []).append(url)

    for folder_name in sorted(grouped.keys()):
        folder_outline = ET.SubElement(
            body,
            "outline",
            text=folder_name,
            title=folder_name,
        )
        urls = grouped[folder_name]
        random.shuffle(urls)
        for url in urls:
            ET.SubElement(
                folder_outline,
                "outline",
                type="rss",
                text=url,
                title=url,
                xmlUrl=url,
            )

    return root


def main() -> None:
    root = build_opml()
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    OUT.write_bytes(xml)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
