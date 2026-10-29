"""
Kill rule and profit-withdrawal rule for the live Delta book (owner, 30 Oct 2026: "set the kill rule and profit
withdrawal rule in system, this will come after i add more than 10k, still with 500-800 i am testing it properly, i
don't want to test 50% kill rule also in real life").

Both rules run from the first deploy, but they only ENFORCE once the money put in (deposits - withdrawals, the same
number the floor uses) reaches `active_from_inr` (Rs10,000). Below that they are in test mode: they work out and
report what they would do, and never block an order or hold money back. The 50%-of-deposits floor is separate and
unchanged.

Kill rule: count live trades and the book's profit from a starting point (profit = wallet - money put in, so deposits
and withdrawals never count as wins or losses). After `kill_after_trades` (100) live trades, if that profit is below
zero, the book has not earned its keep: no new live entries until the owner acknowledges it (setting
delta_india_kill_rule_ack = the trip id in the alert); open trades keep their exchange stops. When the rules switch
from test mode to enforcing, the count starts again, so only real-size trades are judged.

Profit rule: at the first check of each month, last month's profit (same definition). If positive, `withdraw_share`
(half) of it is due to be withdrawn. While enforcing, that amount is left out of the equity the book sizes trades on
until withdrawals of at least that much are seen; in test mode it is only reported.
"""
from __future__ import annotations

import time

ACTIVE_FROM_INR = 10_000.0
KILL_AFTER_TRADES = 100
WITHDRAW_SHARE = 0.5


def _profit(wallet_inr: float, net_inr: float) -> float:
    return wallet_inr - net_inr


def evaluate(rules: dict | None, *, wallet_inr: float, net_inr: float, live_trades: int, now: float | None = None,
             active_from_inr: float = ACTIVE_FROM_INR, kill_after: int = KILL_AFTER_TRADES,
             withdraw_share: float = WITHDRAW_SHARE, ack: str = "") -> tuple[dict, list[dict]]:
    """One check. `rules` is the saved state (state["capital_rules"]); returns the new state and events to report.
    wallet_inr: total wallet now; net_inr: money put in now; live_trades: live entries placed so far (ever)."""
    now = time.time() if now is None else now
    r = dict(rules or {})
    events: list[dict] = []
    enforcing = net_inr >= active_from_inr > 0
    profit_now = _profit(wallet_inr, net_inr)

    # (re)start the kill count: first run, switching from test mode to enforcing, or the owner's acknowledgement
    if ("kill" not in r or r.get("enforcing") != enforcing
            or (r.get("kill", {}).get("tripped") and ack and ack == r["kill"].get("trip_id"))):
        if r.get("kill", {}).get("tripped") and ack and ack == r["kill"].get("trip_id"):
            events.append({"event": "kill_rule_cleared", "trip_id": ack})
        if "enforcing" in r and r.get("enforcing") != enforcing:
            events.append({"event": "capital_rules_mode", "enforcing": enforcing, "net_inr": round(net_inr, 2)})
        r["kill"] = {"since": now, "trades_at_start": int(live_trades), "profit_at_start": round(profit_now, 2),
                     "tripped": False}
    r["enforcing"] = enforcing
    k = r["kill"]
    k["trades"] = int(live_trades) - int(k["trades_at_start"])
    k["profit"] = round(profit_now - float(k["profit_at_start"]), 2)
    if not k.get("tripped") and k["trades"] >= kill_after and k["profit"] < 0:
        k["tripped"] = True
        k["trip_id"] = f"KILL-{time.strftime('%Y%m%d-%H%M', time.gmtime(now))}"
        events.append({"event": "kill_rule_tripped", "enforcing": enforcing, "trip_id": k["trip_id"],
                       "trades": k["trades"], "profit_inr": k["profit"]})

    # monthly profit
    month = time.strftime("%Y-%m", time.gmtime(now))
    m = r.get("month")
    if not m:
        r["month"] = {"month": month, "profit_at_start": round(profit_now, 2)}
    elif m["month"] != month:
        made = round(profit_now - float(m["profit_at_start"]), 2)
        due = round(made * withdraw_share, 2) if made > 0 else 0.0
        if due > 0 and enforcing:
            p = r.setdefault("reserve", {"inr": 0.0, "net_at_set": net_inr})
            p["inr"] = round(float(p["inr"]) + due, 2)
            p["net_at_set"] = net_inr
        events.append({"event": "profit_withdrawal_due", "enforcing": enforcing, "month": m["month"],
                       "profit_inr": made, "withdraw_inr": due})
        r["month"] = {"month": month, "profit_at_start": round(profit_now, 2)}
        r.setdefault("history", []).append({"month": m["month"], "profit_inr": made, "withdraw_inr": due,
                                            "enforcing": enforcing})
        r["history"] = r["history"][-24:]

    # withdrawals since the reserve was set pay it down (money put in falls by what was taken out)
    res = r.get("reserve")
    if res and res.get("inr", 0) > 0:
        taken = max(0.0, float(res["net_at_set"]) - net_inr)
        if taken > 0:
            res["inr"] = round(max(0.0, float(res["inr"]) - taken), 2)
            res["net_at_set"] = net_inr
            if res["inr"] <= 0:
                events.append({"event": "profit_withdrawn"})
    return r, events


def blocks_entries(rules: dict | None) -> str:
    """'kill_rule' when the kill rule is tripped AND enforcing, else ''."""
    r = rules or {}
    return "kill_rule" if r.get("enforcing") and (r.get("kill") or {}).get("tripped") else ""


def reserve_inr(rules: dict | None) -> float:
    """Profit held back from sizing (only while enforcing)."""
    r = rules or {}
    return float((r.get("reserve") or {}).get("inr") or 0.0) if r.get("enforcing") else 0.0


def alert_text(e: dict) -> str:
    """Plain-English Telegram text for one rules event."""
    tag = "" if e.get("enforcing", True) else " (TEST MODE: below Rs10,000 put in, nothing blocked)"
    ev = e.get("event")
    if ev == "kill_rule_tripped":
        return (f"🛑 Kill rule{tag}: after {e['trades']} live trades the book is down Rs{-e['profit_inr']:,.2f} "
                "(deposits and withdrawals not counted). "
                + ("No new live entries; open trades keep their stops. To resume, set 'Kill rule acknowledgement' to "
                   f"{e['trip_id']} in Settings after reviewing." if e.get("enforcing") else
                   "With real money this would stop new live entries until you review."))
    if ev == "profit_withdrawal_due":
        if e["withdraw_inr"] <= 0:
            return f"📅 {e['month']}: no profit to withdraw (Rs{e['profit_inr']:+,.2f}){tag}."
        return (f"💸 {e['month']} profit Rs{e['profit_inr']:,.2f}{tag}: withdraw Rs{e['withdraw_inr']:,.2f} (half). "
                + ("Until it is withdrawn, the book sizes trades without it." if e.get("enforcing") else
                   "With real money the book would stop sizing on it until withdrawn."))
    if ev == "profit_withdrawn":
        return "✅ Profit withdrawal seen: the book sizes on the full wallet again."
    if ev == "kill_rule_cleared":
        return f"▶️ Kill rule {e['trip_id']} acknowledged: live entries allowed again; the 100-trade count restarts."
    if ev == "capital_rules_mode":
        return ("🔒 Money put in reached Rs{:,.0f}: kill and profit rules now ENFORCE (count restarts)." if e["enforcing"]
                else "🧪 Money put in is below the rules' level: kill and profit rules back in test mode.").format(e["net_inr"])
    return ""
