from analysis.signal_rating import rate, tag


def test_strong_spec_and_coin_in_a_normal_market_is_an_a():
    r = rate("swing_vol_breakout", "8h", "SOLUSDT", 0.5)
    assert r["grade"] == "A" and r["regime"] == "normal" and r["expected_r"] > 0.5


def test_weak_coin_and_spec_is_lower_and_wild_markets_are_c():
    assert rate("swing_ichimoku", "4h", "XRPUSDT", 0.2)["grade"] == "C"
    assert rate("swing_vol_breakout", "8h", "SOLUSDT", 0.9)["grade"] == "C"


def test_unknown_inputs_use_the_book_average_and_tag_reads_well():
    r = rate("swing_new", "1h", "NEWUSDT", None)
    assert r["expected_r"] == 0.357 and tag(r) == "rating=B(+0.36R)"
