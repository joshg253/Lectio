from services import itad_waitlist

BODY = """<div>
<a href="https://isthereanydeal.com/game/a/info/" target="_blank">Game A</a>
<div>   Historical low: $3.44   </div>
<div><div>
<a href="https://itad.link/1/" target="_blank">$3.44</a>
<span style="text-align: right"> -80% </span>
<span>on</span> GOG
</div></div></div>
<div>
<a href="https://isthereanydeal.com/game/b/info/" target="_blank">Game &amp; B</a>
<div> Historical low: $2.52 </div>
<div><div>
<a href="https://itad.link/2/" target="_blank">$4.46</a>
<span> -70% </span>
<span>on</span> GamersGate <span>with voucher</span> ITAD
</div></div></div>"""


def test_is_itad_feed():
    assert itad_waitlist.is_itad_feed("https://isthereanydeal.com/feeds/waitlist.rss?token=x")
    assert not itad_waitlist.is_itad_feed("https://example.com/feeds/waitlist.rss")


def test_reformat_folds_each_offer_onto_one_line():
    out = itad_waitlist.reformat(BODY)
    assert out.count("<li ") == 2
    assert "Game &amp; B" in out
    assert "−80% · on GOG · <strong>historical low</strong>" in out  # price equals the low
    assert "with voucher <code>ITAD</code>" in out
    assert out.index("Game A") < out.index("Game &amp; B")
    assert out.count("historical low</strong>") == 1  # Game B's $4.46 is above its $2.52 low


def test_unrecognized_markup_is_returned_unchanged():
    assert itad_waitlist.reformat("<p>hello</p>") == "<p>hello</p>"
    assert itad_waitlist.reformat("<div>Historical low: $1</div>") == "<div>Historical low: $1</div>"
