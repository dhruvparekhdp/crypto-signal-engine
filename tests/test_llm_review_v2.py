"""Post-mortem categories from fixed rules (21 Oct 2026): left to the model they contradicted the numbers."""
from scripts.llm_review_v2 import rule_category


def res(r, best, ex, btc=0.0, after=None):
    return {"r_after_costs": r, "best_point_r": best, "exit_kind": ex, "btc_move_with_trade_pct": btc,
            "price_1_day_after_exit_r": after}


def test_btc_against_the_trade_is_a_market_wide_loss():
    assert rule_category(res(-1.02, 0.48, "stop", btc=-6.6)) == "market-wide move"     # ADA short, BTC +6.6%


def test_ladder_exit_well_below_the_best_point_gave_back_gains():
    assert rule_category(res(0.04, 1.66, "trail")) == "gave back gains"
    assert rule_category(res(1.09, 3.00, "trail")) == "gave back gains"
    assert rule_category(res(1.20, 1.90, "trail")) == "trend continued"                  # gave back < 1R


def test_wins_losses_spikes_and_drift():
    assert rule_category(res(2.97, 3.02, "target", btc=5.3)) == "market-wide move"
    assert rule_category(res(2.97, 3.02, "target", btc=1.0)) == "trend continued"
    assert rule_category(res(-1.02, 0.08, "stop", after=-1.8)) == "reversal"
    assert rule_category(res(-1.02, 0.3, "stop", after=0.4)) == "volatility spike"
    assert rule_category(res(0.27, 0.6, "time")) == "no follow-through"
    assert rule_category(res(-0.3, 0.4, "time")) == "no follow-through"
