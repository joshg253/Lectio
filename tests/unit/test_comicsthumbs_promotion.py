"""ComicControl webcomics embed a small /comicsthumbs/<ts>-<file> image in feed
content. The thumb and the full /comics/ panel carry DIFFERENT cache-bust
timestamp prefixes, so a naive directory swap keeps the wrong timestamp — which
ComicControl serves as a 200 HTML page, breaking the comic. _promote_comicsthumbs_in_content
substitutes the resolved full lead image when the timestamp-stripped filenames match."""
from __future__ import annotations

import main


BODY = '<p>x</p><img src="https://www.atomic-robo.com/comicsthumbs/1782426356-ARV1701_05.jpg" />'


def test_uses_lead_image_when_filenames_match():
    # Thumb ts (…356) differs from the real panel ts (…355). The lead image is the
    # correct /comics/ URL — substitute it exactly so the body image isn't broken.
    out = main._promote_comicsthumbs_in_content(
        BODY, "https://www.atomic-robo.com/comics/1782426355-ARV1701_05.jpg"
    )
    assert "comics/1782426355-ARV1701_05.jpg" in out
    assert "comicsthumbs" not in out


def test_falls_back_to_directory_swap_without_lead_image():
    out = main._promote_comicsthumbs_in_content(BODY, None)
    assert "comics/1782426356-ARV1701_05.jpg" in out
    assert "comicsthumbs" not in out


def test_does_not_substitute_a_different_comic():
    # A lead image for a different file must not replace this comic's thumb.
    out = main._promote_comicsthumbs_in_content(
        BODY, "https://www.atomic-robo.com/comics/12345-SOMETHING_ELSE.jpg"
    )
    assert "comics/1782426356-ARV1701_05.jpg" in out
    assert "SOMETHING_ELSE" not in out


def test_stable_name_strips_timestamp_prefix():
    assert (
        main._comiccontrol_stable_name(
            "https://x/comics/1782426355-ARV1701_05.jpg"
        )
        == "ARV1701_05.jpg"
    )
    # No timestamp prefix → unchanged.
    assert main._comiccontrol_stable_name("https://x/comics/cover.png") == "cover.png"
