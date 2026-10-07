"""Aiohttp web server: dashboard UI + JSON API endpoints."""
from __future__ import annotations

import asyncio
import json as _stdjson
import os
import math
import types as _types


def _json_response(data, **kw):
    kw.setdefault("dumps", _strict_dumps)
    return web.json_response(data, **kw)


def _finite(o):
    """NaN and Infinity are not JSON. A browser rejects the whole payload over one of them (a released
    paper target is stored as Infinity), so every API response sends null instead."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


def _strict_dumps(obj, **kw):
    kw.pop("allow_nan", None)
    return _stdjson.dumps(_finite(obj), allow_nan=False, **kw)


json = _types.SimpleNamespace(**{k: getattr(_stdjson, k) for k in dir(_stdjson) if not k.startswith("__")})
json.dumps = _strict_dumps
import math
from datetime import UTC, datetime, timedelta

from aiohttp import web

from analysis.scalp_levels import ScalpConfig
from config.settings import settings as _SETTINGS

_start_time = datetime.now(UTC)

# One cost model for the page and the engine. The dashboard used to carry its
# own copy of the fee arithmetic in JavaScript, which drifted the moment the
# fees were recalibrated against the real ledger.
_SCALP = ScalpConfig()



# ── JSON API ──────────────────────────────────────────────────────────────────

async def _api_status(runner, request: web.Request) -> web.Response:
    status = runner.get_status()
    count = await runner.crypto_store.count()
    uptime = int((datetime.now(UTC) - _start_time).total_seconds())
    return web.Response(
        text=json.dumps({
            "uptime_seconds": uptime,
            "symbols_tracked": count,
            **status,
        }),
        content_type="application/json",
    )


async def _api_crypto_coins(runner, request: web.Request) -> web.Response:
    """Return live crypto watchlist market states with indicators."""
    states = await runner.crypto_store.get_all()
    coins = []
    for s in states:
        coins.append({
            "symbol": s.symbol.upper(),
            "base_asset": s.base_asset,
            "price": s.current_price,
            "change_24h_pct": round(s.price_change_24h_pct, 2),
            "volume_24h": s.volume_24h,
            "volume_ratio": round(s.volume_ratio, 2),
            # The number that decides every level. Surfacing it makes a dead
            # feed visible: 0.05% where the market really moves 0.15% is why
            # every target came out identical.
            "atr_pct": (round(s.atr_14 / s.current_price * 100, 4)
                        if s.current_price > 0 else None),
            "high_24h": s.high_24h,
            "low_24h": s.low_24h,
            "rsi_14": s.rsi_14,
            "macd_line": round(s.macd_line, 4),
            "bollinger_bandwidth": round(s.bollinger_bandwidth * 100, 2),
            "sentiment_score": s.sentiment_score,
            "timestamp": _iso(s.timestamp),
        })
    return web.Response(text=json.dumps(coins), content_type="application/json")


async def _api_crypto_signals(runner, request: web.Request) -> web.Response:
    """Return recent crypto trade signals with live performance tracking and Hugging Face AI dual reasoning."""
    from config.settings import settings
    from scheduler.pipeline import REASON_WORDS
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    from analysis.trade_evaluator import evaluate_signal_trade_path, get_mirror_ai_dual_reasoning

    mirror_mode = request.query.get('mirror', '') == '1'
    hours = int(request.query.get('hours', '48' if mirror_mode else '24'))
    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        rows = await repo.get_recent_crypto_signals(hours=hours)
        rows = [r for r in rows if getattr(r, 'candidate_role', 'primary') == ('mirror' if mirror_mode else 'primary')]
        trails: dict[int, list] = {}
        if settings.mirror_review_enabled:
            for r in rows:
                if (r.candidate_role == "primary" and r.mirror_of_log_id == 0
                        and r.review_round == 0):
                    trail_rows = await repo.get_review_trail(r.id)
                    if len(trail_rows) > 1:
                        trails[r.id] = trail_rows

    states = {}
    if runner and hasattr(runner, "crypto_store"):
        try:
            states = {st.symbol.lower(): st for st in await runner.crypto_store.get_all()}
        except Exception:
            pass

    tc_dict = {}
    ai_dict = {}

    for r in rows:
        st = states.get(r.symbol.lower())
        candles = st.candles_1m if st else []
        current_p = st.current_price if st else None
        tc = evaluate_signal_trade_path(r, candles, current_price=current_p)
        tc_dict[r.id] = tc

    if mirror_mode and rows:
        async def _fetch_ai(sig_row):
            tc = tc_dict.get(sig_row.id, {})
            try:
                return await get_mirror_ai_dual_reasoning(sig_row, tc)
            except Exception:
                return {
                    "why_it_worked": "Initial technical momentum aligned with setup criteria.",
                    "why_it_failed": "Counter-trend orderflow or resistance capped further progression.",
                    "key_takeaway": "Enforce strict risk management and trailing profit stops.",
                    "source_model": "fallback",
                }

        ai_res_list = await asyncio.gather(*[_fetch_ai(r) for r in rows])
        for r, ai_res in zip(rows, ai_res_list):
            ai_dict[r.id] = ai_res

    signals = [
        {
            "id": r.id,
            "symbol": r.symbol.upper(),
            "signal_type": r.signal_type,
            "direction": r.direction,
            "trigger": r.trigger_description,
            "confidence": round(r.confidence * 100),
            "current_price": r.current_price,
            "target_price": r.target_price,
            "stop_loss": r.stop_loss,
            "edge_pct": r.edge_pct,
            "stake_pct": round(r.stake_pct * 100, 2),
            "timeframe": r.timeframe,
            "sentiment_score": r.sentiment_score,
            "indicators": r.indicators_summary,
            "outcome": r.outcome,
            "pnl_pct": getattr(r, "pnl_pct", 0.0),
            "timestamp": _iso(r.timestamp),
            "skip_reason": r.skip_reason or None,
            "skip_reason_text": (REASON_WORDS.get(r.skip_reason, r.skip_reason.replace("_", " "))
                                 if r.skip_reason else None),
            "trade_mode": getattr(r, "trade_mode", "intraday"),
            "veto_reason": getattr(r, "veto_reason", "") or r.suppressed_by or None,
            "trade_check": tc_dict.get(r.id),
            "ai_reasoning": ai_dict.get(r.id),
            **({"review_trail": [_review_trail_row(t, REASON_WORDS) for t in trails[r.id]]}
               if r.id in trails else {}),
        }
        for r in rows
    ]

    if mirror_mode:
        total = len(rows)
        worked_cnt = sum(1 for r in rows if tc_dict.get(r.id, {}).get("worked"))
        full_cnt = sum(1 for r in rows if tc_dict.get(r.id, {}).get("status") == "won")
        part_cnt = sum(1 for r in rows if tc_dict.get(r.id, {}).get("status") == "partial")
        stop_cnt = sum(1 for r in rows if tc_dict.get(r.id, {}).get("status") == "stopped")
        run_cnt = sum(1 for r in rows if tc_dict.get(r.id, {}).get("status") == "running")
        avg_peak = round(sum(tc_dict.get(r.id, {}).get("peak_gain_pct", 0.0) for r in rows) / max(1, total), 2)
        avg_tgt = round(sum(tc_dict.get(r.id, {}).get("target_pct_reached", 0.0) for r in rows) / max(1, total), 1)

        summary = {
            "total_tested": total,
            "worked_count": worked_cnt,
            "worked_rate_pct": round(worked_cnt / max(1, total) * 100.0, 1),
            "full_win_count": full_cnt,
            "partial_win_count": part_cnt,
            "stopped_count": stop_cnt,
            "running_count": run_cnt,
            "avg_peak_gain_pct": avg_peak,
            "avg_target_reached_pct": avg_tgt,
        }
        return web.Response(text=json.dumps({"signals": signals, "summary": summary}), content_type="application/json")

    return web.Response(text=json.dumps(signals), content_type="application/json")


_sim_process: asyncio.subprocess.Process | None = None


async def _api_crypto_signals_live_check(runner, request: web.Request) -> web.Response:
    """Force re-run of live checks and Hugging Face AI review on mirror signals."""
    from analysis.trade_evaluator import _TRADE_EVAL_CACHE, _AI_REASONING_CACHE
    _TRADE_EVAL_CACHE.clear()
    _AI_REASONING_CACHE.clear()
    if runner and hasattr(runner, "_resolve_signal_outcomes_job"):
        try:
            await runner._resolve_signal_outcomes_job()
        except Exception:
            pass
    return await _api_crypto_signals(runner, request)


async def _api_simulator_status(runner, request: web.Request) -> web.Response:
    """Return live status, progress, test counts, and recent trades from the simulator."""
    from pathlib import Path
    status_file = Path("data/simulator/status.json")
    if status_file.exists():
        try:
            d = json.loads(status_file.read_text())
            # Ensure cycle transactions are stripped for mobile performance
            if "cycle_challenge" in d and isinstance(d["cycle_challenge"], dict):
                cc = d["cycle_challenge"]
                if "cycles" in cc and isinstance(cc["cycles"], list):
                    light_cycles = []
                    for c in cc["cycles"]:
                        if isinstance(c, dict):
                            sc = {k: v for k, v in c.items() if k != "transactions"}
                            sc["transaction_count"] = len(c.get("transactions", [])) if "transactions" in c else c.get("transaction_count", 0)
                            light_cycles.append(sc)
                    cc["cycles"] = light_cycles
            return web.Response(text=json.dumps(d), content_type="application/json")
        except Exception:
            pass
    return web.Response(text=json.dumps({
        "is_running": False,
        "progress_pct": 0.0,
        "status": "idle",
        "ticks_processed": 0,
        "trades_simulated": 0,
        "won_count": 0,
        "partial_count": 0,
        "stopped_count": 0,
        "stagnated_count": 0,
        "win_rate_pct": 0.0,
        "profit_factor": 1.0,
        "recent_trades": [],
    }), content_type="application/json")


async def _api_simulator_start(runner, request: web.Request) -> web.Response:
    """Launch the 3-Year Live Market Simulator as an asynchronous background worker."""
    global _sim_process
    import sys
    try:
        data = await request.json() if request.can_read_body else {}
    except Exception:
        data = {}

    symbols = data.get("symbols", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,AVAXUSDT,LINKUSDT")
    if isinstance(symbols, list):
        symbols = ",".join(symbols)
    years = str(data.get("years", "3.0"))
    tp_r = str(data.get("tp_r", "2.0"))
    sl_r = str(data.get("sl_r", "1.2"))
    anti_flip = str(data.get("anti_flip", "90"))
    stagnation = str(data.get("stagnation", "60"))
    max_ai = str(data.get("max_ai_reviews", "100"))
    cycle_start = str(data.get("cycle_start", "25.0"))
    cycle_target = str(data.get("cycle_target", "100.0"))
    cycle_margin = str(float(data.get("cycle_margin_pct", "25.0")) / 100.0)
    cycle_lev = str(data.get("cycle_leverage", "10.0"))
    stepped_mode = data.get("stepped_mode", True)
    step_market_hours = str(data.get("step_market_hours", "1.0"))
    step_seconds = str(data.get("step_seconds", "60.0"))
    strategy = str(data.get("strategy", "all")).strip()
    gemini_key = str(data.get("gemini_key", os.getenv("GEMINI_API_KEY", ""))).strip()
    hf_tokens = str(data.get("hf_tokens", "")).strip()
    openrouter_key = str(data.get("openrouter_key", "")).strip()
    ai_provider = str(data.get("ai_provider", "none")).strip()

    # Save configuration to data/simulator/config.json for persistence
    try:
        from pathlib import Path
        cfg_path = Path("data/simulator/config.json")
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

    cmd = [
        sys.executable, "-m", "scripts.run_market_simulator",
        "--symbols", symbols,
        "--years", years,
        "--tp-r", tp_r,
        "--sl-r", sl_r,
        "--anti-flip", anti_flip,
        "--stagnation", stagnation,
        "--max-ai-reviews", max_ai,
        "--cycle-start", cycle_start,
        "--cycle-target", cycle_target,
        "--cycle-margin-pct", cycle_margin,
        "--cycle-leverage", cycle_lev,
        "--strategy", strategy,
    ]
    if stepped_mode:
        cmd.append("--stepped-mode")
    cmd.extend(["--step-market-hours", step_market_hours, "--step-seconds", step_seconds])
    if ai_provider:
        cmd.extend(["--ai-provider", ai_provider])
    # keys travel in the environment, never on the command line (visible in the process list)
    import os as _os
    env = {**_os.environ, "SIM_GEMINI_KEY": gemini_key or "", "SIM_HF_TOKENS": hf_tokens or "",
           "SIM_OPENROUTER_KEY": openrouter_key or ""}

    try:
        _sim_process = await asyncio.create_subprocess_exec(*cmd, env=env)
        return web.Response(text=json.dumps({"status": "started", "pid": _sim_process.pid}), content_type="application/json")
    except Exception as exc:
        return web.Response(text=json.dumps({"status": "error", "message": str(exc)}), status=500, content_type="application/json")


async def _api_simulator_ai_events(runner, request: web.Request) -> web.Response:
    """Return live stream of AI inference call events."""
    from pathlib import Path
    events_file = Path("data/simulator/ai_events.json")
    if events_file.exists():
        try:
            return web.Response(text=events_file.read_text(), content_type="application/json")
        except Exception:
            pass
    return web.Response(text="[]", content_type="application/json")


async def _api_simulator_cycles(runner, request: web.Request) -> web.Response:
    """Return the detailed account statements and metrics for all challenge cycles."""
    import os
    report_file = os.path.join(runner.config.project_root if hasattr(runner, "config") else ".", "data/simulator/reports/cycle_statements.json")
    if os.path.exists(report_file):
        try:
            with open(report_file, "r") as f:
                return web.Response(text=f.read(), content_type="application/json")
        except Exception:
            pass
    status_file = "data/simulator/status.json"
    if os.path.exists(status_file):
        try:
            with open(status_file, "r") as f:
                d = json.load(f)
                return web.Response(text=json.dumps(d.get("cycle_challenge", {})), content_type="application/json")
        except Exception:
            pass
    return web.Response(text=json.dumps({"cycles": []}), content_type="application/json")


async def _api_simulator_cycle_ledger(runner, request: web.Request) -> web.Response:
    """Return paginated ledger transactions for a single cycle."""
    from pathlib import Path
    cycle_id_str = request.query.get("cycle_id", "")
    page = max(1, int(request.query.get("page", 1)))
    limit = max(5, min(100, int(request.query.get("limit", 15))))
    report_file = Path("data/simulator/reports/cycle_statements.json")
    if not report_file.exists():
        return web.Response(text=json.dumps({"error": "No cycle statements report found", "transactions": []}), content_type="application/json")
    try:
        report_data = json.loads(report_file.read_text())
        cycles = report_data.get("cycles", [])
        matched = None
        if cycle_id_str:
            for c in cycles:
                if str(c.get("cycle_id")) == cycle_id_str:
                    matched = c
                    break
        elif cycles:
            matched = cycles[0]
        if not matched:
            return web.Response(text=json.dumps({"error": "Cycle not found", "transactions": []}), content_type="application/json")
        
        all_tx = matched.get("transactions", [])
        total_tx = len(all_tx)
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit
        paginated_tx = all_tx[start_idx:end_idx]
        
        summary = {k: v for k, v in matched.items() if k != "transactions"}
        return web.Response(text=json.dumps({
            "cycle": summary,
            "transactions": paginated_tx,
            "page": page,
            "limit": limit,
            "total_transactions": total_tx,
            "total_pages": max(1, (total_tx + limit - 1) // limit),
        }), content_type="application/json")
    except Exception as e:
        return web.Response(text=json.dumps({"error": str(e), "transactions": []}), status=500, content_type="application/json")


async def _api_simulator_pause(runner, request: web.Request) -> web.Response:
    """Pause or stop the simulator background worker."""
    global _sim_process
    if _sim_process and _sim_process.returncode is None:
        try:
            _sim_process.terminate()
            return web.Response(text=json.dumps({"status": "terminated"}), content_type="application/json")
        except Exception as exc:
            return web.Response(text=json.dumps({"status": "error", "message": str(exc)}), status=500, content_type="application/json")
    return web.Response(text=json.dumps({"status": "not_running"}), content_type="application/json")


async def _api_simulator_get_config(runner, request: web.Request) -> web.Response:
    """Return saved simulator configuration, or fallback defaults."""
    from pathlib import Path
    cfg_file = Path("data/simulator/config.json")
    defaults = {
        "years": "0.0082",
        "step_market_hours": 1.0,
        "step_seconds": 60.0,
        "stepped_mode": True,
        "tp_r": 2.2,
        "sl_r": 1.5,
        "anti_flip": 90,
        "stagnation": 60,
        "max_ai_reviews": 150,
        "cycle_start": 25.0,
        "cycle_target": 100.0,
        "cycle_margin_pct": 25.0,
        "cycle_leverage": 10.0,
        "strategy": "all",
        "ai_provider": "none",
        "gemini_key": "",          # never echo a server-side key to the browser
        "openrouter_key": "",
        "hf_tokens": "",
    }
    if cfg_file.exists():
        try:
            saved = json.loads(cfg_file.read_text())
            defaults.update(saved)
        except Exception:
            pass
    return web.Response(text=json.dumps(defaults), content_type="application/json")


async def _api_simulator_save_config(runner, request: web.Request) -> web.Response:
    """Persist simulator configuration to data/simulator/config.json."""
    from pathlib import Path
    try:
        data = await request.json() if request.can_read_body else {}
        cfg_file = Path("data/simulator/config.json")
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        # Load existing, update with incoming
        existing = {}
        if cfg_file.exists():
            try:
                existing = json.loads(cfg_file.read_text())
            except Exception:
                pass
        existing.update(data)
        cfg_file.write_text(json.dumps(existing, indent=2))
        return web.Response(text=json.dumps({"status": "saved", "config": existing}), content_type="application/json")
    except Exception as exc:
        return web.Response(text=json.dumps({"status": "error", "message": str(exc)}), status=500, content_type="application/json")



def _review_trail_row(t, reason_words: dict) -> dict:
    """
    One row of a signal's mirror-review trail — see _api_crypto_signals.

    Status is read off the row itself: a rejection_reason means the
    candidate stopped tracking without a trade; otherwise skip_reason set
    (paper trading off, below the paper floor, etc — the ordinary "why no
    trade" reasons) or an outcome other than "pending" both mean this exact
    row is the one a trade opened from; anything else is still tracking.
    """
    role = "Mirror" if t.candidate_role == "mirror" else "Primary"
    label = f"{role} ({t.direction.capitalize()})"
    if t.rejection_reason:
        status = "Rejected — " + reason_words.get(t.rejection_reason, t.rejection_reason)
    elif t.skip_reason == "mirror_review_tracking":
        status = "Still tracking"
    else:
        # Cleared skip_reason (opened), or a real "why no trade" reason from
        # the ordinary paper-trading pipeline (below its floor, book full,
        # etc) — either way this candidate won its round and was queued.
        status = "Traded" if not t.skip_reason else (
            "Not traded — " + reason_words.get(t.skip_reason, t.skip_reason))
    return {
        "label": label,
        "candidate_role": t.candidate_role,
        "direction": t.direction,
        "review_round": t.review_round,
        "confidence": round(t.confidence * 100),
        "timestamp": _iso(t.timestamp),
        "rejection_reason": t.rejection_reason or None,
        "rejection_reason_text": (reason_words.get(t.rejection_reason, t.rejection_reason)
                                  if t.rejection_reason else None),
        "status": status,
    }



def _iso(dt):
    """
    Serialise a timestamp so the browser cannot mistake it for local time.

    Every DateTime column here is naive UTC, and _iso(datetime) on a
    naive value emits no offset. JavaScript parses an offset-less date-time as
    LOCAL time, so a browser in IST read a UTC instant as an IST wall clock —
    signals displayed 5h30m early with an "ago" that was 5h30m too large. The
    stored instants were always correct; only the wire format was ambiguous.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _signal_row(r) -> dict:
    return {
        "id": r.id, "symbol": r.symbol.upper(), "signal_type": r.signal_type,
        "direction": r.direction, "confidence": round(r.confidence * 100),
        "current_price": r.current_price, "target_price": r.target_price,
        "stop_loss": r.stop_loss, "edge_pct": r.edge_pct,
        "timeframe": r.timeframe, "outcome": r.outcome, "pnl_pct": r.pnl_pct,
        "timestamp": _iso(r.timestamp),
        "trade_mode": getattr(r, "trade_mode", "intraday"),
        "veto_reason": getattr(r, "veto_reason", "") or getattr(r, "suppressed_by", "") or getattr(r, "skip_reason", "") or None,
    }


async def _api_predict(runner, request: web.Request) -> web.Response:
    """
    A band and a lean for every watchlist market, at three horizons.

    Separate from the signal feed on purpose. Signals answer "is there a trade
    worth its costs right now", and the honest answer is usually no — which
    leaves the board empty and says nothing about the market. This says
    something about every market, all the time.
    """
    from analysis import indicators as ind
    from analysis.forecast import detect_surge, forecast_symbol

    rows, surges = [], []
    for st in sorted(await runner.crypto_store.get_all(), key=lambda x: x.symbol):
        fc = forecast_symbol(st)
        candles = st.candles_1m or []
        closed = [c for c in candles if c.is_closed]
        rel = ind.relative_volume([c.volume for c in closed]) if closed else None
        newest = candles[-1].timestamp if candles else None
        lag = (None if newest is None else
               round((datetime.now(UTC) - (newest if newest.tzinfo else newest.replace(tzinfo=UTC))).total_seconds() / 60, 1))
        row = {
            "symbol": st.symbol.upper(),
            "price": st.current_price,
            "change_24h_pct": round(st.price_change_24h_pct, 2),
            "rsi": round(st.rsi_14, 1),
            "atr_pct": (round(st.atr_14 / st.current_price * 100, 4)
                        if st.current_price > 0 else None),
            "relative_volume": round(rel, 2) if rel is not None else None,
            "candles": len(candles),
            "data_age_minutes": lag,
            # Anything older than half an hour is not a current view of the
            # market, and a page that renders it identically to a live one is
            # how a stale board gets traded.
            "stale": lag is not None and lag > 30,
            "forecasts": [f.as_dict() for f in fc],
        }
        if fc:
            row["direction"] = fc[0].direction
            row["confidence"] = fc[0].confidence
        rows.append(row)

        surge = detect_surge(st)
        if surge is not None:
            surges.append({
                "symbol": surge.symbol.upper(), "kind": surge.kind,
                "detail": surge.detail, "magnitude": surge.magnitude,
                "direction": surge.direction,
            })

    surges.sort(key=lambda x: -x["magnitude"])
    return _json_response({
        "generated_at": _iso(datetime.now(UTC)),
        "note": ("The band is roughly one standard deviation of this market's "
                 "own recent range projected over the horizon: price should "
                 "land inside it about two times in three. The centre is "
                 "shifted from spot by where the evidence leans, capped at a "
                 "third of the band — no indicator set earns more than that."),
        "markets": rows,
        "surges": surges,
    })


def _blocked_downstream(runner, state, direction: str) -> str | None:
    """
    Would the live engine's HTF-trend filter or cooldown veto this direction?

    `evaluate()` — the confluence vote — is the only gate /api/debug/signals
    used to check. It agreeing is not the whole story: CryptoEngine.process()
    runs two more gates after it that this endpoint had no way to see,
    reporting "WOULD FIRE" for setups already vetoed live a moment earlier.
    """
    engine = getattr(runner, "crypto_engine", None)
    if engine is None:
        return None
    import types
    fake = types.SimpleNamespace(symbol=state.symbol, direction=direction,
                                 signal_type="confluence")
    try:
        if engine._opposes_htf_trend(fake, state):
            return "against the 1h/15m or daily trend"
        if engine._is_on_cooldown(state.symbol, "confluence"):
            return "same coin fired recently — on cooldown"
    except Exception:
        return None
    return None


async def _api_debug_signals(runner, request: web.Request) -> web.Response:
    """
    Why each watchlist symbol did or did not produce a signal, right now.

    Written because "only XRP is firing" cannot be answered from the outside:
    every gate that refuses a setup logs at debug and then the setup vanishes.
    This asks each gate the same question the engine does and reports the
    first one that says no, per symbol — including the two gates that run
    after the confluence vote (see _blocked_downstream).
    """
    from analysis import indicators as ind
    from analysis.confluence import ConvictionGate, evaluate
    from analysis.crypto_signals import GATE, SCALP
    from analysis.scalp_levels import NoTrade, REASON_TEXT, scalp_levels

    # The feed's own state, first. Every number below is downstream of it, and
    # a stale feed renders exactly like a live one.
    kl = getattr(runner, "klines", None)
    feed = {"source": "binance klines (REST)"}
    if kl is not None:
        age = (None if kl.last_success is None else
               round((datetime.now(UTC) - (kl.last_success if kl.last_success.tzinfo else kl.last_success.replace(tzinfo=UTC))).total_seconds() / 60, 1))
        feed.update({
            "host": kl.host or "none answered",
            "last_success_minutes_ago": age,
            "refreshed_last_sweep": sorted(kl.refreshed),
            "per_symbol": kl.status,
            "last_error": kl.last_error or None,
        })
        if age is None:
            feed["warning"] = ("this feed has never succeeded — every candle "
                               "below is preloaded archive data or poll "
                               "aggregation, not live market data")

    out = []
    for st in sorted(await runner.crypto_store.get_all(), key=lambda x: x.symbol):
        cfg = SCALP.for_symbol(st.symbol)
        row = {
            "symbol": st.symbol.upper(),
            "price": st.current_price,
            "candles": len(st.candles_1m),
            "cost_floor_pct": round(cfg.cost_floor_pct * 100, 4),
            "min_target_pct": round(cfg.min_target_pct * 100, 4),
        }

        if st.current_price <= 0:
            row["verdict"] = "no price feed"
            out.append(row)
            continue

        atr_pct = st.atr_14 / st.current_price if st.atr_14 > 0 else 0.0
        row["atr_pct"] = round(atr_pct * 100, 4)
        row["atr_vs_floor"] = round(atr_pct / cfg.cost_floor_pct, 2) if cfg.cost_floor_pct else None

        if len(st.candles_1m) < 60:
            row["verdict"] = f"warming up — {len(st.candles_1m)}/60 candles"
            out.append(row)
            continue

        highs = [c.high for c in st.candles_1m]
        lows = [c.low for c in st.candles_1m]
        closes = [c.close for c in st.candles_1m]
        flat = sum(1 for c in st.candles_1m if c.high == c.low)
        row["flat_candles"] = f"{flat}/{len(st.candles_1m)}"
        pctile = ind.volatility_percentile(highs, lows, closes)
        row["vol_percentile"] = round(pctile, 2) if pctile is not None else None

        # The same call the analyzers make, so the reason is the real one.
        levels = scalp_levels(st.current_price, True, atr_pct, cfg, symbol=st.symbol)
        if isinstance(levels, NoTrade):
            row["verdict"] = REASON_TEXT.get(levels, levels.value)
            row["gate"] = levels.value
            out.append(row)
            continue
        row["would_target_pct"] = round(levels.target_pct * 100, 4)
        row["would_stop_pct"] = round(levels.stop_pct * 100, 4)
        row["would_take_minutes"] = round(levels.horizon_minutes)
        # The share of the risk the fee eats. Above ~0.35 the trade is mostly
        # a bet on covering its own costs, which is what the whole board was.
        row["cost_share_of_risk"] = (round(levels.cost_pct / levels.stop_pct, 3)
                                     if levels.stop_pct else None)
        row["reward_risk"] = round(levels.reward_risk, 2)
        bar = ind.median_bar_minutes([c.timestamp for c in st.candles_1m])
        row["bar_minutes"] = round(bar, 2)
        # A one-minute series whose bars are not a minute apart is not a
        # one-minute series, and every time estimate built on it is wrong by
        # that factor.
        if bar > 1.5:
            row["bar_warning"] = (f"bars are {bar:.0f} minutes apart, not 1 — "
                                  f"ATR and every time estimate are wrong by "
                                  f"about {bar:.0f}x")
        last = st.candles_1m[-1].timestamp if st.candles_1m else None
        if last is not None:
            last_aware = last if last.tzinfo else last.replace(tzinfo=UTC)
            lag = (datetime.now(UTC) - last_aware).total_seconds() / 60
            row["newest_candle_minutes_ago"] = round(lag, 1)
            if lag > 30:
                row["stale_warning"] = ("newest candle is "
                                        f"{lag / 60:.1f} hours old")
        # Closed bars only — the minute in progress has traded almost nothing,
        # so including it reports a normally trading market as having no volume.
        vols = [c.volume for c in st.candles_1m if c.is_closed]
        row["per_bar_volume"] = ind.has_usable_volume(vols)
        rel = ind.relative_volume(vols)
        row["relative_volume"] = round(rel, 2) if rel is not None else None

        v = evaluate(st.candles_1m, min_atr_pct=cfg.cost_floor_pct,
                     min_agreeing=GATE.min_agreeing, max_dissent=GATE.max_dissent)
        row["votes"] = {x.family.value: x.direction for x in v.votes}
        # Counted from the votes rather than read off the verdict. A vetoed
        # verdict has no direction, so the verdict's own tally reports 0 for
        # both sides — which hid that ETH had three families agreeing and was
        # refused by a volume reading taken from a three-second-old bar.
        longs = sum(1 for x in v.votes if x.direction > 0)
        shorts = sum(1 for x in v.votes if x.direction < 0)
        row["agreeing"] = max(longs, shorts)
        row["dissenting"] = min(longs, shorts)
        row["leaning"] = "long" if longs > shorts else ("short" if shorts > longs else "split")
        blocked = None if v.direction is None else _blocked_downstream(runner, st, v.direction)
        if v.direction is None:
            row["verdict"] = v.vetoes[0] if v.vetoes else "no majority"
            row["gate"] = "confluence"
        elif blocked is not None:
            # Confluence agrees, but two gates after it — the HTF/daily
            # trend filter and the per-symbol cooldown — are not part of
            # `evaluate()` and were reporting WOULD FIRE for setups the live
            # engine had already vetoed a moment earlier (visible on
            # /api/pipeline as "blocked: against the daily trend").
            row["verdict"] = f"blocked live too: {blocked}"
            row["gate"] = "htf_trend"
        else:
            row["verdict"] = f"WOULD FIRE {v.direction} at {v.confidence * 100:.0f}%"
            row["gate"] = None
        out.append(row)

    fired = [r for r in out if r.get("gate") is None and "WOULD" in r.get("verdict", "")]
    return _json_response({
        "checked": len(out),
        "would_fire": len(fired),
        "gate_profile": GATE.label,
        "edge_multiple": SCALP.min_edge_multiple,
        "feed": feed,
        "symbols": out,
    }, dumps=lambda o: json.dumps(o, indent=2, default=str))


async def _api_signal_history(runner, request: web.Request) -> web.Response:
    """
    Signals in a window. `before` carves out the recent end, which is what
    separates the dashboard's live 7 days from the archive behind it.
    """
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    days = max(1, min(365, int(request.query.get("days") or 7)))
    before = max(0, min(365, int(request.query.get("before") or 0)))
    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        rows = await repo.crypto_signals_between(days, before)
        counts = await repo.crypto_signal_counts() if before else None

    payload = [_signal_row(r) for r in rows]
    if counts is None:
        return _json_response(payload)
    return _json_response({"signals": payload, "counts": counts})


async def _api_signal_accuracy(runner, request: web.Request) -> web.Response:
    """
    Calibration, move-size distribution and per-setup accuracy.

    Buckets that have no resolved signals report a null win rate rather than
    zero — a bucket nobody has traded is not a bucket that loses.
    """
    import statistics

    from analysis.scalp_levels import ScalpConfig
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        rows = await repo.crypto_signals_between(365)
        counts = await repo.crypto_signal_counts()

    done = [r for r in rows if r.outcome in ("won", "lost")]
    moves = [abs(r.target_price - r.current_price) / r.current_price * 100
             for r in rows if r.current_price and r.target_price]

    def rate(group):
        g = [r for r in group if r.outcome in ("won", "lost")]
        if not g:
            return None, 0
        return round(sum(1 for r in g if r.outcome == "won") / len(g) * 100, 1), len(g)

    calibration = []
    for lo in (60, 65, 70, 75, 80, 85, 90):
        band = [r for r in done if lo <= r.confidence * 100 < lo + 5]
        wr, n = rate(band)
        if n:
            calibration.append({"bucket": lo, "win_rate_pct": wr, "n": n})

    by_setup = []
    for kind in sorted({r.signal_type for r in done}):
        wr, n = rate([r for r in done if r.signal_type == kind])
        if n:
            by_setup.append({"signal_type": kind, "win_rate_pct": wr, "n": n})

    edges = [0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.8, 1.2, 2.0, 5.0]
    buckets, prev = [], 0.0
    for e in edges:
        buckets.append({"upper_pct": e,
                        "n": sum(1 for m in moves if prev <= m < e)})
        prev = e

    overall, resolved = rate(rows)
    return _json_response({
        "total": len(rows), "resolved": resolved, "pending": counts["pending"],
        "win_rate_pct": overall,
        "median_move_pct": round(statistics.median(moves), 4) if moves else None,
        "min_target_pct": round(ScalpConfig().min_target_pct * 100, 4),
        "calibration": calibration, "by_setup": by_setup, "move_buckets": buckets,
    })


async def _api_debug_volume(runner, request: web.Request) -> web.Response:
    """
    Whether each watchlist symbol has usable per-bar volume, and what the
    venue actually returns for candles.

    Written because "the volume family abstained" is invisible from outside:
    the family votes zero, the setup falls one short of the gate, and the
    signal simply never appears. This says so out loud, and prints the raw
    first candle so a changed response shape can be recognised rather than
    guessed at.
    """
    from analysis import indicators as ind
    from analysis.confluence import thin_volume_veto, volume_vote

    out = []
    states = await runner.crypto_store.get_all() if runner else []
    for st in states:
        volumes = [c.volume for c in st.candles_1m]
        highs = [c.high for c in st.candles_1m]
        lows = [c.low for c in st.candles_1m]
        closes = [c.close for c in st.candles_1m]
        usable = ind.has_usable_volume(volumes)
        vote = volume_vote(highs, lows, closes, volumes) if closes else None
        out.append({
            "symbol": st.symbol,
            "bars": len(volumes),
            "per_bar_volume_usable": usable,
            "relative_volume": ind.relative_volume(volumes),
            "volume_trend": ind.volume_trend(closes, volumes) if closes else None,
            "volume_24h": st.volume_24h,
            "vote_direction": vote.direction if vote else None,
            "vote_weight": round(vote.weight, 3) if vote else None,
            "vote_reason": vote.reason if vote else "",
            "thin_tape_veto": thin_volume_veto(volumes),
        })

    probe = None
    symbol = request.query.get("symbol")
    if symbol and runner is not None:
        got = await runner.coindcx.fetch_candles_raw(symbol, limit=3)
        if got:
            pair, payload = got
            probe = {"pair": pair, "rows": payload[:3] if isinstance(payload, list) else payload}
        else:
            probe = {"error": "no candle pair answered",
                     "tried": list(runner.coindcx._pair_variants(symbol))}

    return _json_response({
        "note": ("A symbol with per_bar_volume_usable=false has no volume "
                 "information at all — the volume family abstains, so only "
                 "four families can vote and signals are correspondingly "
                 "rarer. Pass ?symbol=btcusdt to probe the candle endpoint."),
        "symbols": out,
        "candle_probe": probe,
    })


async def _api_audit(runner, request: web.Request) -> web.Response:
    """
    Every fired signal in a window, scored, plus the slices that explain it.

    Separate from /api/signals/accuracy on purpose: that endpoint answers
    "is the confidence number calibrated". This one answers "which predictions
    succeeded, which failed, and what do the failures have in common" — and it
    nets the round-trip cost off every figure, because the gross column is the
    one that made a leaking system look profitable.
    """
    from analysis.signal_audit import audit
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    days = max(1, min(365, int(request.query.get("days") or 30)))
    async with AsyncSessionFactory() as session:
        rows = await Repository(session).crypto_signals_between(days, limit=2000)

    report = audit(rows)
    report["days"] = days
    report["cost_model"] = {
        "round_trip_pct": round(_SCALP.cost_floor_pct * 100, 4),
        "min_target_pct": round(_SCALP.min_target_pct * 100, 4),
        "min_edge_multiple": _SCALP.min_edge_multiple,
    }
    return _json_response(report)


async def _api_reviews(runner, request: web.Request) -> web.Response:
    """
    GET /api/reviews?days=7&phase=pre&limit=300 — every AI review with what it
    saw and said. phase: pre (before a trade), hold (open trade), post (after).
    """
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    try:
        days = max(1, min(int(request.query.get("days", "7")), 90))
        limit = max(1, min(int(request.query.get("limit", "300")), 2000))
    except ValueError:
        days, limit = 7, 300
    phase = (request.query.get("phase") or "").strip().lower()
    if phase not in ("", "pre", "hold", "post"):
        phase = ""
    async with AsyncSessionFactory() as session:
        rows = await Repository(session).recent_reviews(days, phase, limit)
    body = [{
        "at": _iso(r.created_at), "phase": r.phase, "symbol": r.symbol,
        "signal_type": r.signal_type, "verdict": r.verdict, "factors": r.factors,
        "summary": r.summary, "confidence_delta": r.confidence_delta,
        "outcome": r.outcome, "pnl_pct": r.pnl_pct, "model": r.model,
        "latency_ms": r.latency_ms, "sentiment_score": getattr(r, "sentiment_score", 0.0),
        "fear_greed": getattr(r, "fear_greed", 0), "news_context": getattr(r, "news_context", ""),
    } for r in rows]
    return web.Response(text=json.dumps({"count": len(body), "reviews": body}),
                        content_type="application/json")


async def _api_reviewer_scorecard(runner, request: web.Request) -> web.Response:
    """
    GET /api/audit/reviewer — was the AI reviewer actually right?

    Graded against resolved outcomes, including the signals it suppressed,
    which is the only way the question has an honest answer. If REJECT shows
    a better win rate than APPROVE, the reviewer is costing money.
    """
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    days = max(1, min(90, int(request.query.get("days", 14))))
    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        return _json_response({
            "days": days,
            "scorecard": await repo.reviewer_scorecard(days),
            "post_trade_factors": await repo.review_factor_counts("post", days),
        })


async def _api_audit_methods(runner, request: web.Request) -> web.Response:
    """
    The code that produces a signal, read out of the modules themselves.

    Introspected rather than transcribed, so the page cannot describe a
    function that no longer exists or miss one that was added.
    """
    from analysis.signal_audit import method_catalogue

    return _json_response({"stages": method_catalogue()})


async def _api_research(runner, request: web.Request) -> web.Response:
    """
    The measured state of the edge, and what the model made of it.

    Served from the last weekly pass rather than recomputed: the measurement
    walks every labelled snapshot in the retention window, which is not work
    to do on a page load. `?fresh=1` forces it, for when you have just
    changed something and do not want to wait a week to see it.
    """
    from analysis.research_report import load_and_analyse, render
    from storage.database import AsyncSessionFactory

    if request.query.get("fresh") == "1":
        async with AsyncSessionFactory() as session:
            found = await load_and_analyse(session, days=_SETTINGS.snapshot_retention_days)
        return _json_response({
            "report": render(found), "rows": found.rows, "rejected": found.rejected,
            "span_days": found.span_days, "cost_pct": found.cost_pct,
            "hypotheses": {}, "generated": "just now",
        })

    return _json_response({
        "report": getattr(runner, "last_research_text", "")
                  or "No research pass has run yet. It runs weekly, and needs "
                     "labelled snapshots — labels are written 30 minutes to a "
                     "day after each snapshot. Add ?fresh=1 to measure now.",
        "hypotheses": getattr(runner, "last_research", {}) or {},
    })


async def _api_sentiment_ingest(runner, request: web.Request) -> web.Response:
    """
    Accept scored headlines from an external analyser (Hermes on a laptop).

    Push rather than pull, because the analyser runs behind a home NAT that
    this server cannot reach. Authenticated with a shared secret compared in
    constant time — a plain == leaks the secret one character at a time to
    anyone willing to measure.

    Header only. There used to be a ?token= fallback for curl convenience,
    which put the secret into the access log, the shell history and any
    Referer the browser felt like sending — three places it then sits in
    plaintext forever. A header costs one more flag and leaks none of that.

    Body: {"items": [{external_id, symbol, headline, score, confidence,
                      event_type, source, url, published_at, model}, ...]}
    """
    import hmac

    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    secret = _SETTINGS.sentiment_ingest_token
    if not secret:
        return _json_response(
            {"error": "ingest disabled", "hint": "set SENTIMENT_INGEST_TOKEN"}, status=503)

    supplied = request.headers.get("X-Ingest-Token") or ""
    if not hmac.compare_digest(supplied, secret):
        return _json_response({"error": "unauthorised"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "body must be JSON"}, status=400)

    items = body.get("items")
    if not isinstance(items, list):
        return _json_response({"error": "expected an 'items' list"}, status=400)
    if len(items) > 500:
        return _json_response({"error": "at most 500 items per batch"}, status=413)

    async with AsyncSessionFactory() as session:
        accepted, duplicates = await Repository(session).ingest_news_sentiment(items)
    return _json_response({"accepted": accepted, "duplicates": duplicates,
                              "received": len(items)})


async def _api_sentiment_recent(runner, request: web.Request) -> web.Response:
    """What the analyser has sent lately, so a score can be traced to a headline."""
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    symbol = (request.query.get("symbol") or "all").lower()
    hours = max(1, min(72, int(request.query.get("hours") or 6)))
    async with AsyncSessionFactory() as session:
        rows = await Repository(session).recent_news_sentiment(symbol, hours)
    return _json_response([{
        "symbol": r.symbol.upper(), "headline": r.headline, "source": r.source,
        "score": round(r.score, 3), "confidence": round(r.confidence, 3),
        "event_type": r.event_type, "model": r.model, "url": r.url,
        "published_at": _iso(r.published_at),
    } for r in rows])


async def _api_debug_coindcx(runner, request: web.Request) -> web.Response:
    """
    Show exactly what CoinDCX returns for a symbol, spot and futures.

    The futures response shape is not publicly documented and could not be
    reached from the environment the collector was written in, so this exists
    to close that loop from the running server rather than by guessing.
    Add ?symbol=xauusdt to target one.
    """
    import httpx

    from collectors.coindcx import _base_symbol, _extract_price, _futures_name_variants

    symbol = (request.query.get("symbol") or "xauusdt").strip().lower()
    base = _base_symbol(symbol).upper()
    out: dict = {"symbol": symbol, "base": base}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(runner.coindcx.TICKER_URL)
        out["spot"] = {"status": r.status_code}
        if r.status_code == 200:
            rows = r.json()
            markets = {str(x.get("market", "")).upper() for x in rows}
            out["spot"]["total_markets"] = len(markets)
            out["spot"]["matching"] = sorted(m for m in markets if base in m)[:20]
    except Exception as exc:
        out["spot"] = {"error": f"{type(exc).__name__}: {exc}"}

    try:
        got = await runner.coindcx.fetch_futures_raw()
        if not got:
            out["futures"] = {"error": "no futures endpoint answered"}
        else:
            url, payload = got
            table = payload
            if isinstance(payload, dict):
                for wrapper in ("prices", "data", "result"):
                    if isinstance(payload.get(wrapper), dict):
                        table = payload[wrapper]
                        break
            out["futures"] = {"url": url, "shape": type(table).__name__}
            if isinstance(table, dict):
                keys = [str(k) for k in table]
                out["futures"]["total_instruments"] = len(keys)
                hits = [k for k in keys if base in k.upper()][:20]
                out["futures"]["matching"] = hits
                # One full sample so the price field can be identified.
                if hits:
                    out["futures"]["sample"] = {hits[0]: table[hits[0]]}
                elif keys:
                    out["futures"]["sample_any"] = {keys[0]: table[keys[0]]}
                out["futures"]["variants_tried"] = list(_futures_name_variants(base))
                out["futures"]["resolved_price"] = next(
                    (_extract_price(table.get(v)) for v in _futures_name_variants(base)
                     if _extract_price(table.get(v)) is not None), None)
            else:
                out["futures"]["raw"] = str(payload)[:1000]
    except Exception as exc:
        out["futures"] = {"error": f"{type(exc).__name__}: {exc}"}

    return web.Response(text=json.dumps(out, indent=2, default=str),
                        content_type="application/json")


async def _paper_db_snapshot() -> dict:
    """
    The part of /api/paper that only changes when a trade opens, closes, or
    the tick runs — cached for a few seconds. Mark price and everything
    computed from it is never in here: that has to be the live price on
    every single request, cache or not, or "unrealised P&L" would be
    lying by however far the market moved since the cache was filled.
    """
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    async def fetch():
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            # running_cycles() already holds get_running_cycle()'s answer:
            # both order by id, one just also returns any duplicates. Asking
            # twice paid this database's ~1.4-1.5s network floor a second
            # time on every /api/paper request that had a cycle running,
            # which was most of it (production's p50/p95 for this endpoint
            # tracked almost exactly N x that floor for N round trips).
            running = await repo.running_cycles()
            cycle = running[-1] if running else None
            if cycle is None:
                return {"cycle": None, "recent": await repo.get_recent_cycles(limit=5)}
            rows = await repo.get_open_positions(cycle.id)
            trades = await repo.get_cycle_trades(cycle.id, limit=200)
            return {"cycle": cycle, "rows": rows, "trades": trades, "running": running}

    from scheduler import cache
    return await cache.cached("paper_db_snapshot", 4.0, fetch)


async def _api_swing(runner, request: web.Request) -> web.Response:
    """
    The swing book against its own backtest: what each coin's last closed 4h bar said, open swing
    positions, and closed swing trades in R next to what five years of testing expect.
    """
    snap = await _paper_db_snapshot()
    cycle = snap["cycle"]
    rows = [r for r in (snap.get("rows") or []) if getattr(r, "trade_mode", "") == "swing"]
    trades = [t for t in (snap.get("trades") or []) if str(getattr(t, "signal_type", "")).startswith("swing_")]

    from analysis.strategy_registry import trade_r as r_of

    rs = [r_of(t) for t in trades]
    by_strategy = {}
    for t, r in zip(trades, rs):
        b = by_strategy.setdefault(t.signal_type.replace("swing_", ""), {"n": 0, "wins": 0, "sum_r": 0.0})
        b["n"] += 1
        b["wins"] += int(r > 0)
        b["sum_r"] += r
    return _json_response({
        "enabled": _SETTINGS.swing_enabled,
        "rules": {"strategies": _SETTINGS.swing_strategies, "timeframes": "4h and 8h", "stop": "3 x ATR(14) of the signal bars",
                  "target": "3R", "time_limit_minutes": _SETTINGS.swing_hold_minutes,
                  "risk_per_trade": _SETTINGS.swing_risk_pct, "adaptive_risk": _SETTINGS.swing_adaptive_risk,
                  "max_open": _SETTINGS.swing_max_open, "max_leverage": _SETTINGS.swing_max_leverage,
                  "max_same_direction": _SETTINGS.swing_max_same_side, "excluded_coins": _SETTINGS.swing_exclude_symbols,
                  "signal_order": "strongest strategy and coin first (5-year backtest R per trade)",
                  "families_that_open_trades": _SETTINGS.paper_open_families},
        "expected_from_backtest": {"r_per_trade": "+0.14 to +0.28 net", "win_rate": "42-45%",
                                   "losing_streaks": "8-12 trades happen", "trades_per_day_all_coins": 2.2,
                                   "judge_after_trades": 30},
        "scan": getattr(runner, "_swing_last", {}),
        "open": [{"symbol": r.symbol, "side": r.side, "strategy": r.signal_type, "entry": r.entry_price,
                  "stop": r.stop_price, "target": r.target_price, "leverage": r.leverage, "margin": r.margin,
                  "opened_at": r.opened_at.isoformat() if r.opened_at else None,
                  "expires_at": r.expires_at.isoformat() if r.expires_at else None} for r in rows],
        "closed": {"n": len(trades), "wins": sum(1 for r in rs if r > 0),
                   "win_rate": (sum(1 for r in rs if r > 0) / len(rs)) if rs else None,
                   "avg_r": (sum(rs) / len(rs)) if rs else None, "total_r": sum(rs),
                   "net_pnl": sum(t.net_pnl for t in trades), "by_strategy": by_strategy,
                   "last": [{"symbol": t.symbol, "strategy": t.signal_type, "side": t.side, "r": round(r, 2),
                             "exit_reason": t.exit_reason, "net_pnl": round(t.net_pnl, 2),
                             "closed_at": t.closed_at.isoformat() if t.closed_at else None}
                            for t, r in list(zip(trades, rs))[-15:]]},
        "wallet": cycle.wallet if cycle is not None else None,
        "regime_filter": await _swing_regime_section(runner, trades, rs),
        "registry": await _swing_registry_section(trades),
    })


async def _swing_registry_section(trades) -> list[dict]:
    """Plan 2.3: per spec, the forward clock (live_from) and forward results next to its backtest."""
    from analysis import strategy_registry as reg
    from analysis.swing_book import STRATEGY_R
    try:
        from storage.database import AsyncSessionFactory
        async with AsyncSessionFactory() as session:
            rows = await reg.load(session)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for key, row in sorted(rows.items()):
        rs = reg.forward(trades, row)
        tf, sid = key.split("@", 1)
        out.append({"spec": key, "params": row.params, "status": row.status, "note": row.note,
                    "live_from": row.live_from.isoformat() if row.live_from else None,
                    "forward_trades": len(rs), "forward_total_r": round(sum(rs), 2),
                    "forward_avg_r": round(sum(rs) / len(rs), 3) if rs else None,
                    "forward_drawdown_r": round(reg.drawdown_r(rs), 2),
                    "alarm_at_r": reg.alarm_limit(key, _SETTINGS.swing_dd_alarm_factor),
                    "backtest_avg_r": STRATEGY_R.get((sid, tf))})
    return out


async def _api_llm_budget(runner, request: web.Request) -> web.Response:
    """Every AI model's use today against its free-tier limits, cooldowns after rate limits, and skipped calls."""
    from collectors.llm_budget import SAFETY, budget
    from collectors.llm_client import calls_today
    return _json_response({"safety_share": SAFETY, "models": budget().snapshot(), "calls_by_role_today": calls_today()})


async def _swing_regime_section(runner, trades, rs) -> dict:
    """Shadow test of analysis.regime_gate: closed swing trades split by the verdict their signal carried."""
    from analysis import regime_gate
    out = {"mode": _SETTINGS.swing_regime_filter, "vol_rank_max": _SETTINGS.swing_regime_vol_rank_max,
           "adx_max": _SETTINGS.swing_regime_adx_max, "now": getattr(runner, "_regime_now", {}) or {}}
    try:
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            rows = await Repository(session).swing_signals_since(120)
        sigs = [{"symbol": r.symbol, "signal_type": r.signal_type, "direction": r.direction,
                 "timestamp": r.timestamp if r.timestamp.tzinfo else r.timestamp.replace(tzinfo=UTC),
                 "indicators_summary": r.indicators_summary or ""}
                for r in rows if getattr(r, "trade_mode", "") == "swing"]
        trs = [{"symbol": t.symbol, "signal_type": t.signal_type, "side": t.side, "r": r,
                "opened_at": t.opened_at if t.opened_at.tzinfo else t.opened_at.replace(tzinfo=UTC)}
               for t, r in zip(trades, rs) if t.opened_at is not None]
        out["scoreboard"] = regime_gate.shadow_scoreboard(sigs, trs)
        out["signals"] = regime_gate.signal_scoreboard([
            {"indicators_summary": r.indicators_summary or "", "outcome": r.outcome, "pnl_pct": r.pnl_pct,
             "current_price": r.current_price, "stop_loss": r.stop_loss, "skip_reason": r.skip_reason}
            for r in rows if getattr(r, "trade_mode", "") == "swing"])
    except Exception as e:  # noqa: BLE001 - the swing monitor must render even if this part fails
        out["error"] = str(e)[:200]
    return out


async def _paper_withdrawals(cycle_id: int) -> list[dict]:
    """Profit taken out of this cycle (owner plan: withdraw Rs2,500 each time equity reaches Rs10,000)."""
    try:
        from sqlalchemy import select

        from storage.database import AsyncSessionFactory
        from storage.models import PaperWithdrawal
        async with AsyncSessionFactory() as session:
            rows = (await session.execute(select(PaperWithdrawal).where(PaperWithdrawal.cycle_id == cycle_id)
                                          .order_by(PaperWithdrawal.id))).scalars().all()
        return [{"amount": r.amount, "equity_before": round(r.equity_before, 2), "wallet_after": round(r.wallet_after, 2),
                 "at": r.created_at.isoformat() if r.created_at else None} for r in rows]
    except Exception:  # noqa: BLE001 - the page must load even if this table is unreachable
        return []


async def _api_paper(runner, request: web.Request) -> web.Response:
    """
    Live state of the paper-trading cycle: wallet, open positions, trade log.

    Unrealised P&L on open positions is marked against the current price and
    reported net of the exit fee not yet paid — showing gross there would make
    every position look better than closing it would actually be.
    """
    from analysis.paper_cycle import config_for_cycle, fees_for, summarise

    snap = await _paper_db_snapshot()
    cycle = snap["cycle"]
    if cycle is None:
        return web.Response(
            text=json.dumps({
                "running": False,
                "enabled": _SETTINGS.paper_trading_enabled,
                "past_cycles": [_cycle_row(c) for c in snap["recent"]],
            }),
            content_type="application/json")

    # More than one cycle claiming to be running means trades are being
    # split across them: the page reads the newest and a trade announced
    # on Telegram can be missing here with nothing to explain it.
    rows, trades, running = snap["rows"], snap["trades"], snap["running"]
    cfg = config_for_cycle(cycle)

    states = {st.symbol: st for st in await runner.crypto_store.get_all()}
    positions = []
    unrealised_total = 0.0
    for r in rows:
        st = states.get(r.symbol)
        mark = st.current_price if st and st.current_price > 0 else r.entry_price
        sign = 1.0 if r.side == "long" else -1.0
        gross = sign * (mark - r.entry_price) * r.coin_qty * r.usdt_inr
        exit_fee = mark * r.coin_qty * r.usdt_inr * fees_for(r.symbol).effective_taker_pct
        net = gross - exit_fee
        unrealised_total += net
        positions.append({
            "symbol": r.symbol.upper(),
            "side": r.side,
            "qty": r.coin_qty,
            "entry": r.entry_price,
            "mark": mark,
            "margin": round(r.margin, 2),
            "stop": r.stop_price,
            "target": (None if (r.target_price is None or math.isinf(r.target_price)) else r.target_price),
            "liq": r.liq_price,
            "trailing": r.trail_active,
            "confidence": round(r.confidence * 100),
            "signal_type": r.signal_type,
            "unrealised": round(net, 2),
            "roe_pct": round(net / r.margin * 100, 2) if r.margin else 0.0,
            "opened_at": _iso(r.opened_at),
            # When this position times out. Exposed because a position that
            # outlives its own expiry is the visible symptom of the resolver
            # not reaching it, and that is invisible without this field.
            "expires_at": _iso(r.expires_at),
            "usdt_inr": r.usdt_inr,
            "notional": round(r.coin_qty * mark * r.usdt_inr, 2),
            "trade_mode": getattr(r, "trade_mode", "intraday"),
            "leverage": getattr(r, "leverage", 10.0),
            "tp1_price": getattr(r, "tp1_price", 0.0),
            "tp2_price": getattr(r, "tp2_price", 0.0),
            "partial_closed": getattr(r, "partial_closed", False),
            "partial_pnl": round(getattr(r, "partial_pnl", 0.0), 2),
        })

    withdrawals = await _paper_withdrawals(cycle.id)
    return web.Response(text=json.dumps({
        "running": True,
        "enabled": _SETTINGS.paper_trading_enabled,
        "cycle": _cycle_row(cycle),
        "withdrawals": {"total": round(sum(w["amount"] for w in withdrawals), 2), "count": len(withdrawals),
                        "rule": {"at": _SETTINGS.paper_sweep_at, "amount": _SETTINGS.paper_sweep_amount},
                        "last": withdrawals[-10:]},
        "equity": round(cycle.wallet + sum(p["margin"] for p in positions)
                        + unrealised_total, 2),
        "unrealised": round(unrealised_total, 2),
        "positions": positions,
        "summary": summarise(trades, cycle.wallet, cfg),
        "trades": [_trade_row(t) for t in trades[:60]],
        # Current rate, for figures that are not tied to one trade.
        "usdt_inr": _SETTINGS.paper_usdt_inr,
        "running_cycles": [c.id for c in running],
    }), content_type="application/json")


def _cycle_row(c) -> dict:
    return {
        "id": c.id,
        "status": c.status,
        "wallet": round(c.wallet, 2),
        "starting_wallet": round(c.starting_wallet, 2),
        "target_wallet": round(c.target_wallet, 2),
        "peak_wallet": round(c.peak_wallet, 2),
        "leverage": c.leverage,
        "stop_pct_of_margin": c.stop_pct_of_margin,
        "reward_risk": c.reward_risk,
        "min_confidence": c.min_confidence,
        "trailing_enabled": c.trailing_enabled,
        "scaled_sizing": c.scaled_sizing,
        "started_at": _iso(c.started_at),
        "ended_at": _iso(c.ended_at),
    }


async def _api_paper_events(runner, request: web.Request) -> web.Response:
    """GET /api/paper/events?symbol=SOLUSDT&opened_at=ISO — one trade's change log."""
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    sym = request.query.get("symbol", "").lower()
    raw = request.query.get("opened_at", "")
    try:
        opened = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        opened = opened.astimezone(UTC).replace(tzinfo=None) if opened.tzinfo else opened
    except ValueError:
        return _json_response({"error": "opened_at must be ISO"}, status=400)
    async with AsyncSessionFactory() as session:
        rows = await Repository(session).trade_events(sym, opened)
    return _json_response({"events": [{
        "at": _iso(e.at), "kind": e.kind, "field": e.field, "old": e.old, "new": e.new,
        "note": e.note} for e in rows]})


def _trade_row(t) -> dict:
    return {
        "symbol": t.symbol.upper(),
        "side": t.side,
        "entry": t.entry_price,
        "exit": t.exit_price,
        "margin": round(t.margin, 2),
        "reason": t.exit_reason,
        "gross": round(t.gross_pnl, 2),
        "fees": round(t.trading_fees, 2),
        "funding": round(t.funding_paid, 2),
        "net": round(t.net_pnl, 2),
        "roe_pct": round(t.return_on_margin * 100, 2),
        "wallet_after": round(t.wallet_after, 2),
        "confidence": round(t.confidence * 100),
        "signal_type": t.signal_type,
        "hours_held": round(t.hours_held, 2),
        "closed_at": _iso(t.closed_at),
        "opened_at": _iso(t.opened_at),
        "qty": t.coin_qty,
        "trade_mode": getattr(t, "trade_mode", "intraday"),
        "partial_pnl": round(getattr(t, "partial_pnl", 0.0) or 0.0, 2),
        # Money on this row is INR at the rate the trade was booked at, not
        # today's. Sent so the page converts instead of assuming.
        "usdt_inr": getattr(t, "usdt_inr", 0.0) or 102.0,
    }


async def _api_crypto_forecasts(runner, request: web.Request) -> web.Response:
    """Return multi-horizon forecasts (30m, 1h, 4h, 1d) for all watchlist symbols."""
    states = await runner.crypto_store.get_all()
    forecasts = {}
    for s in states:
        if s.current_price > 0:
            fc = runner.multi_horizon.predict_all_horizons(s)
            forecasts[s.symbol.upper()] = {
                h: {
                    "direction": f.direction,
                    "prob_up": f.probability_up,
                    "pred_change_pct": f.predicted_change_pct,
                    "confidence": f.confidence,
                    "target_price": f.target_price,
                    "support_price": f.support_price,
                    "drivers": f.key_drivers,
                }
                for h, f in fc.items()
            }
    return web.Response(text=json.dumps(forecasts), content_type="application/json")


async def _api_crypto_watchlist_add(runner, request: web.Request) -> web.Response:
    """POST /api/crypto/watchlist/add  body: {"symbol": "dogeusdt"}"""
    from scheduler.security import check_bearer_auth
    is_admin = await _verify_admin_session(request)
    if not is_admin and _SETTINGS.api_auth_token:
        denied = check_bearer_auth(request, _SETTINGS.api_auth_token)
        if denied is not None:
            return denied
    try:
        body = await request.json()
        symbol = str(body.get("symbol", "")).strip().lower()
        if not symbol:
            return web.Response(text=json.dumps({"error": "symbol required"}),
                                 content_type="application/json", status=400)
        # The watchlist is the only list the engine reads, so a pair Binance
        # does not list is a symbol that silently never gets data. Refused
        # when Binance's list is known; allowed with a warning when it could
        # not be fetched, because "unreachable" is not "unlisted".
        from collectors.binance_symbols import is_listed
        listed = await is_listed(symbol)
        if listed is False:
            return web.Response(text=json.dumps({
                "error": f"{symbol.upper()} is not a Binance spot USDT pair (for gold, use PAXGUSDT). "
                         "Search on the Watchlist page and add the exact pair."}),
                content_type="application/json", status=400)
        await runner.add_crypto_symbol(symbol)
        body = {"symbol": symbol, "ok": True}
        if listed is None:
            body["warning"] = "Could not reach Binance to confirm this pair is listed."
        return web.Response(text=json.dumps(body), content_type="application/json")
    except Exception as exc:
        return web.Response(text=json.dumps({"error": str(exc)}),
                             content_type="application/json", status=500)


async def _api_crypto_watchlist(runner, request: web.Request) -> web.Response:
    """
    GET /api/crypto/watchlist — the symbols the engine is following.

    Read-only and public like the other market endpoints. The news scorer on
    the laptop reads it so it attributes headlines to watchlist coins only,
    instead of carrying its own list that drifts from this one.
    """
    symbols = sorted(await runner.crypto_store.get_symbols())
    return web.Response(text=json.dumps({"symbols": symbols}),
                        content_type="application/json")


async def _api_moves(runner, request: web.Request) -> web.Response:
    """
    GET /api/moves?limit=24 — the hourly "why did it move" analyses, newest first,
    with the latest world briefing and the next scheduled releases.
    """
    from analysis.event_calendar import upcoming
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    try:
        limit = max(1, min(int(request.query.get("limit", "24")), 200))
    except ValueError:
        limit = 24
    async with AsyncSessionFactory() as session:
        repo = Repository(session)
        rows = await repo.recent_move_attributions(limit)
        briefing = await repo.latest_briefing()
        events = await repo.recent_events(14)
        shadow = await repo.shadow_trades(30)
    now = datetime.now(UTC).replace(tzinfo=None)
    body = {
        "runs": [{
            "id": r.id, "at": _iso(r.created_at), "window_hours": r.window_hours,
            "model": r.model, "latency_ms": r.latency_ms,
            "moves": json.loads(r.moves or "[]"), "signals": json.loads(r.signals or "[]"),
            "result": json.loads(r.result or "{}"),
        } for r in rows],
        "briefing": None if briefing is None else {
            "at": _iso(briefing.created_at), "risk_tone": briefing.risk_tone,
            "summary": briefing.summary, "events": json.loads(briefing.events or "[]"),
            "model": briefing.model,
        },
        "upcoming": [{"at": _iso(e.at), "kind": e.kind, "name": e.name}
                     for e in upcoming(now, days=7)],
        "monitor": {
            "last_run": _iso(getattr(runner, "_monitor_last_run", None)),
            "failures": list(getattr(runner, "_monitor_failures", []) or []),
        },
        "events": [{
            "id": e.id, "title": e.title, "category": e.category, "at": _iso(e.happened_at),
            "level": e.level_current, "level_initial": e.level_initial,
            "level_confirmed": e.level_confirmed, "btc_move_2h_pct": e.btc_move_2h_pct,
            "direction": e.direction, "status": e.status, "source": e.source,
        } for e in events],
        "shadow": [{
            "event_id": s.event_id, "book": s.book, "symbol": s.symbol, "side": s.side,
            "wallet_pct": s.wallet_pct, "note": s.note, "at": _iso(s.created_at),
        } for s in shadow],
    }
    return web.Response(text=json.dumps(body), content_type="application/json")


async def _api_moves_run(runner, request: web.Request) -> web.Response:
    """POST /api/moves/run — run the analysis now instead of waiting for the hour."""
    import asyncio

    from scheduler.security import check_bearer_auth
    if not await _verify_admin_session(request):
        denied = check_bearer_auth(request, _SETTINGS.api_auth_token)
        if denied is not None:
            return denied
    if not hasattr(runner, "_move_attribution_job"):
        return web.Response(text=json.dumps({"error": "not available"}),
                            content_type="application/json", status=503)
    asyncio.create_task(runner._move_attribution_job())
    return web.Response(text=json.dumps({"ok": True, "started": True}),
                        content_type="application/json")


async def _moves_page(request: web.Request) -> web.Response:
    return web.Response(text=_MOVES_HTML, content_type="text/html")


async def _api_events(runner, request: web.Request) -> web.Response:
    """
    GET /api/events — is trading paused right now, and what is coming up.

    Scheduled releases (FOMC, CPI, jobs, PCE, PPI) for the next two weeks,
    and the event currently holding new trades back, if any.
    """
    from analysis.event_calendar import WINDOWS, upcoming

    now = datetime.now(UTC)
    naive = now.replace(tzinfo=None)
    current = runner._blackout(now) if hasattr(runner, "_blackout") else None

    def row(ev):
        before, after = WINDOWS.get(ev.kind, (0, 0))
        return {"at": _iso(ev.at), "kind": ev.kind, "name": ev.name,
                "pause_from": _iso(ev.at - timedelta(minutes=before)),
                "pause_until": _iso(ev.at + timedelta(minutes=after))}

    body = {"blackout": row(current) if current else None,
            "upcoming": [row(e) for e in upcoming(naive)]}
    return web.Response(text=json.dumps(body), content_type="application/json")


async def _api_binance_symbols(runner, request: web.Request) -> web.Response:
    """
    GET /api/binance/symbols?q=gold — search the pairs Binance actually lists.

    Returns price, 24h change, 24h volume and whether a perpetual exists, so
    a pair can be judged before it goes on the watchlist.
    """
    from collectors.binance_symbols import search

    q = (request.query.get("q") or "").strip()[:40]
    watchlist = set(await runner.crypto_store.get_symbols())
    payload = await search(q, watchlist)
    return web.Response(text=json.dumps(payload), content_type="application/json")


async def _api_crypto_watchlist_remove(runner, request: web.Request) -> web.Response:
    """POST /api/crypto/watchlist/remove  body: {"symbol": "dogeusdt"}"""
    from scheduler.security import check_bearer_auth
    is_admin = await _verify_admin_session(request)
    if not is_admin and _SETTINGS.api_auth_token:
        denied = check_bearer_auth(request, _SETTINGS.api_auth_token)
        if denied is not None:
            return denied
    try:
        body = await request.json()
        symbol = str(body.get("symbol", "")).strip().lower()
        if not symbol:
            return web.Response(text=json.dumps({"error": "symbol required"}),
                                 content_type="application/json", status=400)
        await runner.remove_crypto_symbol(symbol)
        return web.Response(text=json.dumps({"symbol": symbol, "ok": True}),
                             content_type="application/json")
    except Exception as exc:
        return web.Response(text=json.dumps({"error": str(exc)}),
                             content_type="application/json", status=500)


async def _api_binance_probe(runner, request: web.Request) -> web.Response:
    """
    GET /api/debug/binance — actually try to open a Binance WebSocket from THIS
    server and report what each candidate host returns.

    Binance's main host geo-blocks most US cloud IPs with HTTP 451, which no
    amount of client-side retrying can fix. Rather than guess which hosts work
    from Render, this probes them live and tells you.
    """
    import asyncio as _asyncio

    import websockets

    from collectors.binance_ws import BINANCE_WS_HOSTS, is_geoblocked

    results = []
    for host in BINANCE_WS_HOSTS:
        url = f"{host}?streams=btcusdt@kline_1m"
        entry = {"host": host}
        try:
            async with websockets.connect(url, open_timeout=8, close_timeout=3) as ws:
                msg = await _asyncio.wait_for(ws.recv(), timeout=8)
                entry.update({
                    "ok": True,
                    "verdict": "WORKS — real OHLC klines available from this server",
                    "sample_bytes": len(msg),
                })
        except Exception as exc:
            entry.update({
                "ok": False,
                "geoblocked": is_geoblocked(exc),
                "error": str(exc)[:200],
                "verdict": (
                    "GEO-BLOCKED (HTTP 451) — this server's IP is not allowed; retrying cannot help"
                    if is_geoblocked(exc)
                    else "unreachable/other error"
                ),
            })
        results.append(entry)

    any_ok = any(r.get("ok") for r in results)
    return web.Response(
        text=json.dumps({
            "any_host_reachable": any_ok,
            "recommendation": (
                "Set BINANCE_WS_ENABLED=true — a working host was found, and Binance klines "
                "carry true OHLC which makes ATR (and signal targets) far more realistic."
                if any_ok else
                "Leave Binance off. Every host is blocked from this server's IP. "
                "Use CoinDCX/CoinGecko, or redeploy in a non-blocked region."
            ),
            "hosts": results,
        }, indent=2),
        content_type="application/json",
    )


async def _api_commodities(runner, request: web.Request) -> web.Response:
    """Return real-time spot commodities (Gold, Silver, Oil)."""
    states = await runner.commodity_store.get_all()
    comms = [
        {
            "symbol": s.symbol,
            "name": s.name,
            "price": s.current_price,
            "change_24h_pct": round(s.price_change_24h_pct, 2),
            "rsi_14": s.rsi_14,
            "timestamp": _iso(s.timestamp),
        }
        for s in states
    ]
    return web.Response(text=json.dumps(comms), content_type="application/json")


async def _api_debug(runner, request: web.Request) -> web.Response:
    """Diagnostic endpoint — per-symbol feed state and collector status."""
    states = await runner.crypto_store.get_all()
    uptime = int((datetime.now(UTC) - _start_time).total_seconds())
    return web.Response(
        text=json.dumps({
            "uptime_seconds": uptime,
            "symbols": [
                {
                    "symbol": s.symbol,
                    "price": s.current_price,
                    "candles_1m": len(s.candles_1m),
                    "atr_14": s.atr_14,
                    "rsi_14": s.rsi_14,
                    "kline_status": runner.klines.status.get(s.symbol, "not fetched"),
                }
                for s in states
            ],
            "klines_host": runner.klines.host,
            "klines_last_error": runner.klines.last_error,
            "collector_status": runner.get_status(),
        }),
        content_type="application/json",
    )


async def _health(runner, request: web.Request) -> web.Response:
    # Counts priced symbols, not watchlist size: the systemd/uptime check
    # should go red when every feed is down, not merely when the DB row count
    # happens to be non-zero.
    states = await runner.crypto_store.get_all()
    priced = sum(1 for s in states if s.current_price > 0)
    uptime = int((datetime.now(UTC) - _start_time).total_seconds())
    return web.Response(
        text=json.dumps({"status": "ok", "symbols_tracked": len(states),
                         "symbols_priced": priced, "uptime_seconds": uptime}),
        content_type="application/json",
    )


# ── Dashboard HTML ────────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Signal Engine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
header{background:#1e293b;border-bottom:1px solid #334155;padding:14px 20px;display:flex;align-items:center;justify-content:space-between}
header h1{font-size:18px;font-weight:700;color:#f1f5f9;display:flex;align-items:center;gap:8px}
.badge{background:#0ea5e9;color:#fff;font-size:11px;padding:2px 8px;border-radius:9999px;font-weight:600}
.refresh{font-size:12px;color:#64748b}
.nav-btn{margin-left:12px;background:#1e293b;color:#94a3b8;font-size:13px;font-weight:600;padding:6px 14px;border-radius:8px;text-decoration:none;white-space:nowrap;border:1px solid #334155}
.nav-btn:hover{background:#334155;color:#e2e8f0}
.nav-btn.active{background:#0ea5e9;color:#fff;border-color:#0ea5e9}
.nav-btn.active:hover{background:#0284c7;color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;padding:16px 20px 0}
.card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px}
.card-title{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:#64748b;margin-bottom:6px}
.card-value{font-size:26px;font-weight:700;color:#f1f5f9}
.card-sub{font-size:11px;color:#94a3b8;margin-top:3px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.dot-green{background:#22c55e}.dot-red{background:#ef4444}.dot-yellow{background:#f59e0b}.dot-gray{background:#475569}
section{padding:16px 20px}
section h2{font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px}
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.status-card{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:10px}
.status-name{font-size:11px;font-weight:600;color:#94a3b8;margin-bottom:4px}
.status-val{font-size:12px;color:#f1f5f9}
footer{text-align:center;padding:16px;color:#334155;font-size:11px;border-top:1px solid #1e293b;margin-top:4px}
.empty{color:#475569;font-size:13px;padding:20px 0;text-align:center}

/* ── Tab bar ── */
.tab-bar{display:flex;gap:0;padding:0 20px;background:#1e293b;border-bottom:2px solid #0f172a}
.tab-btn{background:none;border:none;border-bottom:3px solid transparent;color:#64748b;font-size:13px;font-weight:600;padding:11px 20px;cursor:pointer;transition:all .15s;margin-bottom:-2px}
.tab-btn.active{color:#f1f5f9;border-bottom-color:#0ea5e9}
.tab-btn:hover:not(.active){color:#94a3b8}
.tab-content{display:none}
.tab-content.active{display:block}
.tab-badge{display:inline-block;background:#dc2626;color:#fff;font-size:10px;font-weight:800;padding:0 6px;border-radius:9999px;margin-left:4px;vertical-align:middle}

/* ── Crypto tab ── */
.cr-note{font-size:11px;color:#94a3b8;line-height:1.6;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:10px 12px;margin-bottom:12px}
.cr-watchlist-manager{display:flex;gap:8px;margin-bottom:14px}
.wl-search{margin:12px 0 18px}
.wl-search .cr-input{width:100%;max-width:520px}
.wl-results{margin-top:10px;display:grid;gap:8px;max-width:720px}
.wl-row{display:grid;grid-template-columns:1fr auto;gap:4px 12px;align-items:center;padding:10px 12px;border:1px solid var(--line,#334155);border-radius:10px;background:var(--panel,#1e293b)}
.wl-row .wl-pair{font-weight:600;color:var(--text-strong,#f1f5f9)}
.wl-row .wl-meta{grid-column:1;font-size:12px;color:var(--muted,#94a3b8);font-variant-numeric:tabular-nums}
.wl-row .wl-up{color:var(--pos,#4ade80)} .wl-row .wl-down{color:var(--neg,#f87171)}
.wl-row button{grid-row:1 / span 2;grid-column:2;padding:8px 14px;border-radius:8px;border:1px solid var(--accent,#0ea5e9);background:transparent;color:var(--accent,#0ea5e9);font-weight:600;cursor:pointer}
.wl-row button:disabled{border-color:var(--line,#334155);color:var(--muted,#94a3b8);cursor:default}
.wl-badge{display:inline-block;margin-left:6px;padding:1px 6px;border-radius:5px;font-size:11px;font-weight:500;background:var(--mut-t,rgba(148,163,184,.15));color:var(--muted,#94a3b8)}
.wl-msg{font-size:13px;color:var(--muted,#94a3b8)}
.wl-sub{font-size:14px;margin:4px 0 10px;color:var(--muted,#94a3b8);font-weight:600}
.cr-input{flex:1;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:9px 12px;color:#e2e8f0;font-size:13px}
.cr-input::placeholder{color:#475569}
.cr-add-btn{background:#0ea5e9;color:#0f172a;border:none;border-radius:8px;padding:9px 16px;font-weight:800;font-size:12px;cursor:pointer}
.cr-add-btn:hover{background:#38bdf8}
.cr-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px}
.cr-coin{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:12px 14px}
.cr-coin-top{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.cr-coin-sym{font-size:14px;font-weight:800;color:#f1f5f9}
.cr-coin-remove{margin-left:auto;background:none;border:none;color:#475569;font-size:14px;cursor:pointer;padding:0 4px;line-height:1}
.cr-coin-remove:hover{color:#f87171}
.cr-coin-price{font-size:20px;font-weight:900;color:#f1f5f9;margin-bottom:2px}
.cr-coin-chg{font-size:12px;font-weight:700}
.cr-coin-chg.up{color:#4ade80}
.cr-coin-chg.down{color:#f87171}
.cr-coin-stats{display:flex;gap:10px;margin-top:8px;font-size:10px;color:#64748b}
.cr-coin-stats b{color:#94a3b8}
.cr-sig-card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:12px 14px;margin-bottom:10px}
.cr-sig-top{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
.cr-sig-dir{font-size:11px;font-weight:800;padding:2px 8px;border-radius:5px}
.cr-sig-dir.long{background:#14532d;color:#4ade80}
.cr-sig-dir.short{background:#450a0a;color:#f87171}
.cr-sig-sym{font-size:13px;font-weight:800;color:#f1f5f9}
.cr-sig-name{font-size:10px;font-weight:700;color:#7dd3fc;background:#0c2140;padding:2px 7px;border-radius:4px}
.cr-sig-tf{font-size:10px;color:#64748b;margin-left:auto}
.cr-sig-when{font-size:10px;color:#64748b;margin-top:2px;font-variant-numeric:tabular-nums}
.cr-coin-nodata{opacity:.75;border-style:dashed}
.cr-nodata{font-size:14px;color:#94a3b8;font-weight:500}
.cr-sig-desc{font-size:12px;color:#94a3b8;margin-bottom:6px}
.cr-sig-row{display:flex;gap:14px;font-size:11px;color:#64748b;flex-wrap:wrap}
.cr-sig-row b{color:#e2e8f0}
.cr-sig-card.unviable{opacity:.72;border-color:#7f1d1d}
.cr-sig-warn{margin-top:8px;font-size:11px;color:#fca5a5;background:#2a1114;border:1px solid #7f1d1d;border-radius:6px;padding:7px 9px;line-height:1.5}
.cr-commodity{display:flex;gap:14px;flex-wrap:wrap}
.cr-comm-card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:12px 16px;min-width:140px}
.cr-comm-name{font-size:11px;color:#64748b;margin-bottom:4px}
.cr-comm-price{font-size:18px;font-weight:800;color:#f1f5f9}
/* Signal tabs + pagination + glossary */
.cr-sig-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.cr-tab-n{display:inline-block;margin-left:5px;font-size:9px;opacity:.65;
  font-variant-numeric:tabular-nums}
.cr-sig-tab.quiet{opacity:.55}
.cr-sig-tab{background:#1e293b;border:1px solid #334155;color:#94a3b8;font-size:11px;font-weight:700;padding:5px 12px;border-radius:9999px;cursor:pointer}
.cr-sig-tab:hover{border-color:#0ea5e9}
.cr-sig-tab.active{background:#0ea5e9;border-color:#0ea5e9;color:#0f172a}
.cr-pagination{display:flex;align-items:center;justify-content:center;gap:14px;margin-top:12px}
.cr-page-btn{background:#1e293b;border:1px solid #334155;color:#e2e8f0;font-size:12px;font-weight:700;padding:6px 14px;border-radius:8px;cursor:pointer}
.cr-page-btn:hover:not(:disabled){border-color:#0ea5e9}
.cr-page-btn:disabled{opacity:.4;cursor:default}
.cr-page-label{font-size:11px;color:#64748b}
.cr-glossary{margin-top:16px;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:10px 14px}
.cr-glossary summary{cursor:pointer;font-size:12px;font-weight:700;color:#7dd3fc;list-style:none}
.cr-glossary summary::-webkit-details-marker{display:none}
.cr-glossary summary::before{content:'▸ ';color:#475569}
.cr-glossary[open] summary::before{content:'▾ '}
.cr-glossary dl{margin-top:10px}
.cr-glossary dt{font-size:12px;font-weight:700;color:#e2e8f0;margin-top:8px}
.cr-glossary dd{font-size:11px;color:#94a3b8;margin-top:2px;line-height:1.5}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}

.side-themes{display:flex;gap:7px;padding:10px 16px;flex-wrap:wrap}
.side-themes .dot{width:15px;height:15px;border-radius:50%;cursor:pointer;
  border:2px solid transparent;box-shadow:0 0 0 1px var(--line);transition:transform .12s}
.side-themes .dot:hover{transform:scale(1.18)}
.side-themes .dot.on{border-color:var(--text-strong);box-shadow:0 0 0 1px var(--text-strong)}

/* ── Signal cards ──────────────────────────────────────────────────────── */
/* The page was one flat slate blue end to end, which made a losing setup and
   a good one look identical at a glance. Direction now tints the card edge,
   the levels carry their own colours, and the track shows where price sits
   between stop and target without reading a single number. */
/* Two columns once there is room. One card stretched across 1140px puts the
   stop and the target so far apart they stop reading as one setup. */
.desk-link{display:flex;align-items:center;gap:13px;text-decoration:none;
  background:linear-gradient(90deg,var(--acc-t),transparent 70%),var(--panel);
  border:1px solid var(--acc-t2);border-radius:12px;padding:13px 16px;margin-bottom:14px}
.desk-link:hover{border-color:var(--accent)}
.desk-ico{font-size:21px;line-height:1}
.desk-txt{display:flex;flex-direction:column;gap:2px;min-width:0}
.desk-txt b{color:var(--text-strong);font-size:14.5px}
.desk-txt span{color:var(--muted);font-size:12px}
.desk-go{margin-left:auto;color:var(--accent);font-weight:700;font-size:13px;white-space:nowrap}
@media(max-width:620px){.desk-txt span{display:none}}
#cr-signals,#dash-signals{display:grid;gap:12px;
  grid-template-columns:repeat(auto-fill,minmax(430px,1fr))}
#cr-signals .sig,#dash-signals .sig{margin-bottom:0}
@media(max-width:900px){#cr-signals,#dash-signals{grid-template-columns:1fr}}

.sig{background:linear-gradient(180deg,var(--panel2) 0%,var(--panel) 100%);
  border:1px solid var(--line);border-left:3px solid var(--muted2);border-radius:12px;
  padding:14px 15px;display:flex;flex-direction:column;gap:11px;margin-bottom:12px}
.sig.long{border-left-color:var(--pos-strong);box-shadow:inset 0 1px 0 var(--pos-t)}
.sig.short{border-left-color:var(--neg-strong);box-shadow:inset 0 1px 0 var(--neg-t)}
.sig.unviable{border-left-color:var(--muted2);opacity:.72}

.sig-head{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.sig-sym{font-size:15px;font-weight:700;color:var(--text-strong);letter-spacing:-.01em}
.sig-dir{font-size:10px;font-weight:600;padding:3px 9px;border-radius:20px}
.sig-dir.long{background:var(--pos-t);color:var(--pos);border:1px solid var(--pos-t2)}
.sig-dir.short{background:var(--neg-t);color:var(--neg);border:1px solid var(--neg-t2)}
.sig-profit{text-align:right;background:var(--pos-t);border:1px solid var(--pos-t2);
  border-radius:9px;padding:5px 11px;line-height:1.15}
.sig-profit b{display:block;font-size:15px;color:var(--pos);font-variant-numeric:tabular-nums}
.sig-profit span{font-size:9px;color:var(--pos);text-transform:uppercase;letter-spacing:.05em}
.sig-profit.muted{background:var(--mut-t);border-color:var(--muted2)}
.sig-profit.muted b{color:var(--muted)}.sig-profit.muted span{color:var(--muted2)}

.sig-meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:10px;color:var(--muted2)}
.sig-setup{background:var(--acc-t);color:var(--accent-soft);border:1px solid var(--acc-t2);
  padding:2px 8px;border-radius:20px;font-weight:600}
.sig-when{color:var(--muted2);font-variant-numeric:tabular-nums}
.sig-horizon{color:var(--accent-soft);font-weight:600}

.sig-levels{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.sig-levels>div{display:flex;flex-direction:column;gap:1px}
.sig-levels .mid{align-items:center;text-align:center}
.sig-levels .right{align-items:flex-end;text-align:right}
.sig-levels label{font-size:9px;color:var(--muted2);text-transform:uppercase;letter-spacing:.05em}
.sig-levels b{font-size:14px;color:var(--text);font-variant-numeric:tabular-nums}
.sig-levels b.pos{color:var(--pos)}.sig-levels b.neg{color:var(--neg)}
.sig-levels span{font-size:9px;color:var(--muted2);font-variant-numeric:tabular-nums}

.sig-trail{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:12px;
  color:var(--muted);background:var(--panel2);border:1px dashed var(--line);
  border-radius:8px;padding:6px 10px;margin-top:8px}
.sig-trail b{color:var(--text-strong);font-variant-numeric:tabular-nums}
.sig-trail-k{font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--accent);font-weight:700}
.sig-track{position:relative;height:6px;background:var(--sunk);border-radius:3px;margin:2px 0 6px}
.sig-track .cap{position:absolute;top:-2px;width:4px;height:10px;border-radius:2px}
.sig-track .cap.sl{left:0;background:var(--neg-strong)}
.sig-track .cap.tp{right:0;background:var(--pos-strong)}
.sig-track .fill{position:absolute;left:0;top:0;height:6px;border-radius:3px;
  background:linear-gradient(90deg,var(--neg-t2),var(--acc-t2))}
.sig-track .now{position:absolute;top:-5px;width:0;height:0;margin-left:-5px;
  border-left:5px solid transparent;border-right:5px solid transparent;
  border-top:7px solid var(--accent2)}

.sig-foot{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.pill{font-size:9px;color:var(--muted);border:1px solid var(--line);background:var(--sunk);
  padding:3px 8px;border-radius:20px;font-variant-numeric:tabular-nums}
.pill.ok{color:var(--pos);border-color:var(--pos-t2);background:var(--pos-t)}
.pill.bad{color:var(--neg);border-color:var(--neg-t2);background:var(--neg-t)}
.sig-act{font-size:11px;font-weight:600;padding:6px 14px;border-radius:8px}
.sig-act.long{background:var(--pos-btn);color:var(--text-strong)}
.sig-act.short{background:var(--neg-btn);color:var(--text-strong)}
.sig-act.off{background:var(--panel);color:var(--muted2);border:1px solid var(--line)}
.sig-warn{background:var(--neg-t);border:1px solid var(--neg-t2);
  border-radius:8px;padding:9px 11px;font-size:10px;color:var(--neg);line-height:1.5}
.sig-trail-toggle{margin-top:8px;font-size:10px;color:var(--accent-soft);cursor:pointer;
  user-select:none;padding:4px 0}
.sig-trail-toggle:hover{text-decoration:underline}
.sig-review-trail{display:none;flex-direction:column;gap:5px;margin-top:4px;
  border-top:1px solid var(--line);padding-top:6px}
.sig-review-trail.open{display:flex}
.trail-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;font-size:10px;
  color:var(--muted2);background:var(--panel);border:1px solid var(--line);
  border-radius:6px;padding:5px 8px}
.trail-role{font-weight:600;color:var(--text)}
.trail-status{margin-left:auto;font-weight:600}
.trail-status.ok{color:var(--pos)}
.trail-status.bad{color:var(--neg)}

/* A little colour elsewhere, so the page is not one flat field of slate. */
.card{background:linear-gradient(180deg,var(--panel) 0%,var(--panel2) 100%)}
.card-value.pos{color:var(--pos)}.card-value.neg{color:var(--neg)}
section h2{color:var(--accent-soft)}

/* ── Tables ────────────────────────────────────────────────────────────── */
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--panel);
  -webkit-overflow-scrolling:touch}
.tbl{border-collapse:collapse;width:100%;font-size:12px}
.tbl th{font-size:9px;color:var(--muted2);text-transform:uppercase;letter-spacing:.07em;
  text-align:left;padding:9px 11px;background:var(--line2);white-space:nowrap;font-weight:600}
.tbl td{padding:9px 11px;border-top:1px solid var(--bg);color:var(--text);
  font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl td.sub{color:var(--muted2);font-size:11px}
.tbl tbody tr:hover{background:var(--line2)}

/* ── Paper trading desk ────────────────────────────────────────────────── */
.ccy-pick{display:flex;align-items:center;gap:7px;margin-right:14px}
.ccy-pick label{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.ccy-pick select{font:inherit;font-size:12px;color:var(--text);background:var(--panel);
  border:1px solid var(--line);border-radius:7px;padding:5px 8px;min-width:88px}

.pt-num{font-variant-numeric:tabular-nums;white-space:nowrap;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.pt-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;
  overflow:hidden;margin-bottom:22px}
.pt-cell{background:var(--panel);padding:11px 13px}
.pt-k{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
  margin-bottom:5px}
.pt-v{font-size:16px;font-weight:600;color:var(--text-strong);
  font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.pt-v small{font-size:11px;font-weight:500;color:var(--muted);margin-left:4px}
.pt-up{color:var(--pos)!important}.pt-down{color:var(--neg)!important}
.pt-rail{height:5px;border-radius:99px;background:var(--sunk);border:1px solid var(--line2);
  overflow:hidden;margin-top:9px}
.pt-rail i{display:block;height:100%;background:var(--accent)}
.pt-railcap{display:flex;justify-content:space-between;font-size:10px;color:var(--muted);
  margin-top:5px}

.pt-logbtn{margin-left:8px;font:inherit;font-size:11px;font-weight:600;padding:3px 9px;
  border-radius:7px;cursor:pointer;background:var(--acc-t);color:var(--accent);
  border:1px solid var(--acc-t2)}
.pt-logrow td{background:var(--panel2);padding:10px 12px!important}
.pt-logrow td::before{content:none!important}
.pt-log{width:100%;border-collapse:collapse;font-size:12px}
.pt-log th{text-align:left;color:var(--muted2);font-weight:600;padding:4px 6px}
.pt-log td{padding:5px 6px;border-top:1px solid var(--line2);vertical-align:top;
  display:table-cell!important;white-space:normal}
.pt-shead{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:baseline;
  justify-content:space-between;margin-bottom:10px}
.pt-shead h2{font-size:12px;font-weight:600;margin:0;letter-spacing:.04em;
  text-transform:uppercase;color:var(--muted)}
.pt-count{font-size:11px;color:var(--muted2)}

.pt-scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;
  background:var(--panel)}
.pt-scroll table{border-collapse:collapse;width:100%;min-width:820px;font-size:12px}
.pt-scroll thead th{background:var(--panel2);text-align:left;font-size:10px;
  letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;
  padding:9px 11px;border-bottom:1px solid var(--line);white-space:nowrap}
.pt-scroll thead th.sortable{cursor:pointer;user-select:none}
.pt-scroll thead th.sortable:hover{color:var(--text)}
.pt-scroll thead th .arw{opacity:.45;font-size:9px;margin-left:3px}
.pt-scroll td{padding:10px 11px;border-bottom:1px solid var(--line2);color:var(--text)}
.pt-scroll tbody tr:last-child td{border-bottom:0}
.pt-scroll tbody tr:hover{background:var(--panel2)}
.pt-scroll th.r,.pt-scroll td.r{text-align:right}
.pt-sym{font-weight:600;color:var(--text-strong)}
.pt-side{display:inline-block;font-size:9.5px;font-weight:700;padding:2px 6px;
  border-radius:4px;letter-spacing:.05em;margin-left:7px}
.pt-side.l{background:rgba(74,222,128,.14);color:var(--pos)}
.pt-side.s{background:rgba(248,113,113,.14);color:var(--neg)}
.pt-setup{font-size:10.5px;color:var(--muted);display:block;margin-top:3px}
.pt-unit{font-size:9.5px;color:var(--muted2);letter-spacing:.03em}
.pt-trail{font-size:9px;color:var(--accent);letter-spacing:.05em;
  text-transform:uppercase;margin-left:6px}
.pt-mode{display:inline-block;font-size:9.5px;font-weight:700;padding:2px 6px;border-radius:4px;letter-spacing:.04em;margin-left:5px}
.pt-mode.intra{background:rgba(59,130,246,.18);color:var(--accent)}
.pt-mode.deliv{background:rgba(168,85,247,.18);color:var(--brand)}
.pt-partial{display:inline-block;font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;background:rgba(34,197,94,.16);color:var(--pos);margin-left:5px}
.pt-muted{color:var(--muted2)}

.pt-prail{position:relative;height:22px;min-width:130px}
.pt-ptrack{position:absolute;top:10px;left:0;right:0;height:3px;background:var(--sunk);
  border-radius:99px;border:1px solid var(--line2)}
.pt-pfill{position:absolute;top:10px;height:3px;border-radius:99px}
.pt-ptick{position:absolute;top:5px;width:1px;height:13px;background:var(--muted2)}
.pt-pmark{position:absolute;top:4px;width:2px;height:15px;border-radius:1px}
.pt-plab{position:absolute;top:0;font-size:9px;color:var(--muted2)}

.pt-tag{display:inline-block;font-size:9.5px;font-weight:700;padding:2px 7px;
  border-radius:99px;letter-spacing:.03em;white-space:nowrap;text-transform:lowercase}
.pt-tag.target,.pt-tag.trail,.pt-tag.profit_lock{background:rgba(74,222,128,.14);color:var(--pos)}
.pt-tag.stop,.pt-tag.liquidated{background:rgba(248,113,113,.14);color:var(--neg)}
.pt-tag.expired{background:var(--sunk);color:var(--muted);border:1px solid var(--line)}

.pt-filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:11px;align-items:center}
.pt-filters label{font-size:10px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted);margin-right:-3px}
.pt-filters select,.pt-filters input[type=search]{font:inherit;font-size:12px;
  color:var(--text);background:var(--panel);border:1px solid var(--line);
  border-radius:7px;padding:6px 9px;min-width:100px}
.pt-filters input[type=search]{min-width:128px}
.pt-chip{font:inherit;font-size:11px;color:var(--muted);background:var(--panel);
  border:1px solid var(--line);border-radius:99px;padding:5px 11px;cursor:pointer}
.pt-chip[aria-pressed="true"]{background:var(--accent);color:var(--sunk);
  border-color:var(--accent);font-weight:700}
.pt-clear{margin-left:auto;font:inherit;font-size:11px;color:var(--muted);
  background:none;border:0;cursor:pointer;text-decoration:underline}

.pt-sum{display:grid;grid-template-columns:repeat(auto-fit,minmax(108px,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);border-top:0;
  border-radius:0 0 10px 10px;overflow:hidden}
.pt-sum .pt-cell{padding:9px 12px}
.pt-sum .pt-v{font-size:14px}
.pt-note{font-size:11px;color:var(--muted2);margin-top:9px;line-height:1.6}
@media (max-width:640px){
  .pt-clear{margin-left:0}
  .ccy-pick label{display:none}

  /* These two tables are eleven and twelve columns wide. .pt-scroll existed
     to drag them sideways past an 820px floor, which on a 390px phone means
     you can see a position's market or its P&L but never both — and this is
     the page that gets opened on a phone more than all the others together.
     Rows become cards like everywhere else; the scroller and the width floor
     both have to go with them, or the card is 820px wide and still scrolls. */
  .pt-scroll{overflow-x:visible;border:0;background:none;border-radius:0}
  .pt-scroll table{min-width:0}
  .pt-scroll .tbl tr{background:var(--panel);border:1px solid var(--line);
    border-radius:10px;padding:11px 13px;margin-bottom:9px}

  /* The first cell is the card's heading — symbol, side, setup — not a value.
     Labelling it "Market" and right-aligning it against nothing reads as a
     missing value rather than a title. */
  .pt-scroll .tbl td:first-child{display:block;text-align:left;
    padding:0 0 8px;margin-bottom:6px;border-bottom:1px solid var(--line2)}
  .pt-scroll .tbl td:first-child::before{content:none}

  /* The stop-to-target rail is a drawing, not a number. In a two-column row
     it gets whatever is left over — which at this width is nothing — so it
     takes the full width under its own label instead. */
  .pt-scroll .tbl td:has(.pt-prail){display:block;text-align:left}
  .pt-scroll .tbl td:has(.pt-prail)::before{display:block;margin-bottom:7px}
  .pt-scroll .pt-prail{min-width:0;width:100%}

  /* The secondary line under a value — the ₹ equivalent of a quantity —
     belongs under it, not beside it, once the row is a label/value pair. */
  .pt-scroll .tbl td{align-items:flex-start}
  .pt-scroll .tbl td .pt-notional{text-align:right}
}

/* ── Sidebar shell ─────────────────────────────────────────────────────── */
.app{display:flex;min-height:100vh}
.sidebar{width:212px;flex:none;background:var(--bg);border-right:1px solid var(--line);
  padding:16px 0;display:flex;flex-direction:column;gap:2px;position:sticky;top:0;height:100vh}
.side-brand{padding:0 16px 16px;display:flex;align-items:center;gap:9px}
.side-brand b{color:var(--text-strong);font-size:13px;font-weight:600;letter-spacing:-.01em}
.side-group{padding:12px 16px 6px;font-size:9px;font-weight:600;color:var(--muted2);
  text-transform:uppercase;letter-spacing:.09em}
.side-item{padding:7px 16px;display:flex;align-items:center;gap:9px;cursor:pointer;
  border-left:2px solid transparent;color:var(--muted);font-size:12px;text-decoration:none;
  transition:background .12s,color .12s}
.side-item:hover{background:var(--line2);color:var(--text)}
.side-item.active{background:var(--panel);border-left-color:var(--accent);color:var(--text-strong);font-weight:600}
.side-item svg{flex:none;stroke:var(--muted2)}
.side-item.active svg{stroke:var(--accent)}
.side-foot{margin-top:auto;padding:12px 16px;border-top:1px solid var(--panel);
  display:flex;flex-direction:column;gap:5px}
.side-dot{width:6px;height:6px;border-radius:50%;background:var(--pos);display:inline-block}
.main{flex-grow:1;min-width:0;display:flex;flex-direction:column}
.main-head{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;
  align-items:center;gap:16px;flex-wrap:wrap}
.main-head h1{font-size:17px;font-weight:600;color:var(--text-strong);letter-spacing:-.01em;margin:0}
.main-head .sub{font-size:11px;color:var(--muted2);margin-top:2px}
.main-body{padding:20px 24px;display:flex;flex-direction:column;gap:16px}
.side-toggle{display:none}
/* Phone: the sidebar becomes a bottom bar. Icons-only in a rail would put the
   nav under the thumb-unreachable top-left corner on a 6" screen. */
@media(max-width:820px){
  .app{flex-direction:column}
  .sidebar{position:fixed;bottom:0;left:0;right:0;top:auto;width:auto;height:auto;
    flex-direction:row;border-right:none;border-top:1px solid var(--line);padding:0;
    overflow-x:auto;z-index:50;gap:0}
  .side-brand,.side-group,.side-foot,.side-themes{display:none}
  .sidebar.more-open .side-themes{display:flex;width:100%;justify-content:center;
    border-top:1px solid var(--line2);padding:9px 0}
  .side-item{flex-direction:column;gap:3px;padding:8px 14px;border-left:none;
    border-top:2px solid transparent;font-size:9px;white-space:nowrap}
  .side-item.active{border-left:none;border-top-color:var(--accent)}
  .main-body{padding:14px 12px 76px}
  .main-head{padding:12px 14px}
}

/* ── Phone ─────────────────────────────────────────────────────────────── */
/* A 9-column table inside a horizontal scroller is technically readable and
   practically useless on a 390px screen — you cannot see the symbol and the
   number at the same time. Below 640px each row becomes its own card with the
   column name beside every value, so nothing needs sideways scrolling. */
@media(max-width:640px){
  .pt-log tr{display:table-row!important;border:0!important;padding:0!important;
    background:none!important}
  .pt-log td,.pt-log th{font-size:11px}
  html{-webkit-text-size-adjust:100%}
  .main-body{padding:12px 11px 20px}
  footer{padding-bottom:76px}
  .main-head{padding:11px 12px}
  .main-head h1{font-size:15px}
  section h2{font-size:11px}

  .cards{grid-template-columns:1fr 1fr !important;gap:9px}
  .card{padding:11px}
  .card-value{font-size:18px}
  .card-title{font-size:10px}
  .card-sub{font-size:9px}

  .scroll{border:none;background:none;overflow-x:visible}
  .tbl,.tbl tbody,.tbl tr,.tbl td{display:block;width:100%}
  .tbl thead{display:none}
  .tbl tr{background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:9px 11px;margin-bottom:8px}
  .tbl tr:hover{background:var(--panel)}
  .tbl td{border:none;padding:3px 0;white-space:normal;font-size:12px;
    display:flex;justify-content:space-between;align-items:baseline;gap:12px}
  .tbl td::before{content:attr(data-label);color:var(--muted2);font-size:10px;
    text-transform:uppercase;letter-spacing:.05em;flex:none}
  .tbl td:first-child{padding-bottom:6px;margin-bottom:4px;
    border-bottom:1px solid var(--bg);font-weight:600;color:var(--text-strong)}
  .tbl td:empty{display:none}

  /* Anything tapped needs a real target, not a 9px label. */
  .side-item{min-height:46px;justify-content:center}
  .cr-add-btn,.cr-page-btn,button{min-height:40px}
  .cr-input{min-height:40px;font-size:16px}   /* 16px stops iOS zooming on focus */
  .cr-sig-tab{min-height:34px;font-size:12px}
  .cr-coin-remove{min-width:32px;min-height:32px}

  .cr-grid{grid-template-columns:1fr 1fr !important}
  .cr-sig-card{padding:11px}
  .sig{padding:12px}
  .sig-sym{font-size:14px}
  .sig-levels b{font-size:13px}
  .sig-act{padding:8px 14px;min-height:38px;display:flex;align-items:center}
  .cr-note{font-size:11px}
}
/* Ten destinations do not fit a phone bar, and a sideways scroller with no
   affordance hides half of them. Five live on the bar; the rest open in a
   sheet.

   The default has to be declared BEFORE the phone rule. It used to sit after
   it at the same specificity, so `display:none` won at every width, the More
   button never rendered, and six pages had no way in on a phone at all. */
.side-more{display:none}

@media(max-width:640px){
  .sidebar{overflow-x:visible;justify-content:space-between}
  .side-secondary{display:none}
  .side-more{display:flex}

  /* The six items have to SHARE the width, not each take what they want.
     At their natural widths — 9px labels, nowrap, 14px of side padding — the
     bar needs about 463px and a phone has 390. It overflowed by 73, and the
     item that fell off the end was More, which is the one that reaches the
     other six pages. The sheet was built, shipped, and unreachable.

     flex:1 1 0 with min-width:0 is what makes the items divide the bar
     instead of overflowing it; without the min-width they refuse to shrink
     below their content and nothing changes. Labels wrap to a second line
     rather than truncating, because "Price Outl…" and "Paper Tradi…" are
     not navigation. */
  .side-item{flex:1 1 0;min-width:0;padding:7px 3px;white-space:normal;
    font-size:9px;line-height:1.14;text-align:center;overflow:hidden}
  .side-item svg{flex:none}
  /* Wrapping at spaces only. Allowing breaks inside words turned the bar
     into "Da shb oar d" — every label here is one or two words, so a space
     is always the right place to break. */
  .side-item span{display:block;width:100%}
  /* Opening it wrapped the bar into a second and third row, which pushed
     the five primary tabs up the screen and reflowed the page behind. A
     sheet leaves the bar where the thumb last saw it. */
  .sidebar.more-open{flex-wrap:wrap;padding-bottom:calc(4px + env(safe-area-inset-bottom,0px))}
  /* The cards lay out ABOVE the bar, not alongside it.
     Both were in one flex-wrap container: the cards claim a third of the
     width each and the bar items shrink to nothing, so the browser packed
     cards and tabs onto the same first row and squeezed the five tabs into
     50px each. order:-1 puts the whole card block ahead of the bar in the
     flow, which with flex-wrap means its own rows above — and leaves the bar
     at the bottom edge where the thumb last saw it. */
  .sidebar.more-open .side-secondary{order:-1;
    display:flex;flex-direction:column;justify-content:center;
    /* Three across, less the horizontal margin, or the third wraps. */
    flex:0 0 calc(33.333% - 3px);max-width:calc(33.333% - 3px);
    padding:13px 4px;gap:3px;border-radius:10px;
    background:var(--panel2);border:1px solid var(--line2);
    margin:3px 1.5px}
  .sidebar.more-open .side-secondary svg{width:19px;height:19px}
  .sidebar.more-open .side-secondary span{font-size:10px;line-height:1.3}
  /* The sheet is the surface in front; lift it off the page behind. */
  .sidebar.more-open{box-shadow:0 -10px 28px rgba(0,0,0,.34)}
  .more-scrim{position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:40;display:none}
  .more-scrim.on{display:block}
}

@media(max-width:380px){
  .cards,.cr-grid{grid-template-columns:1fr !important}
}

</style>
</head>
<body>
<div class="app">
<div class="more-scrim" id="more-scrim" onclick="toggleMore()"></div>
<nav class="sidebar" id="sidebar">
  <div class="side-brand">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#0ea5e9" stroke-width="2"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>
    <b>Trading Desk</b>
  </div>
  <div class="side-item" data-tab="dashboard" data-group="trading" onclick="switchTab('dashboard')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/></svg><span>Trading</span></div>
  <div class="side-item" data-tab="crypto" data-group="signals" onclick="switchTab('crypto')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12h4l3-8 4 16 3-8h6"/></svg><span>Signals</span></div>
  <a class="side-item" data-tab="market" data-group="market" href="/predict"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg><span>Market</span></a>
  <a class="side-item" data-tab="research" data-group="research" href="/pipeline"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3h6M10 3v6L4 19a2 2 0 0 0 2 3h12a2 2 0 0 0 2-3l-6-10V3"/></svg><span>Research</span></a>
  <a class="side-item" data-tab="settings" data-group="settings" href="/settings"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-2.6-1.1l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 3 15.4H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.1-2.6l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/></svg><span>Settings</span></a>
  <a class="side-item" data-tab="admin" data-group="admin" href="/data"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg><span>Admin</span></a>
  <div class="side-themes" id="side-themes">
    <span class="dot" data-t="amber"   style="background:#f59e0b" onclick="setSiteTheme('amber')"   title="Amber Terminal"></span>
    <span class="dot" data-t="carbon"  style="background:#a3e635" onclick="setSiteTheme('carbon')"  title="Carbon Lime"></span>
    <span class="dot" data-t="crimson" style="background:#fb7185" onclick="setSiteTheme('crimson')" title="Crimson"></span>
    <span class="dot" data-t="violet"  style="background:#a78bfa" onclick="setSiteTheme('violet')"  title="Violet Night"></span>
    <span class="dot" data-t="emerald" style="background:#34d399" onclick="setSiteTheme('emerald')" title="Emerald Court"></span>
    <span class="dot" data-t="navy"    style="background:#0ea5e9" onclick="setSiteTheme('navy')"    title="Deep Navy"></span>
    <span class="dot" data-t="light"   style="background:#f4f1ea" onclick="setSiteTheme('light')"   title="Polar White"></span>
    <span class="dot" data-t="neu"     style="background:#e0a84a;box-shadow:inset 2px 2px 4px #0006" onclick="setSiteTheme('neu')" title="Soft UI (dark)"></span>
    <span class="dot" data-t="neu-light" style="background:#e6e9ef;box-shadow:inset 2px 2px 4px #0003" onclick="setSiteTheme('neu-light')" title="Soft UI (light)"></span>
  </div>
  <div class="side-foot">
    <div style="display:flex;align-items:center;gap:6px">
      <span class="side-dot" id="side-status-dot"></span>
      <span style="font-size:10px;color:#94a3b8" id="side-status">Loading&hellip;</span>
    </div>
    <div style="font-size:9px;color:#475569" id="side-uptime">&mdash;</div>
  </div>
</nav>
<div class="main">
  <div class="main-head">
    <div style="min-width:0">
      <h1 id="page-title">Dashboard</h1>
      <div class="sub" id="page-sub">Last 7 days</div>
    </div>
    <div style="flex-grow:1"></div>
    <div class="ccy-pick">
      <label for="ccy-select">Display</label>
      <select id="ccy-select" aria-label="Display currency">
        <option value="USDT">USDT</option>
        <option value="INR">&#8377; INR</option>
      </select>
    </div>
    <span class="refresh" id="refresh-label">Loading&hellip;</span>
  </div>
  <div class="main-body">


<div id="tab-dashboard" class="tab-content active">
  <a class="desk-link" href="/predict">
    <span class="desk-ico">&#128200;</span>
    <span class="desk-txt"><b>Price Outlook</b>
      <span>Where every watchlist market is likely to be in 1, 4 and 24 hours</span></span>
    <span class="desk-go">Open &rarr;</span>
  </a>
  <div class="cards" id="dash-cards"></div>
  <section>
    <h2>Predictions &middot; last 7 days</h2>
    <div class="cr-note">Anything older rolls into <b>Historic Data</b>. Move is the distance to target; &times; cost is how many round trips it covers.</div>
    <div id="dash-signals"><div class="empty">Loading&hellip;</div></div>
  </section>
  <section>
    <h2>Why signals were refused</h2>
    <div id="dash-refused"><div class="empty">Loading&hellip;</div></div>
  </section>
  <section>
    <h2>🛡️ Filter &amp; Veto Activity &middot; Protection Circuit Breakers</h2>
    <div class="cr-note">Signals filtered or dropped by technical protections (RSI exhaustion, 1h macro trend, loss cooldowns) or AI review hard veto (adverse news, funding squeeze).</div>
    <div id="dash-veto-log"><div class="empty">Loading&hellip;</div></div>
  </section>
</div>


<div id="tab-guard" class="tab-content">
  <section>
    <h2>Session Guard</h2>
    <div class="cr-note">Behavioural flags computed from your own closed trades — size drift, hold-time drift, and how much of each gross was kept after costs.</div>
    <div class="cards" id="guard-cards"></div>
    <div id="guard-flags" style="margin-top:14px"><div class="empty">Loading&hellip;</div></div>
  </section>
  <section>
    <h2>Session drift</h2>
    <div id="guard-drift"><div class="empty">Loading&hellip;</div></div>
  </section>
</div>

<div id="tab-accuracy" class="tab-content">
  <section>
    <h2>Accuracy</h2>
    <div id="acc-banner"></div>
    <div class="cards" id="acc-cards"></div>
  </section>
  <section>
    <h2>Is confidence honest?</h2>
    <div class="cr-note">Stated confidence against realised win rate. Below the line means the bot is overconfident, and the sizing ladder is sizing on noise.</div>
    <div id="acc-calibration"><div class="empty">Loading&hellip;</div></div>
  </section>
  <section>
    <h2>Move size vs the cost floor</h2>
    <div class="cr-note">Every signal bucketed by how far its target sat. Red bars could not have paid for the round trip.</div>
    <div id="acc-moves"><div class="empty">Loading&hellip;</div></div>
  </section>
  <section>
    <h2>Accuracy by setup</h2>
    <div id="acc-setups"><div class="empty">Loading&hellip;</div></div>
  </section>
</div>

<div id="tab-historic" class="tab-content">
  <section>
    <h2>Historic Data</h2>
    <div class="cr-note">Everything older than 7 days. Read-only archive.</div>
    <div class="cards" id="hist-cards"></div>
    <div id="hist-table" style="margin-top:14px"><div class="empty">Loading&hellip;</div></div>
  </section>
</div>

<div id="tab-watchlist" class="tab-content">
  <section>
    <h2>Watchlist</h2>
    <div class="cr-note">The engine follows only the pairs on this list. Search Binance, check the price and volume, then add the exact pair. For gold, search <b>gold</b>: Binance lists it as tokenised gold (PAXG, XAUT), not as XAUUSDT.</div>
    <div class="wl-search">
      <input type="search" id="cr-add-input2" class="cr-input" placeholder="Search Binance: gold, sol, pepe…" autocomplete="off" aria-label="Search Binance pairs" onfocus="if(!this.value)wlSearchNow()" oninput="wlSearchSoon()" onkeydown="if(event.key==='Enter')wlSearchNow()">
      <div id="wl-results" class="wl-results" aria-live="polite"></div>
    </div>
    <h3 class="wl-sub">On the watchlist</h3>
    <div id="wl-coins"><div class="empty">Loading&hellip;</div></div>
  </section>
</div>

<div id="tab-paper" class="tab-content">
  <div class="cr-note" style="margin-bottom:12px">
    Simulated only — this never places a real order. A cycle ends when the wallet
    reaches its target or runs out, then a fresh one starts. Every cost is charged:
    brokerage, GST, funding and slippage.
  </div>
  <div id="paper-banner"></div>

  <div class="pt-strip" id="paper-strip"></div>
  <div id="regime-card" style="margin:10px 0"></div>
  <div id="registry-card" style="margin:10px 0"></div>

  <div class="pt-shead">
    <h2>Open positions</h2>
    <span class="pt-count" id="pt-open-count"></span>
  </div>
  <div class="pt-scroll" id="paper-positions"><div class="empty">Loading&hellip;</div></div>

  <div class="pt-shead" style="margin-top:26px">
    <h2>History</h2>
    <span class="pt-count" id="pt-hist-count"></span>
  </div>
  <div class="pt-filters" id="pt-filters">
    <label for="pf-sym">Market</label>
    <select id="pf-sym"><option value="">All</option></select>
    <label for="pf-side">Side</label>
    <select id="pf-side"><option value="">Both</option>
      <option value="long">long</option><option value="short">short</option></select>
    <label for="pf-setup">Setup</label>
    <select id="pf-setup"><option value="">All</option></select>
    <label for="pf-exit">Exit</label>
    <select id="pf-exit"><option value="">All</option></select>
    <button class="pt-chip" id="pf-win" aria-pressed="false" type="button">Wins</button>
    <button class="pt-chip" id="pf-loss" aria-pressed="false" type="button">Losses</button>
    <input type="search" id="pf-q" placeholder="Search&hellip;" aria-label="Search history">
    <button class="pt-clear" id="pf-reset" type="button">Reset</button>
  </div>
  <div class="pt-scroll" id="paper-trades"><div class="empty">Loading&hellip;</div></div>
  <div class="pt-sum" id="paper-scorecard"></div>
  <div class="pt-note">
    Costs stay broken out rather than netted into P&amp;L — fees and funding are the
    reason a winning hit rate can still lose money, so they keep their own columns.
    Summary figures follow the filters above.
  </div>
</div>

<div id="tab-crypto" class="tab-content">
  <section>
    <h2>🪙 Live Crypto Watchlist</h2>
    <div class="cr-note">Prices come from whatever you've enabled on <a href="/settings" style="color:inherit">Settings</a> — live, tick-by-tick when the Binance stream is on; polled every 15-60s otherwise. Add or remove symbols here — changes apply immediately, no redeploy needed.</div>
    <div class="cr-watchlist-manager">
      <input type="text" id="cr-add-input" class="cr-input" placeholder="Add symbol, e.g. dogeusdt" onkeydown="if(event.key==='Enter')addCryptoSymbol()">
      <button class="cr-add-btn" onclick="addCryptoSymbol()">+ Add</button>
    </div>
    <div id="cr-coins"><div class="empty">Loading watchlist…</div></div>
  </section>
  <section>
    <h2>Crypto Signals (last 24h)</h2>
    <div class="cr-sig-tabs" id="cr-sig-tabs"></div>
    <div id="cr-signals"><div class="empty">No crypto signals fired yet</div></div>
    <div class="cr-pagination" id="cr-sig-pagination"></div>
    <details class="cr-glossary">
      <summary>What do these terms mean?</summary>
      <dl>
        <dt>Momentum Reversal</dt>
        <dd>Price and momentum are disagreeing — e.g. price hits a new high but the move is losing steam. Often an early sign the current trend is running out.</dd>
        <dt>Volume Surge</dt>
        <dd>A lot more buying/selling activity than usual for this coin. Surges like this often come right before a bigger price move.</dd>
        <dt>Breakout Setup</dt>
        <dd>Price had been stuck in an unusually tight range and just broke out of it. Tight ranges tend to resolve with a sharper move than usual.</dd>
        <dt>News Catalyst</dt>
        <dd>Recent news coverage for this coin is unusually one-sided (strongly positive or negative).</dd>
        <dt>Confidence</dt>
        <dd>How strongly the model believes this signal will play out — not a guarantee. Higher is stronger, but every signal still carries risk.</dd>
        <dt>Edge</dt>
        <dd>The estimated price move (%) between the entry price and the target price.</dd>
        <dt>Entry / Target / Stop</dt>
        <dd>Suggested price to enter the trade, take profit at, and cut losses at if the trade goes the wrong way.</dd>
        <dt>Suggested stake</dt>
        <dd>A conservative position size (a small % of your bank) so no single trade risks too much — not a recommendation to trade this amount.</dd>
        <dt>RSI (Relative Strength Index)</dt>
        <dd>A 0–100 gauge of how "overbought" or "oversold" a coin is. Above 70 usually means overbought, below 30 usually means oversold.</dd>
      </dl>
    </details>
  </section>
  <section id="cr-commodities-section" style="display:none">
    <h2>Commodities</h2>
    <div id="cr-commodities"></div>
  </section>
</div>

<div id="tab-mirror" class="tab-content">
  <section>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:10px">
      <div>
        <h2 style="font-size:20px;font-weight:700;color:#f1f5f9;margin:0">Mirror Signals & Live Trade Check</h2>
        <div style="font-size:12px;color:#94a3b8;margin-top:2px">Live trade trajectory, excursion metrics (MFE/MAE), and Hugging Face AI dual reasoning</div>
      </div>
      <button id="btn-mirror-live-check" onclick="forceMirrorLiveCheck(this)" class="cr-page-btn" style="background:linear-gradient(135deg,#0284c7,#0ea5e9);color:#fff;border:none;padding:8px 16px;border-radius:8px;cursor:pointer;font-weight:600;display:flex;align-items:center;gap:6px;box-shadow:0 2px 8px rgba(14,165,233,0.3)">
        <span>⚡ Force Live Check & AI Review</span>
      </button>
    </div>

    <!-- Live Performance Scorecard -->
    <div id="cr-mirror-scorecard" class="grid" style="padding:0;margin-bottom:16px;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px">
      <div class="card"><div class="card-title">Total Tested</div><div class="card-value" id="ms-total">—</div><div class="card-sub">Last 48 hours</div></div>
      <div class="card"><div class="card-title">Worked Rate</div><div class="card-value" id="ms-rate" style="color:#10b981">—</div><div class="card-sub">Full + Partial</div></div>
      <div class="card"><div class="card-title">Full Target (Won)</div><div class="card-value" id="ms-full" style="color:#10b981">—</div><div class="card-sub">100% Target Hit</div></div>
      <div class="card"><div class="card-title">Partially Worked</div><div class="card-value" id="ms-partial" style="color:#f59e0b">—</div><div class="card-sub">>=40% to Target</div></div>
      <div class="card"><div class="card-title">Stopped Out</div><div class="card-value" id="ms-stopped" style="color:#ef4444">—</div><div class="card-sub">Direct Stop Loss</div></div>
      <div class="card"><div class="card-title">Active Now</div><div class="card-value" id="ms-running" style="color:#38bdf8">—</div><div class="card-sub">Live in Market</div></div>
      <div class="card"><div class="card-title">Avg Peak Gain</div><div class="card-value" id="ms-peak" style="color:#a78bfa">—</div><div class="card-sub">Max Excursion</div></div>
    </div>

    <!-- Status filter tabs -->
    <div class="cr-sig-tabs" id="cr-mirror-status-filter" style="margin-bottom:10px"></div>
    <!-- Symbol filter tabs -->
    <div class="cr-sig-tabs" id="cr-mirror-sig-tabs"></div>
    <!-- Signal cards -->
    <div id="cr-mirror-signals"><div class="empty">Loading mirror signals & running live trade checks...</div></div>
    <div class="cr-pagination" id="cr-mirror-sig-pagination"></div>
  </section>
</div>

<div id="tab-simulator" class="tab-content">
  <section>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:12px">
      <div>
        <h2 style="font-size:20px;font-weight:700;color:#38bdf8;margin:0">⚡ Live Market Simulator & Strategy Testing</h2>
        <div style="font-size:12px;color:#94a3b8;margin-top:2px">Test quantitative strategies, compounding cycles ($25 ➔ $100), and institutional order flow with zero lag</div>
      </div>
      <div style="display:flex;gap:8px">
        <button id="sim-btn-start" onclick="startSimulator()" style="background:#10b981;color:#fff;border:none;padding:9px 18px;border-radius:8px;cursor:pointer;font-weight:700;font-size:13px;display:flex;align-items:center;gap:6px">▶️ Run Simulator</button>
        <button id="sim-btn-pause" onclick="pauseSimulator()" style="background:#e11d48;color:#fff;border:none;padding:9px 18px;border-radius:8px;cursor:pointer;font-weight:700;font-size:13px;display:none;align-items:center;gap:6px">⏸️ Stop / Pause</button>
      </div>
    </div>

    <!-- Stepped Clock & Market Time Header -->
    <div id="sim-clock-bar" style="background:#0f172a;border:1px solid #334155;border-radius:10px;padding:12px 16px;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
      <div style="display:flex;align-items:center;gap:12px">
        <span style="font-size:12px;color:#94a3b8;font-weight:600">📅 MARKET CLOCK:</span>
        <span id="sim-clock-market" style="font-size:14px;font-weight:700;color:#38bdf8;font-family:monospace">Ready to start</span>
      </div>
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:12px;color:#94a3b8;font-weight:600">⏱️ PACING:</span>
        <span id="sim-clock-countdown" style="font-size:13px;font-weight:700;color:#10b981;background:rgba(16,185,129,0.1);padding:4px 10px;border-radius:6px;border:1px solid rgba(16,185,129,0.3)">Fast Mode</span>
      </div>
    </div>

    <!-- Progress Bar -->
    <div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px;margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;font-size:12px;color:#94a3b8;margin-bottom:6px">
        <span id="sim-progress-label">Simulation Progress: 0%</span>
        <span id="sim-progress-eta">ETA: Ready</span>
      </div>
      <div style="background:#0f172a;height:12px;border-radius:6px;overflow:hidden;border:1px solid #334155">
        <div id="sim-progress-bar" style="background:linear-gradient(90deg,#0ea5e9,#10b981);height:100%;width:0%;transition:width 0.4s ease"></div>
      </div>
    </div>

    <!-- Modular View Selector (Mobile Fast Loading) -->
    <div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;border-bottom:1px solid #334155;padding-bottom:10px">
      <button class="sub-tab-btn active" id="sim-view-btn-summary" onclick="switchSimView('summary')">📊 Executive Metrics</button>
      <button class="sub-tab-btn" id="sim-view-btn-cycles" onclick="switchSimView('cycles')">🏆 Compounding Cycles</button>
      <button class="sub-tab-btn" id="sim-view-btn-trades" onclick="switchSimView('trades')">📜 Trade Stream</button>
      <button class="sub-tab-btn" id="sim-view-btn-events" onclick="switchSimView('events')">📡 AI Events Log</button>
      <button class="sub-tab-btn" id="sim-view-btn-config" onclick="switchSimView('config')">⚙️ Strategy & Settings</button>
    </div>

    <!-- VIEW 1: EXECUTIVE METRICS & SUMMARY -->
    <div id="sim-view-summary">
      <!-- Live Telemetry Monitor -->
      <div class="grid" style="padding:0;margin-bottom:16px;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px">
        <div class="card"><div class="card-title">Simulator Status</div><div class="card-value" id="sim-val-status" style="font-size:18px">Idle</div><div class="card-sub" id="sim-sub-status">Ready to run</div></div>
        <div class="card"><div class="card-title">Ticks Replayed</div><div class="card-value" id="sim-val-ticks">0</div><div class="card-sub">WebSocket steps</div></div>
        <div class="card"><div class="card-title">Trades Simulated</div><div class="card-value" id="sim-val-trades">0</div><div class="card-sub" id="sim-sub-trades">0 won &middot; 0 lost</div></div>
        <div class="card"><div class="card-title">Win Rate</div><div class="card-value" id="sim-val-wr" style="color:#10b981">—</div><div class="card-sub" id="sim-sub-pf">PF: —</div></div>
        <div class="card"><div class="card-title">AI Inferences</div><div class="card-value" id="sim-val-hf" style="color:#38bdf8">0</div><div class="card-sub" id="sim-sub-hf">AI reviews done</div></div>
        <div class="card"><div class="card-title">Elapsed Time</div><div class="card-value" id="sim-val-time">0s</div><div class="card-sub" id="sim-sub-sym">Symbol: —</div></div>
      </div>

      <!-- Cycle Challenge Scorecard -->
      <div class="card" style="margin-bottom:16px;background:linear-gradient(135deg,rgba(16,185,129,0.08),rgba(56,189,248,0.08));border:1px solid rgba(56,189,248,0.3)">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px">
          <div>
            <div style="font-size:15px;font-weight:700;color:#38bdf8;display:flex;align-items:center;gap:6px">
              <span>🏆 25 USDT ➔ 100 USDT Compounding Challenge</span>
            </div>
            <div style="font-size:11px;color:#94a3b8">Simulated paper trading account cycles. Resets to $25 upon reaching $100 OR upon busting ($0).</div>
          </div>
          <div style="font-size:11px;color:#cbd5e1;background:#0f172a;padding:4px 10px;border-radius:6px;border:1px solid #334155">
            Rule: <b>Margin 25% · 10x Lev · Full Fee Deduction</b>
          </div>
        </div>
        <div class="grid" style="padding:0;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px">
          <div style="background:#0f172a;padding:10px;border-radius:6px;border:1px solid #334155">
            <div style="font-size:10px;color:#94a3b8">TOTAL CYCLES</div>
            <div id="sim-cycle-total" style="font-size:18px;font-weight:700;color:#f1f5f9">—</div>
            <div id="sim-cycle-total-sub" style="font-size:10px;color:#64748b">completed</div>
          </div>
          <div style="background:#0f172a;padding:10px;border-radius:6px;border:1px solid #334155">
            <div style="font-size:10px;color:#94a3b8">TARGET REACHED ($100)</div>
            <div id="sim-cycle-won" style="font-size:18px;font-weight:700;color:#10b981">—</div>
            <div id="sim-cycle-won-sub" style="font-size:10px;color:#10b981">100% goals hit</div>
          </div>
          <div style="background:#0f172a;padding:10px;border-radius:6px;border:1px solid #334155">
            <div style="font-size:10px;color:#94a3b8">BUSTED ($0)</div>
            <div id="sim-cycle-busted" style="font-size:18px;font-weight:700;color:#ef4444">—</div>
            <div id="sim-cycle-busted-sub" style="font-size:10px;color:#ef4444">liquidated/ruined</div>
          </div>
          <div style="background:#0f172a;padding:10px;border-radius:6px;border:1px solid #334155">
            <div style="font-size:10px;color:#94a3b8">CYCLE WIN RATE</div>
            <div id="sim-cycle-wr" style="font-size:18px;font-weight:700;color:#38bdf8">—</div>
            <div id="sim-cycle-wr-sub" style="font-size:10px;color:#64748b">target vs bust</div>
          </div>
          <div style="background:#0f172a;padding:10px;border-radius:6px;border:1px solid #334155">
            <div style="font-size:10px;color:#94a3b8">TOTAL NET PROFIT</div>
            <div id="sim-cycle-profit" style="font-size:18px;font-weight:700;color:#10b981">—</div>
            <div id="sim-cycle-profit-sub" style="font-size:10px;color:#64748b">across all cycles</div>
          </div>
          <div style="background:#0f172a;padding:10px;border-radius:6px;border:1px solid #334155">
            <div style="font-size:10px;color:#94a3b8">AVG TRADES / CYCLE</div>
            <div id="sim-cycle-trades" style="font-size:18px;font-weight:700;color:#e2e8f0">—</div>
            <div id="sim-cycle-trades-sub" style="font-size:10px;color:#64748b">to reach target</div>
          </div>
        </div>
      </div>
    </div>

    <!-- VIEW 2: COMPOUNDING CYCLES (PAGINATED ON-DEMAND) -->
    <div id="sim-view-cycles" style="display:none">
      <div class="card" style="margin-bottom:16px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px">
          <div>
            <div class="card-title" style="margin:0">📜 Cycle-by-Cycle Account Statements</div>
            <div style="font-size:11px;color:#94a3b8">Select an individual cycle to inspect its audit statement without lagging your device.</div>
          </div>
          <div style="display:flex;gap:6px">
            <button onclick="filterCycleStatements('ALL')" class="sub-tab-btn active" id="cs-tab-all">All</button>
            <button onclick="filterCycleStatements('TARGET_REACHED')" class="sub-tab-btn" id="cs-tab-won">Targets Hit (100$)</button>
            <button onclick="filterCycleStatements('BUSTED')" class="sub-tab-btn" id="cs-tab-busted">Busted (0$)</button>
          </div>
        </div>

        <!-- Cycle Selector Dropdown for Mobile -->
        <div style="background:#090d16;padding:12px;border-radius:8px;border:1px solid #1e293b;margin-bottom:12px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <label style="font-size:12px;color:#38bdf8;font-weight:700">Choose Cycle:</label>
          <select id="sim-cycle-picker" onchange="onCyclePickerChange(this.value)" style="flex-grow:1;max-width:360px;background:#0f172a;color:#f1f5f9;border:1px solid #334155;padding:8px 10px;border-radius:6px;font-size:12px">
            <option value="">-- No cycle simulated yet --</option>
          </select>
          <span id="sim-cycle-picker-badge" style="font-size:11px;color:#94a3b8"></span>
        </div>

        <div id="sim-cycle-statements-container">
          <div class="empty">No cycle simulation recorded yet. Run the simulator to generate cycle statements.</div>
        </div>
      </div>
    </div>

    <!-- VIEW 3: LIVE TRADE STREAM (PAGINATED) -->
    <div id="sim-view-trades" style="display:none">
      <div class="card" style="margin-bottom:16px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
          <div class="card-title" style="margin:0">Recent Simulated Trades & Strategy Post-Mortem</div>
          <div style="font-size:11px;color:#94a3b8">Paginated 10 per page for smooth mobile browsing</div>
        </div>
        <div id="sim-trade-stream"><div class="empty">No simulator run active yet. Click "Run Simulator" to begin.</div></div>
      </div>
    </div>

    <!-- VIEW 4: AI EVENTS LOG -->
    <div id="sim-view-events" style="display:none">
      <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px">
          <div>
            <div class="card-title" style="margin:0;color:#38bdf8">📡 AI API Events & Telemetry</div>
            <div style="font-size:11px;color:#94a3b8">External AI API calls log (When AI is disabled, 0 calls are made).</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <span id="ai-events-count-badge" style="font-size:11px;background:#0f172a;color:#38bdf8;padding:4px 8px;border-radius:4px;border:1px solid #334155">0 Calls Logged</span>
            <button onclick="fetchAIEvents(true)" style="background:#1e293b;color:#f1f5f9;border:1px solid #334155;padding:5px 10px;border-radius:6px;cursor:pointer;font-size:11px">🔄 Refresh</button>
          </div>
        </div>
        <div style="overflow-x:auto">
          <table style="width:100%;border-collapse:collapse;font-size:11px;text-align:left">
            <thead>
              <tr style="background:#1e293b;color:#94a3b8">
                <th style="padding:6px 8px">Time (UTC)</th>
                <th style="padding:6px 8px">Provider & Model</th>
                <th style="padding:6px 8px">Call Type</th>
                <th style="padding:6px 8px">Symbol / Setup</th>
                <th style="padding:6px 8px">Latency</th>
                <th style="padding:6px 8px">Status</th>
                <th style="padding:6px 8px">Summary & Reasoning</th>
              </tr>
            </thead>
            <tbody id="sim-ai-events-table">
              <tr><td colspan="7" style="padding:12px;text-align:center;color:#64748b">No AI events logged yet. (AI calls are stopped).</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- VIEW 5: STRATEGY & PARAMETERS SETTINGS -->
    <div id="sim-view-config" style="display:none">
      <!-- Simulator Configuration Card (Strategy, Risk & Compounding) -->
      <div class="card" style="margin-bottom:16px">
        <div class="card-title" style="margin-bottom:10px">Strategy, Protections & Compounding Parameters</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px">
          <div>
            <label style="font-size:11px;color:#38bdf8;display:block;margin-bottom:4px;font-weight:700">🎯 Strategy Selection</label>
            <select id="sim-input-strategy" onchange="saveSimulatorSettings()" style="width:100%;background:#0f172a;color:#38bdf8;border:1px solid #0284c7;padding:8px 10px;border-radius:6px;font-weight:600">
              <option value="all" selected>⚡ All Strategies (Ensemble / Multi-Strategy)</option>
              <option value="confluence">🎯 5-Family Confluence Gate</option>
              <option value="bollinger_squeeze">📊 Bollinger Bands Squeeze Breakout</option>
              <option value="volume_spike">📈 Volume Anomaly Spike & Surge</option>
              <option value="rsi_divergence">🔀 RSI Divergence (Trend Reversals)</option>
              <option value="trend_pullback">🌊 Trend Pullback & Continuation (15m/5m)</option>
              <option value="range_breakout">💥 Momentum Range Breakout</option>
              <option value="sweep_reclaim">🧹 Liquidity Sweep & Reclaim</option>
              <option value="breakout_retest">🔁 Breakout & Retest Confirmation</option>
            </select>
          </div>
          <div>
            <label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:4px">AI Provider (AI API Calls)</label>
            <select id="sim-input-ai-provider" onchange="saveSimulatorSettings()" style="width:100%;background:#0f172a;color:#f1f5f9;border:1px solid #334155;padding:8px 10px;border-radius:6px">
              <option value="none" selected>⛔ Disabled / Offline (0 AI API Calls, Instant, Free)</option>
              <option value="auto">Auto Hybrid (Gemini + Groq + OpenRouter + HF)</option>
              <option value="gemini">Gemini Only (gemini-3-flash)</option>
              <option value="groq">Groq Turbo (qwen3.8-27b)</option>
              <option value="openrouter">OpenRouter Free (DeepSeek-R1)</option>
              <option value="hf">Hugging Face Free (Governed)</option>
            </select>
          </div>
          <div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
              <label style="font-size:11px;color:#94a3b8;font-weight:600">🎯 Target Multiple (R)</label>
              <span id="sim-tp-calc-badge" style="font-size:10px;color:#10b981;font-weight:700">+55.0% ROE · +$55 on $100</span>
            </div>
            <input type="number" step="0.1" id="sim-input-tp" value="2.2" oninput="updateRMathCard()" style="width:100%;background:#0f172a;color:#10b981;border:1px solid #059669;padding:8px 10px;border-radius:6px;font-weight:700">
          </div>
          <div>
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
              <label style="font-size:11px;color:#94a3b8;font-weight:600">🛑 Stop-Loss Multiple (R)</label>
              <span id="sim-sl-calc-badge" style="font-size:10px;color:#ef4444;font-weight:700">-30.0% ROE · -$30 on $100</span>
            </div>
            <input type="number" step="0.1" id="sim-input-sl" value="1.5" oninput="updateRMathCard()" style="width:100%;background:#0f172a;color:#ef4444;border:1px solid #e11d48;padding:8px 10px;border-radius:6px;font-weight:700">
          </div>
          <div>
            <label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:4px">Anti-Flip Cooldown (min)</label>
            <input type="number" id="sim-input-antiflip" value="90" style="width:100%;background:#0f172a;color:#f1f5f9;border:1px solid #334155;padding:8px 10px;border-radius:6px">
          </div>
          <div>
            <label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:4px">Smart Stagnation Exit (min)</label>
            <input type="number" id="sim-input-stagnation" value="60" style="width:100%;background:#0f172a;color:#f1f5f9;border:1px solid #334155;padding:8px 10px;border-radius:6px">
          </div>
          <div>
            <label style="font-size:11px;color:#10b981;display:block;margin-bottom:4px;font-weight:700">💰 Cycle Start Capital ($)</label>
            <input type="number" step="1" id="sim-input-cycle-start" value="25" style="width:100%;background:#0f172a;color:#10b981;border:1px solid #059669;padding:8px 10px;border-radius:6px;font-weight:700">
          </div>
          <div>
            <label style="font-size:11px;color:#38bdf8;display:block;margin-bottom:4px;font-weight:700">🎯 Target Goal Reset ($)</label>
            <input type="number" step="5" id="sim-input-cycle-target" value="100" style="width:100%;background:#0f172a;color:#38bdf8;border:1px solid #0284c7;padding:8px 10px;border-radius:6px;font-weight:700">
          </div>
          <div>
            <label style="font-size:11px;color:#cbd5e1;display:block;margin-bottom:4px">Margin / Trade (%)</label>
            <input type="number" step="5" id="sim-input-cycle-margin" value="25" oninput="updateRMathCard()" style="width:100%;background:#0f172a;color:#f1f5f9;border:1px solid #334155;padding:8px 10px;border-radius:6px">
          </div>
          <div>
            <label style="font-size:11px;color:#cbd5e1;display:block;margin-bottom:4px">Leverage (x)</label>
            <input type="number" step="1" id="sim-input-cycle-lev" value="10" oninput="updateRMathCard()" style="width:100%;background:#0f172a;color:#f1f5f9;border:1px solid #334155;padding:8px 10px;border-radius:6px">
          </div>
          <div>
            <label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:4px">Historical Time Horizon</label>
            <select id="sim-input-years" onchange="updateSimulatorEstimates()" style="width:100%;background:#0f172a;color:#f1f5f9;border:1px solid #334155;padding:8px 10px;border-radius:6px">
              <option value="0.00274">1 Day (24 Hours) - Ultra Fast</option>
              <option value="0.0082" selected>3 Days (72 Hours) - Fast Verification</option>
              <option value="0.0192">1 Week (168 Hours) - Recommended</option>
              <option value="0.0384">2 Weeks (336 Hours) - Comprehensive</option>
              <option value="0.083">1 Month (720 Hours)</option>
              <option value="0.25">3 Months (2,160 Hours)</option>
              <option value="1.0">1 Year (8,760 Hours)</option>
              <option value="3.0">3 Years (26,280 Hours)</option>
            </select>
          </div>
          <div>
            <label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:4px">Market Hours per Step (hrs)</label>
            <input type="number" step="0.5" id="sim-input-step-hours" value="1.0" min="0.1" max="24" oninput="updateSimulatorEstimates()" style="width:100%;background:#0f172a;color:#f1f5f9;border:1px solid #334155;padding:8px 10px;border-radius:6px">
          </div>
          <div>
            <label style="font-size:11px;color:#94a3b8;display:block;margin-bottom:4px">Analysis Budget per Step (sec)</label>
            <input type="number" step="5" id="sim-input-step-seconds" value="10" min="2" max="300" oninput="updateSimulatorEstimates()" style="width:100%;background:#0f172a;color:#f1f5f9;border:1px solid #334155;padding:8px 10px;border-radius:6px">
          </div>
          <div style="display:flex;align-items:flex-end">
            <label style="font-size:12px;color:#10b981;font-weight:700;display:flex;align-items:center;gap:6px;cursor:pointer;padding-bottom:10px">
              <input type="checkbox" id="sim-input-stepped" checked style="accent-color:#10b981;cursor:pointer">
              Stepped Clock Mode
            </label>
          </div>
        </div>
      </div>

      <!-- Live Profit & Loss R Math Breakdown Card ($100 Base Margin) -->
      <div class="card" style="margin-bottom:16px;background:linear-gradient(135deg,#090d16,#0f172a);border:1px solid rgba(16,185,129,0.35);border-radius:10px;padding:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px">
          <div style="font-size:13px;font-weight:700;color:#38bdf8;display:flex;align-items:center;gap:6px">
            <span>💡 R-Multiple Live Math Breakdown (Using $100 Margin Base)</span>
          </div>
          <div style="font-size:10.5px;color:#94a3b8;background:#1e293b;padding:3px 8px;border-radius:4px;border:1px solid #334155">
            Auto-calculates dynamically based on your Target R, Stop R &amp; Leverage
          </div>
        </div>

        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin-bottom:12px">
          <!-- Take Profit Box -->
          <div style="background:rgba(16,185,129,0.06);border:1px solid rgba(16,185,129,0.25);border-radius:8px;padding:10px">
            <div style="font-size:11px;font-weight:700;color:#10b981;margin-bottom:4px">🎯 TAKE PROFIT TARGET</div>
            <div id="sim-math-tp-roe" style="font-size:18px;font-weight:800;color:#10b981">+55.0% ROE</div>
            <div id="sim-math-tp-cash" style="font-size:12px;font-weight:700;color:#f1f5f9;margin-top:2px">+$55.00 USDT profit on $100</div>
            <div id="sim-math-tp-price" style="font-size:10.5px;color:#94a3b8;margin-top:3px">Requires +2.20% price move</div>
          </div>

          <!-- Stop Loss Box -->
          <div style="background:rgba(239,68,68,0.06);border:1px solid rgba(239,68,68,0.25);border-radius:8px;padding:10px">
            <div style="font-size:11px;font-weight:700;color:#ef4444;margin-bottom:4px">🛑 STOP-LOSS RISK</div>
            <div id="sim-math-sl-roe" style="font-size:18px;font-weight:800;color:#ef4444">-30.0% ROE</div>
            <div id="sim-math-sl-cash" style="font-size:12px;font-weight:700;color:#f1f5f9;margin-top:2px">-$30.00 USDT loss on $100</div>
            <div id="sim-math-sl-price" style="font-size:10.5px;color:#94a3b8;margin-top:3px">Hits at -1.20% price move</div>
          </div>

          <!-- Risk / Reward Box -->
          <div style="background:#090d16;border:1px solid #334155;border-radius:8px;padding:10px">
            <div style="font-size:11px;font-weight:700;color:#38bdf8;margin-bottom:4px">⚖️ RISK-TO-REWARD (R:R)</div>
            <div id="sim-math-rr" style="font-size:18px;font-weight:800;color:#38bdf8">1.83 : 1</div>
            <div id="sim-math-rr-desc" style="font-size:12px;color:#cbd5e1;margin-top:2px">Gain $1.83 for every $1.00 risked</div>
            <div id="sim-math-position" style="font-size:10.5px;color:#94a3b8;margin-top:3px">Position size: $1,000 at 10x</div>
          </div>
        </div>

        <div style="font-size:11px;color:#94a3b8;line-height:1.45;background:#030712;padding:8px 12px;border-radius:6px;border:1px solid #1e293b">
          ℹ️ <b>What does 'R' mean?</b> <code>1R</code> is your baseline risk unit (defined as <b>1.2% price move</b> / 1.8x ATR). 
          With <b>10x leverage</b>: <code>1R = 12% ROE ($12 per $100 margin)</code>. At <b>25x leverage</b>: <code>1R = 30% ROE ($30 per $100 margin)</code>.<br>
          • <b>Target Multiple:</b> Multiplier of 1R. E.g. at 2.2R and 10x, you make <code>+26.4% ROE (+$26.40 on $100)</code>.<br>
          • <b>Stop-Loss Multiple:</b> Multiplier of 1R. E.g. at 1.5R and 10x, you risk <code>-18.0% ROE (-$18.00 on $100)</code>.
        </div>
      </div>

      <!-- Live Dynamic Estimator Box -->
      <div class="card" style="margin-bottom:16px;background:linear-gradient(180deg,#1e293b,#0f172a);border:1px solid #38bdf8">
        <div style="font-size:14px;font-weight:700;color:#38bdf8;margin-bottom:10px">⏱️ Run Duration Estimator</div>
        <div style="background:#090d16;border:1px solid #1e293b;border-radius:8px;padding:12px 14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px">
          <div>
            <div style="font-size:10px;color:#94a3b8">TOTAL MARKET STEPS</div>
            <div id="sim-est-steps" style="font-size:17px;font-weight:700;color:#38bdf8">72 Steps</div>
            <div style="font-size:10px;color:#64748b" id="sim-est-hours">72 market hours</div>
          </div>
          <div>
            <div style="font-size:10px;color:#94a3b8">ESTIMATED RUN TIME</div>
            <div id="sim-est-time" style="font-size:17px;font-weight:700;color:#10b981">12 mins</div>
            <div style="font-size:10px;color:#64748b">at 10s / step</div>
          </div>
          <div>
            <div style="font-size:10px;color:#94a3b8">EXPECTED COMPLETION</div>
            <div id="sim-est-finish" style="font-size:17px;font-weight:700;color:#f59e0b">Calculating...</div>
            <div style="font-size:10px;color:#64748b">based on local clock</div>
          </div>
        </div>
      </div>
    </div>
  </section>
</div>

  </div>
</div>
</div>
<footer>Auto-refreshes every 30s &middot; <span id="last-updated">&mdash;</span> &middot; <a href="/data" style="color:#38bdf8;text-decoration:none">🗄️ DB Dump</a> &middot; <a href="/settings" style="color:#3fb950;text-decoration:none">⚙️ Settings</a> &middot; <a href="/api/debug" style="color:#a78bfa;text-decoration:none">🔬 Debug</a></footer>

<script>

function fmtUptime(s){if(s==null||isNaN(s))return '—';if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h+'h '+m+'m';}
const _IST={timeZone:'Asia/Kolkata'};
function fmtTime(iso){ return window.fmtStamp(iso); }

function esc(s){
  const d=document.createElement('div');
  d.textContent=s||'';
  return d.innerHTML;
}

// ── TAB SWITCHING ─────────────────────────────────────────────────────────────

// ── PAPER TRADING ─────────────────────────────────────────────────────────────
const PAPER_REASON = {
  target:'hit target', stop:'stopped out', liquidation:'LIQUIDATED',
  expiry:'time expired', cycle_end:'cycle closed', signal_flip:'setup reversed',
  conviction_lost:'conviction faded', market_shock:'market shock'
};
function money(v){
  const sign = v < 0 ? '-' : '';
  return sign + '₹' + Math.abs(v).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2});
}
function signed(v){ return (v>=0?'+':'') + money(v).replace('-',''); }
function pnlClass(v){ return v>0?'pos':(v<0?'neg':''); }

// ── Paper trading desk ──────────────────────────────────────────────────
// One fetch feeds three views: the cycle strip, open positions and a
// filtered history whose summary recomputes against whatever is showing —
// so "how does volume_spike actually do" is a two-click question rather
// than a database query.
let _pt = null;                       // last payload, kept so filtering and a
                                      // currency change re-render without refetching
const _ptF = {sym:'', side:'', setup:'', exit:'', win:false, loss:false, q:''};
let _ptSort = {key:'closed_at', dir:-1};

function _ptMoney(v, rate, signed){ return window.Money.fmt(v, rate, signed); }
function _ptCls(n){ return n > 0 ? 'pt-up' : n < 0 ? 'pt-down' : ''; }
function _ptQty(q){
  if(!q) return '0';
  const dp = q >= 1000 ? 2 : q >= 1 ? 4 : 6;
  return q.toFixed(dp);
}
// A position past its own expiry is still open only if nothing resolved it.
// Saying "overdue" is the difference between noticing that and not.
function _ptExpiry(iso){
  if(!iso) return '<span class="pt-down">no expiry</span>';
  const mins = (new Date(iso) - Date.now()) / 60000;
  if(mins < 0) return `<span class="pt-down">overdue ${Math.round(-mins)}m</span>`;
  return `<span class="pt-muted">in ${Math.round(mins)}m</span>`;
}

async function loadRegimeCard(){
  /* Shadow test of the market filter: closed swing trades split by what the filter would have done. */
  const el = document.getElementById('regime-card'); if(!el) return;
  let d; try { d = await (await fetch('/api/swing')).json(); } catch(e){ return; }
  renderRegistryCard(d.registry || []);
  const g = d.regime_filter; if(!g){ el.innerHTML=''; return; }
  const sb = g.signals || g.scoreboard || {take:{n:0},skip:{n:0}}, now = g.now || {};
  const r = x => x==null ? '–' : (x>0?'+':'') + x.toFixed(2) + ' R';
  const row = (label, x, hint) => `<tr><td>${label}</td><td style="text-align:right">${x.n}</td><td style="text-align:right">${x.n?Math.round(100*x.wins/x.n)+'%':'–'}</td><td style="text-align:right" class="${x.avg_r>0?'pos':x.avg_r<0?'neg':''}">${r(x.avg_r)}</td><td class="pt-muted">${hint}</td></tr>`;
  const rank = now.btc_vol_rank;
  const state = rank==null ? 'not measured yet' : rank > g.vol_rank_max ? `<b class="neg">wild</b> (${Math.round(rank*100)}th percentile of the past year)` : `<b class="pos">calm or normal</b> (${Math.round(rank*100)}th percentile of the past year)`;
  el.innerHTML = `<div class="cr-note"><b>Market filter · ${g.mode==='on'?'ON (skipping)':g.mode==='shadow'?'shadow test (trading everything, recording what it would skip)':'off'}</b><br>
  Skips a swing signal when Bitcoin's 30-day volatility is in the top third of its past year${g.adx_max?`, or the coin's daily ADX is above ${g.adx_max}`:''}. Bitcoin right now: ${state}.
  <table class="tbl" style="margin-top:8px"><thead><tr><th>swing signals, replayed to their stop/target/7 days</th><th>n</th><th>won</th><th>avg</th><th>backtest expects</th></tr></thead><tbody>
  ${row(g.mode==='on'?'taken':'filter would take', sb.take, '+0.36 R')}${row(g.mode==='on'?'skipped by the filter':'filter would skip', sb.skip, 'about 0 or worse')}</tbody></table>
  <span class="pt-muted">${(sb.take.running||0)+(sb.skip.running||0)} still running. Every signal is scored the same way, traded or not, so "skipped" shows what the filter saved or cost. Judge after 30+ in each row.</span></div>`;
}

function renderRegistryCard(rows){
  /* Plan 2.3: each swing strategy's forward clock. Only results after "live from" are clean evidence. */
  const el = document.getElementById('registry-card'); if(!el) return;
  if(!rows.length){ el.innerHTML=''; return; }
  const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const st = s => s==='paused' ? '<b class="neg">paused</b>' : s==='retired' ? '<span class="pt-muted">retired</span>'
                 : s==='trusted' ? '<b class="pos">trusted</b>' : 'incubating';
  const r = x => x==null ? '–' : (x>0?'+':'') + Number(x).toFixed(2) + ' R';
  const day = iso => iso ? fmtStamp(iso) : '<span class="pt-muted">no trade yet</span>';
  const bar = (dd, lim) => { const w = Math.min(100, lim ? 100*dd/lim : 0);
    return `<div title="drawdown ${dd} R of ${lim} R alarm" style="height:6px;background:var(--line,#ddd);border-radius:3px;margin-top:3px">`
         + `<div style="width:${w}%;height:6px;border-radius:3px;background:${w>=75?'#c0392b':w>=40?'#d68910':'#2e86c1'}"></div></div>`; };
  el.innerHTML = `<div class="cr-note"><b>Strategies · forward evidence</b><br>
  Each swing strategy's clock starts at its first paper trade ("live from") and restarts if its code or settings change.
  Only these results are clean: the backtest picked the strategies on the same history it tested them on.
  A strategy pauses itself if it falls further below its best than it ever did in 5 years of testing.
  <div style="margin-top:8px">${rows.map(x => `<div title="${esc(x.note)}" style="padding:8px 0;border-top:1px solid var(--line,#3333)">
    <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><span><b>${esc(x.spec)}</b> <span class="pt-muted">${esc(x.params)}</span></span><span>${st(x.status)}</span></div>
    <div class="pt-muted" style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap">
      <span>live from ${day(x.live_from)} · ${x.forward_trades} trades · <span class="${x.forward_total_r>0?'pos':x.forward_total_r<0?'neg':''}">${r(x.forward_total_r)}</span>${x.forward_avg_r==null?'':' (avg '+r(x.forward_avg_r)+')'}</span>
      <span>backtest avg ${r(x.backtest_avg_r)}</span></div>
    <div class="pt-muted" style="font-size:12px">drawdown ${x.forward_drawdown_r} of ${x.alarm_at_r} R alarm${bar(x.forward_drawdown_r, x.alarm_at_r)}</div></div>`).join('')}</div>
  <span class="pt-muted">Judge a strategy after ~30 forward trades; until then its average can swing by more than its whole edge.</span></div>`;
}

async function loadPaper(){
  let d;
  try { d = await (await fetch('/api/paper')).json(); }
  catch(e){
    document.getElementById('paper-banner').innerHTML =
      '<div class="cr-sig-warn">Could not reach /api/paper.</div>';
    return;
  }
  _pt = d;
  renderPaper();
  loadRegimeCard();
}

function renderPaper(){
  const d = _pt;
  if(!d) return;
  const banner = document.getElementById('paper-banner');

  if((d.running_cycles || []).length > 1){
    banner.innerHTML = '<div class="cr-sig-warn">Two paper cycles are running ('
      + d.running_cycles.join(', ') + '). Trades are split between them, so this '
      + 'page is showing only one. The engine retires the duplicate on its next '
      + 'tick — check for a second crypto-engine process.</div>';
  } else if(!d.enabled){
    banner.innerHTML = '<div class="cr-sig-warn">Paper trading is switched off — '
      + 'turn it on in <a href="/settings">Settings</a>.</div>';
  } else if(!d.running){
    banner.innerHTML = '<div class="cr-note">No cycle running — one starts on the next tick.</div>';
  } else { banner.innerHTML = ''; }

  if(!d.running){
    document.getElementById('paper-strip').innerHTML = '';
    document.getElementById('paper-positions').innerHTML = '<div class="empty">No open positions</div>';
    document.getElementById('paper-trades').innerHTML = '<div class="empty">No trades yet</div>';
    document.getElementById('paper-scorecard').innerHTML = '';
    document.getElementById('pt-open-count').textContent = '';
    document.getElementById('pt-hist-count').textContent = '';
    return;
  }

  const c = d.cycle, rate = d.usdt_inr || 102;
  const margin = (d.positions || []).reduce((a, p) => a + p.margin, 0);
  // Margin locked in an open position has left the wallet but has not been
  // lost. Measuring realised P&L as wallet-minus-start counts it as a loss:
  // a 554 margin made a -252 cycle read as -806, three times worse than the
  // truth, and the error grows with every position left open.
  const realised = c.wallet + margin - c.starting_wallet;
  const span = Math.max(1, c.target_wallet - c.starting_wallet);
  const pct = Math.max(0, Math.min(100, (c.wallet - c.starting_wallet) / span * 100));

  document.getElementById('paper-strip').innerHTML = `
    <div class="pt-cell"><div class="pt-k">Equity</div>
      <div class="pt-v">${_ptMoney(d.equity, rate)}</div></div>
    <div class="pt-cell"><div class="pt-k">Wallet</div>
      <div class="pt-v">${_ptMoney(c.wallet, rate)}</div></div>
    <div class="pt-cell"><div class="pt-k">Unrealised</div>
      <div class="pt-v ${_ptCls(d.unrealised)}">${_ptMoney(d.unrealised, rate, true)}</div></div>
    <div class="pt-cell"><div class="pt-k">Realised P&amp;L</div>
      <div class="pt-v ${_ptCls(realised)}">${_ptMoney(realised, rate, true)}</div></div>
    <div class="pt-cell"><div class="pt-k">Margin in use</div>
      <div class="pt-v">${_ptMoney(margin, rate)}<small> · ${(d.positions||[]).length} open</small></div></div>
    ${(() => { const w = d.withdrawals || {}, rule = w.rule || {};
      if(!rule.at) return `<div class="pt-cell"><div class="pt-k">Cycle ${c.id} · ${c.leverage}&times;</div>
      <div class="pt-v" style="font-size:13px">${_ptMoney(c.starting_wallet, rate)} &rarr; ${_ptMoney(c.target_wallet, rate)}</div>
      <div class="pt-rail"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="pt-railcap"><span>${pct.toFixed(0)}% there</span>`;
      const p2 = Math.max(0, Math.min(100, (d.equity - c.starting_wallet) / Math.max(1, rule.at - c.starting_wallet) * 100));
      return `<div class="pt-cell"><div class="pt-k">Withdrawn · cycle ${c.id}</div>
      <div class="pt-v">${_ptMoney(w.total || 0, rate)}<small> · ${w.count || 0}&times;</small></div>
      <div class="pt-rail"><i style="width:${p2.toFixed(1)}%"></i></div>
      <div class="pt-railcap"><span>next ${_ptMoney(rule.amount, rate)} at ${_ptMoney(rule.at, rate)} · ${p2.toFixed(0)}%</span>`; })()}
        <span>${window.Money.get() === 'INR' ? 'figures in &#8377;' : '1 USDT = &#8377;' + rate}</span></div></div>`;

  renderPaperPositions(d.positions || [], rate);
  renderPaperHistory();
}

// Per-trade change log (owner's request, 26 Sep): opened, stop moves, trail,
// target released, AI reviews and votes, close. Open logs survive the 30 s
// refresh because their state lives here, not in the DOM.
let _ptLogs = {};
function ptLogKey(sym, at){ return sym + '|' + at; }
function ptLogBtn(sym, at){
  if(!at) return '';
  const k = ptLogKey(sym, at);
  return '<button type="button" class="pt-logbtn" onclick="ptLogToggle(\''+k+'\')">'
    + (_ptLogs[k] !== undefined ? 'Hide log' : 'Log') + '</button>';
}
async function ptLogToggle(k){
  if(_ptLogs[k] !== undefined){ delete _ptLogs[k]; renderPaper(); return; }
  _ptLogs[k] = null; renderPaper();
  const [sym, at] = k.split('|');
  try{
    const r = await fetch('/api/paper/events?symbol=' + encodeURIComponent(sym)
                          + '&opened_at=' + encodeURIComponent(at));
    _ptLogs[k] = r.ok ? (await r.json()).events : [];
  }catch(e){ _ptLogs[k] = []; }
  renderPaper();
}
function ptLogRow(sym, at, cols){
  const k = ptLogKey(sym, at), ev = _ptLogs[k];
  if(ev === undefined) return '';
  const body = ev === null ? '<div class="pt-muted">Loading…</div>'
    : !ev.length ? '<div class="pt-muted">No changes logged for this trade (trades opened '
      + 'before the log existed have none).</div>'
    : '<table class="pt-log"><tr><th>When (IST)</th><th>What</th><th>Change</th><th>Details</th></tr>'
      + ev.map(e => '<tr><td>' + fmtStamp(e.at, {seconds:true}) + '<br><span class="pt-muted">'
        + fmtAgo(e.at) + '</span></td><td><span class="pt-tag">' + e.kind + '</span>'
        + (e.field && e.field !== e.kind ? ' ' + e.field : '') + '</td><td class="pt-num">'
        + (e.old ? e.old + ' &rarr; ' : '') + (e.new || '') + '</td><td>'
        + String(e.note || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))
        + '</td></tr>').join('') + '</table>';
  return '<tr class="pt-logrow"><td colspan="' + cols + '">' + body + '</td></tr>';
}

function renderPaperPositions(rows, rate){
  const el = document.getElementById('paper-positions');
  document.getElementById('pt-open-count').textContent =
    rows.length ? rows.length + ' open' : '';
  if(!rows.length){ el.innerHTML = '<div class="empty">No open positions</div>'; return; }

  el.innerHTML = '<table class="tbl"><thead><tr>'
    + '<th>Market</th><th class="r">Quantity</th><th class="r">Entry</th><th class="r">Mark</th>'
    + '<th>Stop &middot; Target</th><th class="r">Margin</th><th class="r">Liq.</th>'
    + '<th class="r">Unrealised</th><th class="r">ROE</th><th class="r">Opened</th>'
    + '<th class="r">Expires</th>'
    + '</tr></thead><tbody>'
    + rows.map(p => {
        const long = p.side === 'long';
        const lo = Math.min(p.stop, p.target), hi = Math.max(p.stop, p.target);
        const span = Math.max(1e-9, hi - lo);
        const at = x => Math.max(0, Math.min(100, (x - lo) / span * 100));
        const eAt = at(p.entry), mAt = at(p.mark);
        const good = long ? p.mark >= p.entry : p.mark <= p.entry;
        const col = good ? 'var(--pos)' : 'var(--neg)';
        const fillL = Math.min(eAt, mAt), fillW = Math.abs(mAt - eAt);
        const unit = p.symbol.replace(/USDT$/, '');
        const isIntraday = (p.trade_mode || 'intraday') === 'intraday';
        const modeBadge = isIntraday 
          ? `<span class="pt-mode intra">⚡ Intra ${p.leverage || 10}x</span>`
          : `<span class="pt-mode deliv">📦 Delivery</span>`;
        const partialBadge = p.partial_closed
          ? `<span class="pt-partial" title="Partial profit of +₹${p.partial_pnl} taken at TP1">TP1 HIT (50% Closed)</span>`
          : '';
        const trailBadge = p.trailing ? '<span class="pt-trail">🔒 Trail Locked</span>' : '';
        return `<tr>
          <td><span class="pt-sym">${p.symbol}</span>`
          + `<span class="pt-side ${long ? 'l' : 's'}">${p.side.toUpperCase()}</span>`
          + modeBadge
          + partialBadge
          + trailBadge
          + `<span class="pt-setup">${(p.signal_type||'').replace(/_/g,' ')} &middot; ${p.confidence}%</span>`
          + ptLogBtn(p.symbol, p.opened_at) + `</td>
          <td class="r pt-num">${_ptQty(p.qty)} <span class="pt-unit">${unit}</span>
            <span class="pt-notional">${_ptMoney(p.notional, rate)}</span></td>
          <td class="r pt-num">${fmtPrice(p.entry)}</td>
          <td class="r pt-num ${good ? 'pt-up' : 'pt-down'}">${fmtPrice(p.mark)}</td>
          <td><div class="pt-prail">
              <span class="pt-ptrack"></span>
              <span class="pt-pfill" style="left:${fillL}%;width:${fillW}%;background:${col}"></span>
              <span class="pt-ptick" style="left:${eAt}%"></span>
              <span class="pt-pmark" style="left:${mAt}%;background:${col}"></span>
              <span class="pt-plab" style="left:0">${fmtPrice(long ? p.stop : p.target)}</span>
              <span class="pt-plab" style="right:0">${fmtPrice(long ? p.target : p.stop)}</span>
            </div></td>
          <td class="r pt-num">${_ptMoney(p.margin, rate)}</td>
          <td class="r pt-num pt-muted">${fmtPrice(p.liq)}</td>
          <td class="r pt-num ${_ptCls(p.unrealised)}">${_ptMoney(p.unrealised, rate, true)}</td>
          <td class="r pt-num ${_ptCls(p.roe_pct)}">${p.roe_pct > 0 ? '+' : ''}${p.roe_pct}%</td>
          <td class="r pt-num pt-muted">${fmtTime(p.opened_at)}</td>
          <td class="r pt-num">${_ptExpiry(p.expires_at)}</td>
        </tr>` + ptLogRow(p.symbol, p.opened_at, 11);
      }).join('')
    + '</tbody></table>';
}

function _ptFill(id, values){
  const sel = document.getElementById(id);
  if(!sel) return;
  const keep = sel.value;
  const first = sel.options[0];
  sel.innerHTML = '';
  sel.appendChild(first);
  values.forEach(v => {
    const o = document.createElement('option');
    o.value = v; o.textContent = String(v).replace(/_/g, ' ');
    sel.appendChild(o);
  });
  if(values.includes(keep)) sel.value = keep;
}

function renderPaperHistory(){
  const d = _pt;
  if(!d || !d.running) return;
  const all = d.trades || [], rate = d.usdt_inr || 102;

  _ptFill('pf-sym',   [...new Set(all.map(t => t.symbol))].sort());
  _ptFill('pf-setup', [...new Set(all.map(t => t.signal_type).filter(Boolean))].sort());
  _ptFill('pf-exit',  [...new Set(all.map(t => t.reason).filter(Boolean))].sort());

  const q = _ptF.q.toLowerCase();
  let rows = all.filter(t =>
    (!_ptF.sym   || t.symbol === _ptF.sym) &&
    (!_ptF.side  || t.side === _ptF.side) &&
    (!_ptF.setup || t.signal_type === _ptF.setup) &&
    (!_ptF.exit  || t.reason === _ptF.exit) &&
    (!_ptF.win   || t.net > 0) &&
    (!_ptF.loss  || t.net <= 0) &&
    (!q || ((t.symbol + ' ' + (t.signal_type||'') + ' ' + (t.reason||'')).toLowerCase().includes(q)))
  );

  const k = _ptSort.key, dir = _ptSort.dir;
  rows = rows.slice().sort((a, b) => {
    const x = a[k], y = b[k];
    if(x === y) return 0;
    return (x > y ? 1 : -1) * dir;
  });

  document.getElementById('pt-hist-count').textContent =
    `showing ${rows.length} of ${all.length} closed`;

  const th = (key, label, right) =>
    `<th class="sortable${right ? ' r' : ''}" data-sort="${key}">${label}`
    + (k === key ? `<span class="arw">${dir > 0 ? '&uarr;' : '&darr;'}</span>` : '') + '</th>';

  const el = document.getElementById('paper-trades');
  el.innerHTML = rows.length
    ? '<table class="tbl"><thead><tr>'
      + th('closed_at','Closed') + '<th>Market</th>'
      + '<th class="r">Quantity</th><th class="r">Entry &rarr; Exit</th>'
      + th('reason','Exit') + th('gross','Gross',1) + '<th class="r">Fees</th>'
      + '<th class="r">Funding</th>' + th('net','Net',1) + th('roe_pct','ROE',1)
      + th('hours_held','Held',1) + '<th class="r">Free cash</th>'
      + '</tr></thead><tbody>'
      + rows.map(t => {
          const r = t.usdt_inr || rate;
          const unit = t.symbol.replace(/USDT$/, '');
          const mode = t.trade_mode || 'intraday';
          const modeBadge = mode === 'delivery' 
            ? '<span class="pt-mode deliv">📦 Deliv</span>'
            : '<span class="pt-mode intra">⚡ Intra</span>';
          const partialBadge = t.partial_pnl 
            ? `<span class="pt-partial" title="TP1 Partial: +₹${t.partial_pnl}">+TP1</span>`
            : '';
          return `<tr>
            <td class="pt-num pt-muted">${fmtTime(t.closed_at)}</td>
            <td><span class="pt-sym">${t.symbol}</span>`
            + `<span class="pt-side ${t.side === 'long' ? 'l' : 's'}">${t.side.toUpperCase()}</span>`
            + modeBadge
            + partialBadge
            + `<span class="pt-setup">${(t.signal_type||'').replace(/_/g,' ')} &middot; ${t.confidence}%</span>`
            + ptLogBtn(t.symbol, t.opened_at) + `</td>
            <td class="r pt-num">${_ptQty(t.qty)} <span class="pt-unit">${unit}</span></td>
            <td class="r pt-num">${fmtPrice(t.entry)} <span class="pt-muted">&rarr;</span> ${fmtPrice(t.exit)}</td>
            <td><span class="pt-tag ${t.reason}">${t.reason}</span></td>
            <td class="r pt-num ${_ptCls(t.gross)}">${_ptMoney(t.gross, r, true)}</td>
            <td class="r pt-num pt-muted">${_ptMoney(-t.fees, r)}</td>
            <td class="r pt-num pt-muted">${_ptMoney(-t.funding, r)}</td>
            <td class="r pt-num ${_ptCls(t.net)}" style="font-weight:600">${_ptMoney(t.net, r, true)}</td>
            <td class="r pt-num ${_ptCls(t.roe_pct)}">${t.roe_pct > 0 ? '+' : ''}${t.roe_pct}%</td>
            <td class="r pt-num">${t.hours_held < 1 ? Math.round(t.hours_held*60) + 'm' : t.hours_held + 'h'}</td>
            <td class="r pt-num pt-muted">${_ptMoney(t.wallet_after, r)}</td>
          </tr>` + ptLogRow(t.symbol, t.opened_at, 12);
        }).join('')
      + '</tbody></table>'
    : '<div class="empty">No trades match these filters</div>';

  el.querySelectorAll('th.sortable').forEach(h => h.addEventListener('click', () => {
    const key = h.dataset.sort;
    _ptSort = {key: key, dir: _ptSort.key === key ? -_ptSort.dir : -1};
    renderPaperHistory();
  }));

  const filtered = !!(_ptF.sym || _ptF.side || _ptF.setup || _ptF.exit
                      || _ptF.win || _ptF.loss || _ptF.q);
  renderPaperSummary(rows, rate, filtered);
}

function renderPaperSummary(rows, rate, filtered){
  const el = document.getElementById('paper-scorecard');
  if(!rows.length){ el.innerHTML = ''; return; }
  const n = rows.length;
  const wins = rows.filter(t => t.net > 0), losses = rows.filter(t => t.net <= 0);
  const net = rows.reduce((a,t) => a + t.net, 0);
  const gross = rows.reduce((a,t) => a + t.gross, 0);
  const grossWon = rows.reduce((a,t) => a + Math.max(0, t.gross), 0);
  const fees = rows.reduce((a,t) => a + t.fees, 0);
  const funding = rows.reduce((a,t) => a + t.funding, 0);
  const avgW = wins.length ? wins.reduce((a,t)=>a+t.net,0)/wins.length : 0;
  const avgL = losses.length ? losses.reduce((a,t)=>a+t.net,0)/losses.length : 0;
  let streak = 0, worst = 0;
  rows.slice().sort((a,b) => (a.closed_at > b.closed_at ? 1 : -1))
      .forEach(t => { streak = t.net <= 0 ? streak+1 : 0; worst = Math.max(worst, streak); });

  // Unfiltered, the server's figure wins: /api/paper returns only the most
  // recent trades, so a ratio computed here would quietly describe a window
  // rather than the cycle. Filtered, the window IS the question being asked.
  const srv = (_pt && _pt.summary) ? _pt.summary.costs_as_pct_of_gross : null;
  const costRatio = (!filtered && srv !== null && srv !== undefined)
    ? srv.toFixed(1) + '%'
    : (grossWon > 0 ? ((fees + funding) / grossWon * 100).toFixed(1) + '%' : '—');

  const cell = (k, v, cls) =>
    `<div class="pt-cell"><div class="pt-k">${k}</div><div class="pt-v ${cls||''}">${v}</div></div>`;
  el.innerHTML =
      cell('Win rate', (wins.length / n * 100).toFixed(1) + '%')
    + cell('Gross P&amp;L', _ptMoney(gross, rate, true), _ptCls(gross))
    + cell('Trading fees', _ptMoney(-fees, rate), 'pt-down')
    + cell('Funding', _ptMoney(-funding, rate), 'pt-down')
    + cell('Net P&amp;L', _ptMoney(net, rate, true), _ptCls(net))
    + cell('Expectancy', _ptMoney(net / n, rate, true), _ptCls(net))
    + cell('Realised R:R', avgL ? Math.abs(avgW/avgL).toFixed(2) : '—')
    + cell('Costs / gross', costRatio)
    + cell('Worst streak', worst);
}

(function wirePaperFilters(){
  const bind = (id, key) => {
    const el = document.getElementById(id);
    if(el) el.addEventListener('input', () => { _ptF[key] = el.value; renderPaperHistory(); });
  };
  bind('pf-sym','sym'); bind('pf-side','side'); bind('pf-setup','setup');
  bind('pf-exit','exit'); bind('pf-q','q');
  [['pf-win','win','pf-loss','loss'], ['pf-loss','loss','pf-win','win']]
    .forEach(([id, key, otherId, otherKey]) => {
      const b = document.getElementById(id);
      if(!b) return;
      b.addEventListener('click', () => {
        _ptF[key] = !_ptF[key];
        b.setAttribute('aria-pressed', String(_ptF[key]));
        if(_ptF[key]){
          _ptF[otherKey] = false;
          document.getElementById(otherId).setAttribute('aria-pressed','false');
        }
        renderPaperHistory();
      });
    });
  const reset = document.getElementById('pf-reset');
  if(reset) reset.addEventListener('click', () => {
    Object.assign(_ptF, {sym:'',side:'',setup:'',exit:'',win:false,loss:false,q:''});
    ['pf-sym','pf-side','pf-setup','pf-exit','pf-q'].forEach(i => {
      const e = document.getElementById(i); if(e) e.value = '';
    });
    ['pf-win','pf-loss'].forEach(i => {
      const e = document.getElementById(i); if(e) e.setAttribute('aria-pressed','false');
    });
    renderPaperHistory();
  });
  // A currency change is a repaint, never a refetch.
  document.addEventListener('ccychange', () => { if(_pt) renderPaper(); });
})();


// Copy each table's column names onto its cells so the phone layout can show
// them beside the values. Cheap, idempotent, and keeps every table builder
// free of presentation concerns.
// labelTables and its observer now live in the shared theme snippet, so the
// four other pages get them too. A second copy here meant two observers
// walking the same DOM on every mutation.

// ── SIDEBAR VIEWS ─────────────────────────────────────────────────────────
// Everything below computes from endpoints that already exist. Where a number
// genuinely cannot be produced yet it says so rather than showing a zero.

function card(label,value,sub,cls){
  return `<div class="card"><div class="card-title">${label}</div>
    <div class="card-value ${cls||''}">${value}</div><div class="card-sub">${sub||''}</div></div>`;
}
function moveOf(s){
  return (s.target_price && s.current_price)
    ? Math.abs(s.target_price - s.current_price) / s.current_price * 100 : 0;
}
function xCost(s){ return moveOf(s) / BREAK_EVEN_PCT; }

async function loadDashboard(){
  const [sigs,paper] = await Promise.all([jget('/api/signals/history?days=7',[]), jget('/api/paper',{})]);
  const taken = sigs.filter(s=>xCost(s) >= MIN_TARGET_PCT/BREAK_EVEN_PCT);
  const eq = paper.running ? paper.equity : null;
  const s = paper.summary || {};
  document.getElementById('dash-cards').innerHTML =
      card('Equity', eq==null?'&mdash;':money(eq),
           paper.running?`from ${money(paper.cycle.starting_wallet)}`:'no cycle running',
           eq!=null && paper.running && eq>=paper.cycle.starting_wallet?'pos':'')
    + card('Signals, 7d', sigs.length, `${taken.length} viable · ${sigs.length-taken.length} refused`)
    + card('Hit rate, 7d', s.win_rate_pct!=null?s.win_rate_pct+'%':'&mdash;',
           s.trades?`${s.trades} closed trades`:'needs closed trades')
    + card('Costs, 7d', s.trading_fees!=null?money(s.trading_fees+s.funding_paid):'&mdash;',
           s.costs_as_pct_of_gross!=null?s.costs_as_pct_of_gross+'% of gross':'', 'neg');

  document.getElementById('dash-signals').innerHTML = sigs.length ? `
    <div class="scroll"><table class="tbl"><thead><tr>
      <th>Fired</th><th>Symbol</th><th>Setup</th><th>Dir</th><th>Mode</th><th>Move</th><th>&times; cost</th><th>Conf</th><th>Status</th>
    </tr></thead><tbody>` + sigs.slice(0,40).map(x=>{
      const m=moveOf(x), xc=xCost(x), ok=m>=MIN_TARGET_PCT;
      const isDeliv = (x.trade_mode || 'intraday') === 'delivery';
      const modeBadge = isDeliv ? '<span class="pt-mode deliv">📦 Deliv</span>' : '<span class="pt-mode intra">⚡ Intra</span>';
      const statusBadge = x.veto_reason 
        ? `<span class="pt-tag stop" title="${esc(x.veto_reason)}">VETOED</span>`
        : (ok ? '<span class="pt-tag target">VIABLE</span>' : '<span class="pt-tag expired">SUB-COST</span>');
      return `<tr>
        <td class="sub">${fmtSignalTime(x.timestamp)}</td>
        <td><b>${esc(x.symbol)}</b></td>
        <td>${esc(CR_SIG_NAME[x.signal_type]||x.signal_type)}</td>
        <td class="${x.direction==='long'?'pos':'neg'}">${x.direction.toUpperCase()}</td>
        <td>${modeBadge}</td>
        <td>${m.toFixed(3)}%</td>
        <td class="${ok?'pos':'neg'}">${xc.toFixed(1)}&times;</td>
        <td>${x.confidence}%</td>
        <td>${statusBadge}</td></tr>`;
    }).join('') + '</tbody></table></div>'
    : '<div class="empty">No signals in the last 7 days</div>';

  const refused = {};
  sigs.forEach(x=>{ 
    if(moveOf(x) < MIN_TARGET_PCT) refused['Below cost floor (<0.4%)'] = (refused['Below cost floor (<0.4%)']||0)+1; 
    if(x.veto_reason) refused[x.veto_reason] = (refused[x.veto_reason]||0)+1;
    else if(x.skip_reason) refused[x.skip_reason_text||x.skip_reason] = (refused[x.skip_reason_text||x.skip_reason]||0)+1;
  });
  const rows = Object.entries(refused);
  document.getElementById('dash-refused').innerHTML = rows.length
    ? rows.map(([k,v])=>`<div style="display:flex;align-items:center;gap:10px;padding:4px 0">
        <div style="height:6px;background:#334155;border-radius:3px;width:${Math.min(240,v*12)}px"></div>
        <span style="font-size:11px;color:#94a3b8">${esc(k)} · ${v}</span></div>`).join('')
    : '<div class="empty">Nothing refused in this window</div>';

  const vetoed = sigs.filter(s => s.veto_reason || (s.skip_reason && s.skip_reason !== 'paper_off'));
  const vetoEl = document.getElementById('dash-veto-log');
  if (vetoEl) {
    vetoEl.innerHTML = vetoed.length ? `
      <div class="scroll"><table class="tbl"><thead><tr>
        <th>Time</th><th>Symbol</th><th>Dir</th><th>Mode</th><th>Gate / Reason</th><th>Action</th>
      </tr></thead><tbody>` + vetoed.slice(0, 30).map(x => {
        const isDeliv = (x.trade_mode || 'intraday') === 'delivery';
        const modeBadge = isDeliv ? '<span class="pt-mode deliv">📦 Delivery</span>' : '<span class="pt-mode intra">⚡ Intraday</span>';
        const reason = x.veto_reason || x.skip_reason_text || x.skip_reason || 'Filtered';
        return `<tr>
          <td class="sub">${fmtSignalTime(x.timestamp)}</td>
          <td><b>${esc(x.symbol)}</b></td>
          <td class="${x.direction==='long'?'pos':'neg'}">${x.direction ? x.direction.toUpperCase() : '—'}</td>
          <td>${modeBadge}</td>
          <td><span class="pt-tag stop">${esc(reason)}</span></td>
          <td class="neg" style="font-weight:600">Dropped / Cooldown</td>
        </tr>`;
      }).join('') + '</tbody></table></div>'
      : '<div class="empty">No recent vetoes or filtered signals</div>';
  }
}

async function loadWatchlist(){
  const coins = await jget('/api/crypto/coins',[]);
  const el = document.getElementById('wl-coins');
  const prev = document.getElementById('cr-coins');
  renderCryptoCoins.call(null, coins);
  el.innerHTML = prev ? prev.innerHTML : '<div class="empty">No symbols</div>';
}

async function loadGuard(){
  const paper = await jget('/api/paper',{});
  const trades = (paper.trades||[]).slice().reverse();   // oldest first
  if(!trades.length){
    document.getElementById('guard-cards').innerHTML='';
    document.getElementById('guard-flags').innerHTML='<div class="empty">No closed trades yet — flags appear once a session has trades to compare.</div>';
    document.getElementById('guard-drift').innerHTML='';
    return;
  }
  const notional = t => Math.abs(t.margin) * (t.leverage||1);
  const kept = t => t.gross ? (t.net/t.gross*100) : null;
  const first = trades[0], last = trades[trades.length-1];

  const sizeDrift = notional(last)/notional(first)-1;
  const keptFirst = kept(first), keptLast = kept(last);
  document.getElementById('guard-cards').innerHTML =
      card('Trades', trades.length, 'this cycle')
    + card('Size trend', (sizeDrift>=0?'+':'')+(sizeDrift*100).toFixed(0)+'%',
           `${money(notional(first))} → ${money(notional(last))}`, sizeDrift>0.25?'neg':'')
    + card('Median hold', fmtHours(median(trades.map(t=>t.hours_held))), 'per trade')
    + card('Kept of gross', keptLast==null?'&mdash;':keptLast.toFixed(0)+'%',
           keptFirst!=null?`was ${keptFirst.toFixed(0)}% on the first`:'',
           keptLast!=null && keptLast<60?'neg':'pos');

  const flags=[];
  if(sizeDrift > 0.25 && keptLast!=null && keptFirst!=null && keptLast < keptFirst)
    flags.push(['red','Escalating size while the edge shrank',
      'Each trade got larger while less of the gross survived costs. Fees scale with size; the edge did not.',
      `${money(notional(first))} → ${money(notional(last))} · kept ${keptFirst.toFixed(0)}% → ${keptLast.toFixed(0)}%`]);
  const thin = trades.filter(t=>t.gross && Math.abs(t.gross)>0 && (t.fees+t.funding)/Math.abs(t.gross) > 0.33);
  if(thin.length)
    flags.push(['red','Trades where costs took a third or more',
      'At this size the round trip is eating the result. The target has to clear the cost floor by a wide margin, not by a hair.',
      thin.map(t=>`${t.symbol} ${money(t.gross)} gross, ${money(t.fees+t.funding)} fees`).join(' · ')]);
  const quick = trades.filter(t=>t.hours_held!=null && t.hours_held < 0.1);
  if(quick.length >= 2)
    flags.push(['amber','Several trades held under six minutes',
      'Short holds capture small moves, and a small move is where the fee share is largest.',
      `${quick.length} of ${trades.length} trades`]);
  const flips=[];
  for(let i=1;i<trades.length;i++){
    const a=trades[i-1], b=trades[i];
    if(a.symbol===b.symbol && a.side!==b.side &&
       Math.abs(new Date(b.closed_at)-new Date(a.closed_at)) < 45*60*1000)
      flips.push(`${a.symbol} ${a.side}→${b.side}`);
  }
  if(flips.length)
    flags.push(['amber','Direction reversed in the same symbol within the hour',
      'Closing one side and opening the other shortly after usually means the exit was about discomfort rather than the setup changing.',
      flips.join(' · ')]);
  if(!flags.length)
    flags.push(['ok','Nothing to flag',
      'Size, hold time and cost share are all steady across this session.',
      `${trades.length} trades compared`]);

  const COL={red:['#f87171','#3f1d1d'],amber:['#fbbf24','#3a2f14'],ok:['#4ade80','#14532d']};
  document.getElementById('guard-flags').innerHTML = flags.map(([lv,t,d,e])=>{
    const [c,bg]=COL[lv];
    return `<div style="border:1px solid ${c};background:${bg};border-radius:8px;padding:11px 13px;margin-bottom:9px">
      <div style="font-size:12px;font-weight:600;color:#f1f5f9">${esc(t)}</div>
      <div style="font-size:11px;color:#cbd5e1;margin-top:3px">${esc(d)}</div>
      <div style="font-size:10px;color:#94a3b8;margin-top:5px">${esc(e)}</div></div>`;
  }).join('');

  document.getElementById('guard-drift').innerHTML = `
    <div class="scroll"><table class="tbl"><thead><tr>
      <th>#</th><th>Symbol</th><th>Notional</th><th>Held</th><th>Gross</th><th>Costs</th><th>Kept</th>
    </tr></thead><tbody>` + trades.map((t,i)=>{
      const k=kept(t);
      return `<tr><td class="sub">${i+1}</td><td><b>${esc(t.symbol)}</b></td>
        <td>${money(notional(t))}</td><td>${fmtHours(t.hours_held)}</td>
        <td class="${t.gross>=0?'pos':'neg'}">${signed(t.gross)}</td>
        <td class="neg">-${money(t.fees+t.funding)}</td>
        <td class="${k!=null&&k<60?'neg':'pos'}">${k==null?'&mdash;':k.toFixed(0)+'%'}</td></tr>`;
    }).join('') + '</tbody></table></div>';
}

function median(xs){ const v=xs.filter(x=>x!=null).sort((a,b)=>a-b); return v.length?v[Math.floor(v.length/2)]:null; }
function fmtHours(h){
  if(h==null) return '&mdash;';
  if(h<1/60) return Math.round(h*3600)+'s';
  if(h<1) return Math.round(h*60)+'m';
  return h.toFixed(1)+'h';
}

async function loadAccuracy(){
  const stats = await jget('/api/signals/accuracy',{});
  const banner = document.getElementById('acc-banner');
  if(!stats.resolved){
    banner.innerHTML = `<div class="cr-sig-warn">Outcomes are not resolved yet, so accuracy cannot be computed.
      ${stats.pending||0} signals are stored as pending. The resolver walks stored candles forward from
      each signal to see whether target or stop came first — until it runs, every chart here is empty rather than wrong.</div>`;
  } else { banner.innerHTML=''; }

  document.getElementById('acc-cards').innerHTML =
      card('Signals', stats.total||0, 'in the archive')
    + card('Resolved', stats.resolved||0, stats.pending?`${stats.pending} pending`:'')
    + card('Win rate', stats.win_rate_pct!=null?stats.win_rate_pct+'%':'&mdash;', 'of resolved')
    + card('Median move', stats.median_move_pct!=null?stats.median_move_pct.toFixed(3)+'%':'&mdash;',
           stats.median_move_pct!=null?(stats.median_move_pct/BREAK_EVEN_PCT).toFixed(1)+'× cost':'');

  document.getElementById('acc-calibration').innerHTML =
    (stats.calibration||[]).length ? barRows((stats.calibration||[]).map(b=>
      [`${b.bucket}% stated`, b.win_rate_pct, `${b.win_rate_pct}% · n=${b.n}`]))
    : '<div class="empty">Needs resolved outcomes</div>';

  document.getElementById('acc-moves').innerHTML =
    (stats.move_buckets||[]).length ? moveHistogram(stats.move_buckets)
    : '<div class="empty">Needs archived signals</div>';

  document.getElementById('acc-setups').innerHTML =
    (stats.by_setup||[]).length ? barRows((stats.by_setup||[]).map(b=>
      [esc(CR_SIG_NAME[b.signal_type]||b.signal_type), b.win_rate_pct, `${b.win_rate_pct}% · n=${b.n}`]))
    : '<div class="empty">Needs resolved outcomes</div>';
}

function barRows(rows){
  const mx = Math.max(...rows.map(r=>r[1]), 1);
  return '<div style="display:flex;flex-direction:column;gap:8px">' + rows.map(([lab,v,note])=>
    `<div style="display:grid;grid-template-columns:130px 1fr 92px;align-items:center;gap:10px">
      <span style="font-size:11px;color:#94a3b8">${lab}</span>
      <div style="height:8px;background:#0f172a;border-radius:4px;overflow:hidden">
        <div style="height:8px;width:${v/mx*100}%;background:#0ea5e9;border-radius:4px"></div></div>
      <span style="font-size:10px;color:#64748b;text-align:right">${note}</span></div>`).join('') + '</div>';
}

function moveHistogram(buckets){
  const mx = Math.max(...buckets.map(b=>b.n), 1);
  return '<div style="display:flex;align-items:flex-end;gap:5px;height:130px">' + buckets.map(b=>{
    const under = b.upper_pct < MIN_TARGET_PCT;
    return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:5px;height:100%;justify-content:flex-end">
      <div style="width:100%;height:${b.n/mx*100}%;background:${under?'#f87171':'#0ea5e9'};border-radius:2px 2px 0 0"
           title="${b.n} signals"></div>
      <span style="font-size:9px;color:#475569">${b.upper_pct}%</span></div>`;
  }).join('') + `</div><div style="font-size:10px;color:#64748b;margin-top:6px">
    Red is under the ${MIN_TARGET_PCT.toFixed(3)}% the engine requires. A round trip alone costs ${BREAK_EVEN_PCT.toFixed(3)}%.</div>`;
}

async function loadHistoric(){
  const d = await jget('/api/signals/history?days=365&before=7',{signals:[],counts:{}});
  const c = d.counts||{};
  document.getElementById('hist-cards').innerHTML =
      card('Archived signals', c.signals||0, 'older than 7 days')
    + card('Closed trades', c.trades||0, 'across all cycles')
    + card('Snapshots', c.snapshots||0, 'price + indicator')
    + card('Cycles', c.cycles||0, 'completed');
  const rows = d.signals||[];
  document.getElementById('hist-table').innerHTML = rows.length ? `
    <div class="scroll"><table class="tbl"><thead><tr>
      <th>Date</th><th>Symbol</th><th>Setup</th><th>Dir</th><th>Entry</th><th>Move</th><th>&times; cost</th><th>Conf</th><th>Outcome</th>
    </tr></thead><tbody>` + rows.map(x=>{
      const m=moveOf(x);
      return `<tr>
        <td class="sub">${window.fmtStamp(x.timestamp)}</td>
        <td><b>${esc(x.symbol)}</b></td>
        <td>${esc(CR_SIG_NAME[x.signal_type]||x.signal_type)}</td>
        <td class="${x.direction==='long'?'pos':'neg'}">${x.direction.toUpperCase()}</td>
        <td>${fmtPrice(x.current_price)}</td><td>${m.toFixed(3)}%</td>
        <td class="${m>=MIN_TARGET_PCT?'pos':'neg'}">${(m/BREAK_EVEN_PCT).toFixed(1)}&times;</td>
        <td>${x.confidence}%</td>
        <td class="sub">${esc(x.outcome||'pending')}</td></tr>`;
    }).join('') + '</tbody></table></div>'
    : '<div class="empty">Nothing older than 7 days yet</div>';
}

const TAB_META = {
  dashboard:{title:'Dashboard',       sub:'Last 7 days'},
  crypto:   {title:'Signals',         sub:'Live market and recent predictions'},
  mirror:   {title:'Mirror Signals',  sub:'Opposite direction setup review candidates'},
  simulator:{title:'Market Simulator',sub:'3-Year historical tick replay with Maximum Hugging Face AI reasoning'},
  paper:    {title:'Paper Trading',   sub:'Simulated only — never places a real order'},
  guard:    {title:'Session Guard',   sub:'Behavioural flags from your own trades'},
  accuracy: {title:'Accuracy',        sub:'Calibration, move size and setup performance'},
  historic: {title:'Historic Data',   sub:'Older than 7 days · read-only archive'},
  watchlist:{title:'Watchlist',       sub:'Symbols the collectors track'},
};

function switchTab(tab){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
  const grp = window.hubGroupOf ? window.hubGroupOf('/#'+tab) : null;
  document.querySelectorAll('.side-item').forEach(b=>b.classList.toggle('active', grp ? b.dataset.group===grp.id : b.dataset.tab===tab));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.toggle('active',c.id==='tab-'+tab));
  const m = TAB_META[tab];
  if(m){
    document.getElementById('page-title').textContent = m.title;
    document.getElementById('page-sub').textContent = m.sub;
  }
  // The hash keeps a reload on the same screen, which matters on a free
  // instance that restarts often.
  if(location.hash !== '#'+tab) history.replaceState(null,'','#'+tab);
  if(window.renderHub) window.renderHub();
  if(tab==='paper')     loadPaper();
  if(tab==='guard')     loadGuard();
  if(tab==='accuracy')  loadAccuracy();
  if(tab==='historic')  loadHistoric();
  if(tab==='watchlist') loadWatchlist();
  if(tab==='dashboard') loadDashboard();
  // Panels are no longer rebuilt while hidden, so arriving at one means its
  // data may be a refresh cycle old. Fetch what this tab actually needs.
  if(tab==='crypto' || tab==='mirror' || tab==='simulator') refresh();
  if(tab==='simulator') { pollSimulatorStatus(); loadSimulatorSettings(); }
}

function toggleMore(){
  const bar=document.getElementById('sidebar');
  const on=bar.classList.toggle('more-open');
  document.getElementById('more-scrim').classList.toggle('on',on);
}
// Picking a destination closes the sheet — leaving it open over the screen you
// just navigated to is the classic version of this bug.
document.addEventListener('click',e=>{
  const item=e.target.closest('.side-item');
  if(item && !item.classList.contains('side-more'))
    document.getElementById('sidebar').classList.remove('more-open'),
    document.getElementById('more-scrim').classList.remove('on');
});

// Binance search on the Watchlist tab. The old box posted to a route that did
// not exist, so adding from this tab silently did nothing; and a symbol typed
// from memory is how "xauusdt" ended up on a list Binance cannot price.
let _wlTimer=null, _wlSeq=0;
function wlSearchSoon(){ clearTimeout(_wlTimer); _wlTimer=setTimeout(wlSearchNow,300); }
function _wlCompact(n){
  if(n==null||!isFinite(n)) return '—';
  const a=Math.abs(n);
  return a>=1e9?(n/1e9).toFixed(2)+'B':a>=1e6?(n/1e6).toFixed(1)+'M':a>=1e3?(n/1e3).toFixed(0)+'K':n.toFixed(0);
}
async function wlSearchNow(){
  const q=(document.getElementById('cr-add-input2').value||'').trim();
  const box=document.getElementById('wl-results');
  const seq=++_wlSeq;
  box.innerHTML='<div class="wl-msg">Searching Binance…</div>';
  const d=await jget('/api/binance/symbols?q='+encodeURIComponent(q),{results:[],available:false});
  if(seq!==_wlSeq) return;             // a newer search already answered
  box.textContent='';
  if(!d.available){ const m=document.createElement('div'); m.className='wl-msg';
    m.textContent=d.error||'Binance did not answer. Try again in a minute.'; box.appendChild(m); return; }
  if(!d.results.length){ const m=document.createElement('div'); m.className='wl-msg';
    m.textContent='No Binance USDT pair matches "'+q+'".'; box.appendChild(m); return; }
  for(const r of d.results){
    const row=document.createElement('div'); row.className='wl-row';
    const pair=document.createElement('div'); pair.className='wl-pair';
    pair.textContent=r.symbol.toUpperCase()+'  ';
    const base=document.createElement('span'); base.className='wl-badge'; base.textContent=r.pair; pair.appendChild(base);
    if(r.futures===true){ const f=document.createElement('span'); f.className='wl-badge'; f.textContent='perp futures'; pair.appendChild(f); }
    if(r.futures===false){ const f=document.createElement('span'); f.className='wl-badge'; f.textContent='spot only'; pair.appendChild(f); }
    const meta=document.createElement('div'); meta.className='wl-meta';
    const chg=r.change_pct;
    meta.append(document.createTextNode((r.price!=null?'$'+fmtPrice(r.price):'—')+' · 24h '));
    const c=document.createElement('span'); c.className=chg>=0?'wl-up':'wl-down';
    c.textContent=chg==null?'—':(chg>=0?'+':'')+chg.toFixed(2)+'%'; meta.appendChild(c);
    meta.append(document.createTextNode(' · vol $'+_wlCompact(r.quote_volume_24h)));
    if(r.quote_volume_24h!=null && r.quote_volume_24h<5e6)
      meta.append(document.createTextNode(' · thin: spreads will be wide'));
    const btn=document.createElement('button'); btn.type='button';
    if(r.on_watchlist){ btn.textContent='Added'; btn.disabled=true; }
    else { btn.textContent='+ Add'; btn.onclick=()=>wlAdd(r.symbol,btn); }
    row.append(pair,btn,meta); box.appendChild(row);
  }
}
async function wlAdd(sym,btn){
  btn.disabled=true; btn.textContent='Adding…';
  const res=await apiFetch('/api/crypto/watchlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym})});
  let body={}; try{ body=await res.json(); }catch(e){}
  if(res.ok){ btn.textContent=body.warning?'Added (unverified)':'Added'; loadWatchlist(); }
  else { btn.disabled=false; btn.textContent='+ Add'; alert(body.error||('Could not add: HTTP '+res.status)); }
}
function addCryptoSymbol2(){ wlSearchNow(); }

// ── CRYPTO ────────────────────────────────────────────────────────────────────
// Enough precision that entry, target and stop are always distinguishable.
// Rounding a $1,905 price to whole dollars made every signal render as
// "Entry $1,905  Target $1,905  Stop $1,905", hiding the real distances.
// A price distance, shown at the precision of the price it belongs to. Running
// a delta through fmtPrice alone gives "10.7500" for ten and three quarters.
function fmtDelta(d, ref){
  if(ref>=1000) return d.toFixed(2);
  if(ref>=1) return d.toFixed(4);
  return d.toFixed(6);
}
function fmtPrice(p){
  if(p == null || !isFinite(p)) return '—';
  if(p>=1000) return p.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  if(p>=1) return p.toFixed(4);
  return p.toFixed(6);
}
// Injected from the Python cost model at render time — see _COST_SNIPPET.
// Hardcoding these here is how the page and the engine drifted apart: the
// dashboard called a signal viable that the engine would have refused.
const BREAK_EVEN_PCT = __BREAK_EVEN_PCT__;
const MIN_TARGET_PCT = __MIN_TARGET_PCT__;
const PAPER_LEVERAGE = __PAPER_LEVERAGE__;
function renderCryptoCoins(coins){
  _crWatchlistSymbols = (coins||[]).map(c=>String(c.symbol).toUpperCase());
  renderCryptoSignalTabs();
  const el=document.getElementById('cr-coins');
  if(!coins.length){el.innerHTML='<div class="empty">Watchlist is empty — add a symbol above</div>';return;}
  el.innerHTML='<div class="cr-grid">'+coins.map(c=>{
    const up=c.change_24h_pct>=0;
    // A price of zero is missing data, not a price. Rendering $0.000000 the
    // same way as a real quote is how XAUUSDT looked live for hours.
    const hasData = c.price > 0;
    if(!hasData){
      return `<div class="cr-coin cr-coin-nodata">
        <div class="cr-coin-top"><span class="cr-coin-sym">${esc(c.symbol)}</span>
          <button class="cr-coin-remove" onclick="removeCryptoSymbol('${esc(c.symbol.toLowerCase())}')" title="Remove from watchlist">✕</button></div>
        <div class="cr-coin-price cr-nodata">no price feed</div>
        <div class="cr-coin-chg">Not carried by the spot ticker. Futures-only
          instruments like gold are fetched separately — check
          <span class="mono">/api/debug/coindcx?symbol=${esc(c.symbol.toLowerCase())}</span></div>
      </div>`;
    }
    return `<div class="cr-coin">
      <div class="cr-coin-top"><span class="cr-coin-sym">${esc(c.symbol)}</span>
        <button class="cr-coin-remove" onclick="removeCryptoSymbol('${esc(c.symbol.toLowerCase())}')" title="Remove from watchlist">✕</button></div>
      <div class="cr-coin-price">$${fmtPrice(c.price)}</div>
      <div class="cr-coin-chg ${up?'up':'down'}">${up?'▲':'▼'} ${Math.abs(c.change_24h_pct).toFixed(2)}% (24h)</div>
      <div class="cr-coin-stats"><span>RSI <b>${c.rsi_14.toFixed(0)}</b></span><span>ATR <b>${
        c.atr_pct==null?'—':c.atr_pct.toFixed(3)+'%'}</b></span><span>Vol <b>${
        c.volume_ratio>0?'×'+c.volume_ratio.toFixed(1):'n/a'}</b></span></div>
    </div>`;
  }).join('')+'</div>';
}
const CR_SIG_NAME={rsi_divergence:'Momentum Reversal',volume_spike:'Volume Surge',bollinger_squeeze:'Breakout Setup',sentiment_shift:'News Catalyst'};
const CR_SIG_PAGE_SIZE=8;
let _crSignalsAll=[];
let _crSignalFilter='ALL';
let _crSignalPage=0;

function renderCryptoSignals(signals){
  _crSignalsAll=signals||[];
  if(_crSignalFilter!=='ALL' && !_crSignalsAll.some(s=>s.symbol===_crSignalFilter)){
    _crSignalFilter='ALL';
  }
  renderCryptoSignalTabs();
  renderCryptoSignalsPage();
}

// Every watched symbol gets a tab, whether or not it has fired lately. Tabs
// built only from signals meant a quiet coin vanished from the filter — the
// one case where you most want to check whether anything fired.
let _crWatchlistSymbols = [];

function renderCryptoSignalTabs(){
  const el=document.getElementById('cr-sig-tabs');
  if(!el) return;
  const withSignals = new Set(_crSignalsAll.map(s=>s.symbol));
  const symbols=[...new Set([..._crWatchlistSymbols, ...withSignals])].sort();
  if(!symbols.length){el.innerHTML='';return;}
  const counts={};
  _crSignalsAll.forEach(s=>{counts[s.symbol]=(counts[s.symbol]||0)+1;});
  const tab=(t,label,n)=>
    `<button class="cr-sig-tab ${t===_crSignalFilter?'active':''}${n===0?' quiet':''}"
      onclick="setCryptoSignalFilter('${esc(t)}')">${esc(label)}${
      n===undefined?'':`<span class="cr-tab-n">${n}</span>`}</button>`;
  el.innerHTML = tab('ALL','All',_crSignalsAll.length)
    + symbols.map(sym=>tab(sym,sym,counts[sym]||0)).join('');
}

function setCryptoSignalFilter(sym){
  _crSignalFilter=sym;
  _crSignalPage=0;
  renderCryptoSignalTabs();
  renderCryptoSignalsPage();
}

function changeCryptoSignalPage(delta){
  _crSignalPage+=delta;
  renderCryptoSignalsPage();
}

function renderCryptoSignalsPage(){
  const el=document.getElementById('cr-signals');
  const pageEl=document.getElementById('cr-sig-pagination');
  const filtered=_crSignalFilter==='ALL'?_crSignalsAll:_crSignalsAll.filter(s=>s.symbol===_crSignalFilter);

  if(!filtered.length){
    el.innerHTML='<div class="empty">No crypto signals in the last 24 hours</div>';
    if(pageEl) pageEl.innerHTML='';
    return;
  }

  const totalPages=Math.max(1,Math.ceil(filtered.length/CR_SIG_PAGE_SIZE));
  _crSignalPage=Math.min(Math.max(0,_crSignalPage),totalPages-1);
  const start=_crSignalPage*CR_SIG_PAGE_SIZE;
  el.innerHTML=filtered.slice(start,start+CR_SIG_PAGE_SIZE).map(renderCryptoSignalCard).join('');

  if(pageEl){
    pageEl.innerHTML = totalPages<=1 ? '' : `
      <button class="cr-page-btn" ${_crSignalPage===0?'disabled':''} onclick="changeCryptoSignalPage(-1)">‹ Prev</button>
      <span class="cr-page-label">Page ${_crSignalPage+1} of ${totalPages}</span>
      <button class="cr-page-btn" ${_crSignalPage>=totalPages-1?'disabled':''} onclick="changeCryptoSignalPage(1)">Next ›</button>`;
  }
}

let _crMirrorSignalsAll = [];
let _crMirrorSummary = null;
let _crMirrorSignalFilter = 'ALL';
let _crMirrorStatusFilter = 'ALL';
let _crMirrorSignalPage = 0;

function renderMirrorSignals(data){
  if(data && Array.isArray(data.signals)){
    _crMirrorSignalsAll = data.signals;
    _crMirrorSummary = data.summary;
  } else {
    _crMirrorSignalsAll = Array.isArray(data) ? data : [];
    _crMirrorSummary = null;
  }

  // Update scorecard
  const total = _crMirrorSignalsAll.length;
  const worked = _crMirrorSignalsAll.filter(s => s.trade_check && s.trade_check.worked).length;
  const full = _crMirrorSignalsAll.filter(s => s.trade_check && s.trade_check.status === 'won').length;
  const partial = _crMirrorSignalsAll.filter(s => s.trade_check && s.trade_check.status === 'partial').length;
  const stopped = _crMirrorSignalsAll.filter(s => s.trade_check && s.trade_check.status === 'stopped').length;
  const running = _crMirrorSignalsAll.filter(s => s.trade_check && s.trade_check.status === 'running').length;
  const sumPeak = _crMirrorSignalsAll.reduce((acc, s) => acc + (s.trade_check ? (s.trade_check.peak_gain_pct || 0) : 0), 0);
  const avgPeak = total > 0 ? (sumPeak / total).toFixed(2) : '0.00';
  const rate = total > 0 ? ((worked / total) * 100).toFixed(1) : '0.0';

  setText('ms-total', total);
  setText('ms-rate', rate + '%');
  setText('ms-full', full);
  setText('ms-partial', partial);
  setText('ms-stopped', stopped);
  setText('ms-running', running);
  setText('ms-peak', '+' + avgPeak + '%');

  renderMirrorSignalStatusTabs();
  renderMirrorSignalTabs();
  renderMirrorSignalsPage();
}

function renderMirrorSignalStatusTabs(){
  const el = document.getElementById('cr-mirror-status-filter');
  if(!el) return;
  const workedCount = _crMirrorSignalsAll.filter(s => s.trade_check && s.trade_check.worked).length;
  const fullCount = _crMirrorSignalsAll.filter(s => s.trade_check && s.trade_check.status === 'won').length;
  const partCount = _crMirrorSignalsAll.filter(s => s.trade_check && s.trade_check.status === 'partial').length;
  const runCount = _crMirrorSignalsAll.filter(s => s.trade_check && s.trade_check.status === 'running').length;
  const stopCount = _crMirrorSignalsAll.filter(s => s.trade_check && s.trade_check.status === 'stopped').length;

  const btn = (st, label, n, color) => `
    <button class="cr-sig-tab ${st === _crMirrorStatusFilter ? 'active' : ''}"
      onclick="setMirrorStatusFilter('${st}')" style="${st === _crMirrorStatusFilter ? 'border-color:' + color + ';color:' + color : ''}">
      ${label} <span class="cr-tab-n">${n}</span>
    </button>`;

  el.innerHTML =
    btn('ALL', 'All Signals', _crMirrorSignalsAll.length, '#0ea5e9') +
    btn('WORKED', '🟢 Worked (Full+Part)', workedCount, '#10b981') +
    btn('WON', '🏆 Full Target', fullCount, '#10b981') +
    btn('PARTIAL', '⚡ Partial Win', partCount, '#f59e0b') +
    btn('RUNNING', '🔵 Active / Running', runCount, '#38bdf8') +
    btn('STOPPED', '🛑 Direct Stop', stopCount, '#ef4444');
}

function setMirrorStatusFilter(st){
  _crMirrorStatusFilter = st;
  _crMirrorSignalPage = 0;
  renderMirrorSignalStatusTabs();
  renderMirrorSignalsPage();
}

function renderMirrorSignalTabs(){
  const el = document.getElementById('cr-mirror-sig-tabs');
  if(!el) return;
  const withSignals = new Set(_crMirrorSignalsAll.map(s => s.symbol));
  const symbols = [...new Set([..._crWatchlistSymbols, ...withSignals])].sort();
  if(!symbols.length){ el.innerHTML = ''; return; }
  const counts = {};
  _crMirrorSignalsAll.forEach(s => { counts[s.symbol] = (counts[s.symbol] || 0) + 1; });
  const tab = (t, label, n) =>
    `<button class="cr-sig-tab ${t === _crMirrorSignalFilter ? 'active' : ''}${n === 0 ? ' quiet' : ''}"
      onclick="setMirrorSignalFilter('${esc(t)}')">${esc(label)}${
      n === undefined ? '' : `<span class="cr-tab-n">${n}</span>`}</button>`;
  el.innerHTML = tab('ALL', 'All Coins', _crMirrorSignalsAll.length)
    + symbols.map(sym => tab(sym, sym, counts[sym] || 0)).join('');
}

function setMirrorSignalFilter(sym){
  _crMirrorSignalFilter = sym;
  _crMirrorSignalPage = 0;
  renderMirrorSignalTabs();
  renderMirrorSignalsPage();
}

function changeMirrorSignalPage(delta){
  _crMirrorSignalPage += delta;
  renderMirrorSignalsPage();
}

function renderMirrorSignalsPage(){
  const el = document.getElementById('cr-mirror-signals');
  const pageEl = document.getElementById('cr-mirror-sig-pagination');

  let filtered = _crMirrorSignalsAll;
  if(_crMirrorSignalFilter !== 'ALL'){
    filtered = filtered.filter(s => s.symbol === _crMirrorSignalFilter);
  }
  if(_crMirrorStatusFilter === 'WORKED'){
    filtered = filtered.filter(s => s.trade_check && s.trade_check.worked);
  } else if(_crMirrorStatusFilter === 'WON'){
    filtered = filtered.filter(s => s.trade_check && s.trade_check.status === 'won');
  } else if(_crMirrorStatusFilter === 'PARTIAL'){
    filtered = filtered.filter(s => s.trade_check && s.trade_check.status === 'partial');
  } else if(_crMirrorStatusFilter === 'RUNNING'){
    filtered = filtered.filter(s => s.trade_check && s.trade_check.status === 'running');
  } else if(_crMirrorStatusFilter === 'STOPPED'){
    filtered = filtered.filter(s => s.trade_check && s.trade_check.status === 'stopped');
  }

  if(!filtered.length){
    el.innerHTML = '<div class="empty">No mirror signals matching filter in the last 48 hours</div>';
    if(pageEl) pageEl.innerHTML = '';
    return;
  }

  const totalPages = Math.max(1, Math.ceil(filtered.length / CR_SIG_PAGE_SIZE));
  _crMirrorSignalPage = Math.min(Math.max(0, _crMirrorSignalPage), totalPages - 1);
  const start = _crMirrorSignalPage * CR_SIG_PAGE_SIZE;
  el.innerHTML = filtered.slice(start, start + CR_SIG_PAGE_SIZE).map(renderMirrorSignalCard).join('');

  if(pageEl){
    pageEl.innerHTML = totalPages <= 1 ? '' : `
      <button class="cr-page-btn" ${_crMirrorSignalPage === 0 ? 'disabled' : ''} onclick="changeMirrorSignalPage(-1)">‹ Prev</button>
      <span class="cr-page-label">Page ${_crMirrorSignalPage + 1} of ${totalPages}</span>
      <button class="cr-page-btn" ${_crMirrorSignalPage >= totalPages - 1 ? 'disabled' : ''} onclick="changeMirrorSignalPage(1)">Next ›</button>`;
  }
}

async function forceMirrorLiveCheck(btn){
  if(btn){
    btn.disabled = true;
    btn.innerHTML = '<span>⏳ Evaluating Live Trades & Hugging Face AI...</span>';
  }
  try {
    const res = await fetch('/api/crypto/signals/live-check?mirror=1', { method: 'POST' });
    if(res.ok){
      const data = await res.json();
      renderMirrorSignals(data);
    }
  } catch(e){
    console.error('live check error:', e);
  } finally {
    if(btn){
      btn.disabled = false;
      btn.innerHTML = '<span>⚡ Force Live Check & AI Review</span>';
    }
  }
}

function renderMirrorSignalCard(s){
  const name = CR_SIG_NAME[s.signal_type] || s.signal_type.replace(/_/g, ' ');
  const long = s.direction === 'long';
  const entry = s.current_price, tp = s.target_price, sl = s.stop_loss;
  const move = (tp && entry) ? Math.abs(tp - entry) / entry * 100 : 0;
  const risk = (sl && entry) ? Math.abs(entry - sl) / entry * 100 : 0;

  const tc = s.trade_check || {
    status: s.outcome || 'running',
    worked: s.outcome === 'won',
    worked_desc: 'Evaluating live trajectory...',
    target_pct_reached: s.outcome === 'won' ? 100 : 0,
    peak_gain_pct: s.pnl_pct || 0,
    peak_r: 0,
    peak_price: entry,
    max_drawdown_pct: 0,
    realized_pnl_pct: s.pnl_pct || 0,
    current_pnl_pct: s.pnl_pct || 0,
  };
  const ai = s.ai_reasoning || {};

  let badgeColor = '#64748b';
  let badgeText = 'PENDING';
  if(tc.status === 'won'){
    badgeColor = '#10b981';
    badgeText = '🏆 WORKED (FULL TARGET +' + (tc.target_dist_pct || move).toFixed(2) + '%)';
  } else if(tc.status === 'partial'){
    badgeColor = '#f59e0b';
    badgeText = '⚡ PARTIALLY WORKED (' + tc.target_pct_reached + '% OF TARGET)';
  } else if(tc.status === 'running'){
    badgeColor = '#0ea5e9';
    badgeText = '🔵 ACTIVE TRADE (' + (tc.current_pnl_pct >= 0 ? '+' : '') + tc.current_pnl_pct.toFixed(2) + '%)';
  } else if(tc.status === 'stopped'){
    badgeColor = '#ef4444';
    badgeText = '🛑 STOPPED OUT (-' + (tc.risk_dist_pct || risk).toFixed(2) + '%)';
  }

  const gaugePercent = Math.min(100, Math.max(0, tc.target_pct_reached || 0));

  return `<div class="sig ${long ? 'long' : 'short'}" style="border-top:3px solid ${badgeColor};padding:16px;margin-bottom:14px">
    <div class="sig-head" style="margin-bottom:8px">
      <span class="sig-sym">${esc(s.symbol)}</span>
      <span class="sig-dir ${long ? 'long' : 'short'}">${long ? 'Long' : 'Short'}</span>
      <span class="pt-mode" style="background:${badgeColor};color:#fff;font-weight:700">${badgeText}</span>
      <div style="flex-grow:1"></div>
      <div style="text-align:right">
        <span style="font-size:11px;color:#94a3b8">Peak Gain: </span>
        <b style="font-size:15px;color:#10b981">+${tc.peak_gain_pct.toFixed(2)}%</b>
        <span style="font-size:11px;color:#64748b"> (+${tc.peak_r}R)</span>
      </div>
    </div>

    <div class="sig-meta" style="margin-bottom:12px">
      <span class="sig-setup">${esc(name)}</span>
      <span>&middot;</span><span>${s.confidence}% confidence</span>
      <span>&middot;</span><span>Target: ~${esc(s.timeframe)}</span>
      <div style="flex-grow:1"></div>
      <span class="sig-when">${fmtSignalTime(s.timestamp)}</span>
    </div>

    <!-- Levels Row -->
    <div class="sig-levels" style="margin-bottom:10px">
      <div><label>Stop Loss</label><b class="neg">${fmtPrice(sl)}</b><span>−${risk.toFixed(2)}%</span></div>
      <div class="mid"><label>Entry Price</label><b>${fmtPrice(entry)}</b><span>LTP</span></div>
      <div class="right"><label>Take Profit</label><b class="pos">${fmtPrice(tp)}</b><span>+${move.toFixed(2)}%</span></div>
    </div>

    <!-- How Much It Worked Visual Track Gauge -->
    <div style="margin:12px 0 10px;background:#0f172a;border-radius:8px;padding:10px 12px;border:1px solid #334155">
      <div style="display:flex;justify-content:space-between;font-size:11px;color:#94a3b8;margin-bottom:4px">
        <span>Stop Loss: ${fmtPrice(sl)}</span>
        <span style="color:#f1f5f9;font-weight:600">Peak Reached: ${fmtPrice(tc.peak_price)} [★ ${tc.target_pct_reached}% to Target]</span>
        <span>Target: ${fmtPrice(tp)}</span>
      </div>
      <div style="background:#1e293b;height:10px;border-radius:5px;position:relative;overflow:hidden">
        <div style="position:absolute;left:0;top:0;height:100%;width:${gaugePercent}%;background:linear-gradient(90deg,#0ea5e9,${tc.status === 'won' ? '#10b981' : '#f59e0b'})"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:10px;color:#64748b;margin-top:4px">
        <span>0% (Entry)</span>
        <span>50% (TP1 Milestone)</span>
        <span>100% (Full Target)</span>
      </div>
    </div>

    <!-- Narrative / How much it worked -->
    <div class="sig-warn" style="background:rgba(14,165,233,0.06);border-left-color:#0ea5e9;color:#e2e8f0;margin-bottom:12px">
      <b>Performance Summary:</b> ${esc(tc.worked_desc || 'Signal executed in live market.')}
    </div>

    <!-- Hugging Face Dual AI Reasoning -->
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin-bottom:10px">
      <div style="background:rgba(16,185,129,0.07);border:1px solid rgba(16,185,129,0.25);border-radius:8px;padding:10px">
        <div style="font-size:11px;font-weight:700;color:#10b981;margin-bottom:4px;display:flex;align-items:center;gap:4px">
          <span>🟢 AI: Why It Worked</span>
        </div>
        <div style="font-size:11.5px;color:#cbd5e1;line-height:1.45">${esc(ai.why_it_worked || 'Technical momentum and volume expansion supported the move toward peak levels.')}</div>
      </div>
      <div style="background:rgba(239,68,68,0.07);border:1px solid rgba(239,68,68,0.25);border-radius:8px;padding:10px">
        <div style="font-size:11px;font-weight:700;color:#f87171;margin-bottom:4px;display:flex;align-items:center;gap:4px">
          <span>🔴 AI: Why It Failed / Retraced</span>
        </div>
        <div style="font-size:11.5px;color:#cbd5e1;line-height:1.45">${esc(ai.why_it_failed || 'Opposing liquidity or counter-trend resistance capped further continuation.')}</div>
      </div>
    </div>

    <!-- Key Heuristic Footer -->
    <div style="display:flex;align-items:center;justify-content:space-between;padding-top:6px;border-top:1px solid rgba(255,255,255,0.06);font-size:11px;color:#94a3b8">
      <div>💡 <b>Key Takeaway:</b> ${esc(ai.key_takeaway || 'Manage risk proactively with dynamic trailing stops.')}</div>
      <div style="font-size:10px;color:#64748b">Provider: ${esc(ai.source_model || 'Hugging Face')}</div>
    </div>

    ${renderReviewTrail(s)}
  </div>`;
}

// ── SIMULATOR CLIENT JS ────────────────────────────────────────────────────────
let _simPollTimer = null;
async function fetchSimulatorStatus(){
  try {
    const res = await fetch('/api/simulator/status');
    if(!res.ok) return;
    const data = await res.json();
    renderSimulator(data);
    if(data.is_running){
      if(!_simPollTimer) _simPollTimer = setTimeout(fetchSimulatorStatus, 2500);
    } else {
      if(_simPollTimer) { clearTimeout(_simPollTimer); _simPollTimer = null; }
    }
  } catch(e){
    console.error('fetchSimulatorStatus error:', e);
  }
}

function pollSimulatorStatus(){
  if(_simPollTimer) clearTimeout(_simPollTimer);
  _simPollTimer = setTimeout(fetchSimulatorStatus, 500);
}

function updateRMathCard(){
  const tpR = parseFloat(document.getElementById('sim-input-tp')?.value || '2.2');
  const slR = parseFloat(document.getElementById('sim-input-sl')?.value || '1.5');
  const lev = parseFloat(document.getElementById('sim-input-cycle-lev')?.value || '10');
  const baseMargin = 100.0; // Base $100 margin example requested by user
  
  // In the simulator engine, 1R benchmark volatility distance is 1.2% price move (min 1.2%, or 1.8x ATR)
  const oneRPricePct = 1.2;
  const tpPricePct = tpR * oneRPricePct;
  const slPricePct = slR * oneRPricePct;
  
  // ROE = Price move % * Leverage
  const tpRoePct = tpPricePct * lev;
  const slRoePct = slPricePct * lev;
  
  // Dollar profit / loss on $100 margin
  const tpCash = (tpRoePct / 100.0) * baseMargin;
  const slCash = (slRoePct / 100.0) * baseMargin;
  
  const rr = (slR > 0) ? (tpR / slR).toFixed(2) : '—';
  const posSize = baseMargin * lev;

  // Update input header badges
  setText('sim-tp-calc-badge', `+${tpRoePct.toFixed(1)}% ROE · +$${tpCash.toFixed(2)} on $100`);
  setText('sim-sl-calc-badge', `-${slRoePct.toFixed(1)}% ROE · -$${slCash.toFixed(2)} on $100`);

  // Update live breakdown card
  setText('sim-math-tp-roe', `+${tpRoePct.toFixed(1)}% ROE`);
  setText('sim-math-tp-cash', `+$${tpCash.toFixed(2)} USDT profit on $100`);
  setText('sim-math-tp-price', `Requires +${tpPricePct.toFixed(2)}% price move (${tpR.toFixed(1)}R)`);

  setText('sim-math-sl-roe', `-${slRoePct.toFixed(1)}% ROE`);
  setText('sim-math-sl-cash', `-$${slCash.toFixed(2)} USDT loss on $100`);
  setText('sim-math-sl-price', `Hits at -${slPricePct.toFixed(2)}% price move (${slR.toFixed(1)}R)`);

  setText('sim-math-rr', `${rr} : 1`);
  setText('sim-math-rr-desc', `Gain $${rr} for every $1.00 risked`);
  setText('sim-math-position', `Position size: $${posSize.toLocaleString()} at ${lev}x`);
}

function updateSimulatorEstimates(){
  const yearsVal = parseFloat(document.getElementById('sim-input-years')?.value || '0.083');
  const stepHours = parseFloat(document.getElementById('sim-input-step-hours')?.value || '1.0');
  const stepBudgetSec = parseFloat(document.getElementById('sim-input-step-seconds')?.value || '60.0');

  const totalMarketHours = Math.round(yearsVal * 8760);
  const totalSteps = Math.max(1, Math.round(totalMarketHours / Math.max(0.1, stepHours)));
  const totalDurationSec = totalSteps * stepBudgetSec;

  const hours = Math.floor(totalDurationSec / 3600);
  const mins = Math.floor((totalDurationSec % 3600) / 60);
  const timeStr = (hours > 0 ? (hours + ' hrs ') : '') + mins + ' mins';

  const finishDate = new Date(Date.now() + totalDurationSec * 1000);
  const finishStr = window.fmtStamp(finishDate.toISOString());   // the one shared formatter: date + time, IST

  setText('sim-est-steps', totalSteps.toLocaleString() + ' Steps');
  setText('sim-est-hours', totalMarketHours.toLocaleString() + ' market hours');
  setText('sim-est-time', timeStr);
  setText('sim-est-finish', finishStr);
}

// ── Persistent Simulator Settings Storage (localStorage + Server) ──
const SIM_SETTINGS_KEY = 'sim_settings_v3';

let _currentSimView = 'summary';
function switchSimView(viewName) {
  _currentSimView = viewName;
  const views = ['summary', 'cycles', 'trades', 'events', 'config'];
  views.forEach(v => {
    const el = document.getElementById('sim-view-' + v);
    const btn = document.getElementById('sim-view-btn-' + v);
    if (el) el.style.display = (v === viewName) ? 'block' : 'none';
    if (btn) {
      if (v === viewName) btn.classList.add('active');
      else btn.classList.remove('active');
    }
  });
  if (viewName === 'events') fetchAIEvents();
  if (viewName === 'cycles' && _cachedCycleData) {
    renderCycleStatements(_cachedCycleData);
  }
}

function getSimulatorFormValues() {
  return {
    strategy: document.getElementById('sim-input-strategy')?.value || 'all',
    years: document.getElementById('sim-input-years')?.value || '0.0082',
    tp_r: parseFloat(document.getElementById('sim-input-tp')?.value || '2.2'),
    sl_r: parseFloat(document.getElementById('sim-input-sl')?.value || '1.5'),
    anti_flip: parseInt(document.getElementById('sim-input-antiflip')?.value || '90'),
    stagnation: parseInt(document.getElementById('sim-input-stagnation')?.value || '60'),
    max_ai_reviews: 150,
    cycle_start: parseFloat(document.getElementById('sim-input-cycle-start')?.value || '25'),
    cycle_target: parseFloat(document.getElementById('sim-input-cycle-target')?.value || '100'),
    cycle_margin_pct: parseFloat(document.getElementById('sim-input-cycle-margin')?.value || '25'),
    cycle_leverage: parseFloat(document.getElementById('sim-input-cycle-lev')?.value || '10'),
    stepped_mode: document.getElementById('sim-input-stepped')?.checked ?? true,
    step_market_hours: parseFloat(document.getElementById('sim-input-step-hours')?.value || '1.0'),
    step_seconds: parseFloat(document.getElementById('sim-input-step-seconds')?.value || '60.0'),
    gemini_key: '',
    openrouter_key: '',
    hf_tokens: '',
    ai_provider: document.getElementById('sim-input-ai-provider')?.value || 'none',
  };
}

let _saveSimDebounce = null;
function saveSimulatorSettings() {
  const vals = getSimulatorFormValues();
  try {
    localStorage.setItem(SIM_SETTINGS_KEY, JSON.stringify(vals));
  } catch(e){}
  if (_saveSimDebounce) clearTimeout(_saveSimDebounce);
  _saveSimDebounce = setTimeout(() => {
    fetch('/api/simulator/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(vals)
    }).catch(e => console.debug('save sim config error:', e));
  }, 300);
}

async function loadSimulatorSettings() {
  let cfg = null;
  try {
    const raw = localStorage.getItem(SIM_SETTINGS_KEY);
    if (raw) cfg = JSON.parse(raw);
  } catch(e){}

  if (!cfg) {
    try {
      const res = await fetch('/api/simulator/config');
      if (res.ok) cfg = await res.json();
    } catch(e){}
  }

  if (cfg) {
    applySimulatorConfig(cfg);
  }
  updateSimulatorEstimates();
  updateRMathCard();
  attachSimulatorInputListeners();
}

function applySimulatorConfig(c) {
  if (c.strategy !== undefined && document.getElementById('sim-input-strategy')) document.getElementById('sim-input-strategy').value = c.strategy;
  if (c.years !== undefined && document.getElementById('sim-input-years')) document.getElementById('sim-input-years').value = c.years;
  if (c.tp_r !== undefined && document.getElementById('sim-input-tp')) document.getElementById('sim-input-tp').value = c.tp_r;
  if (c.sl_r !== undefined && document.getElementById('sim-input-sl')) document.getElementById('sim-input-sl').value = c.sl_r;
  if (c.anti_flip !== undefined && document.getElementById('sim-input-antiflip')) document.getElementById('sim-input-antiflip').value = c.anti_flip;
  if (c.stagnation !== undefined && document.getElementById('sim-input-stagnation')) document.getElementById('sim-input-stagnation').value = c.stagnation;
  if (c.cycle_start !== undefined && document.getElementById('sim-input-cycle-start')) document.getElementById('sim-input-cycle-start').value = c.cycle_start;
  if (c.cycle_target !== undefined && document.getElementById('sim-input-cycle-target')) document.getElementById('sim-input-cycle-target').value = c.cycle_target;
  if (c.cycle_margin_pct !== undefined && document.getElementById('sim-input-cycle-margin')) document.getElementById('sim-input-cycle-margin').value = c.cycle_margin_pct;
  if (c.cycle_leverage !== undefined && document.getElementById('sim-input-cycle-lev')) document.getElementById('sim-input-cycle-lev').value = c.cycle_leverage;
  if (c.stepped_mode !== undefined && document.getElementById('sim-input-stepped')) document.getElementById('sim-input-stepped').checked = !!c.stepped_mode;
  if (c.step_market_hours !== undefined && document.getElementById('sim-input-step-hours')) document.getElementById('sim-input-step-hours').value = c.step_market_hours;
  if (c.step_seconds !== undefined && document.getElementById('sim-input-step-seconds')) document.getElementById('sim-input-step-seconds').value = c.step_seconds;
  if (c.ai_provider && document.getElementById('sim-input-ai-provider')) document.getElementById('sim-input-ai-provider').value = c.ai_provider;
}

let _simListenersAttached = false;
function attachSimulatorInputListeners() {
  if (_simListenersAttached) return;
  const ids = [
    'sim-input-strategy', 'sim-input-years', 'sim-input-tp', 'sim-input-sl', 'sim-input-antiflip',
    'sim-input-stagnation', 'sim-input-cycle-start',
    'sim-input-cycle-target', 'sim-input-cycle-margin', 'sim-input-cycle-lev',
    'sim-input-stepped', 'sim-input-step-hours', 'sim-input-step-seconds',
    'sim-input-ai-provider'
  ];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('input', () => { saveSimulatorSettings(); updateSimulatorEstimates(); updateRMathCard(); });
      el.addEventListener('change', () => { saveSimulatorSettings(); updateSimulatorEstimates(); updateRMathCard(); });
    }
  });
  _simListenersAttached = true;
}

let _aiEventsPollTimer = null;
async function fetchAIEvents(force = false){
  try {
    const res = await fetch('/api/simulator/ai-events');
    if(!res.ok) return;
    const events = await res.json();
    renderAIEvents(events);
  } catch(e){
    console.error('fetchAIEvents error:', e);
  }
}

function renderAIEvents(events){
  const tbody = document.getElementById('sim-ai-events-table');
  const countBadge = document.getElementById('ai-events-count-badge');
  if(!tbody || !Array.isArray(events)) return;
  if(countBadge) countBadge.innerText = events.length + ' Calls Logged';
  if(!events.length){
    tbody.innerHTML = '<tr><td colspan="7" style="padding:12px;text-align:center;color:#64748b">No AI events logged yet (AI calls are stopped).</td></tr>';
    return;
  }
  tbody.innerHTML = events.slice(-30).reverse().map((e, idx) => `
    <tr style="border-bottom:1px solid #1e293b;background:${idx % 2 === 0 ? 'rgba(15,23,42,0.4)' : 'rgba(30,41,59,0.2)'}">
      <td style="padding:6px 8px;color:#94a3b8;font-family:monospace">${esc(e.timestamp ? e.timestamp.slice(11, 19) : '')}</td>
      <td style="padding:6px 8px">
        <span style="padding:2px 6px;border-radius:4px;font-size:10px;font-weight:700;color:#fff;background:${e.provider === 'gemini' ? '#059669' : (e.provider === 'groq' ? '#0284c7' : (e.provider === 'openrouter' ? '#7c3aed' : '#d97706'))}">
          ${esc((e.provider || 'AI').toUpperCase())}
        </span>
        <span style="font-size:10px;color:#cbd5e1;margin-left:4px">${esc(e.model || '')}</span>
      </td>
      <td style="padding:6px 8px;color:#38bdf8">${esc(e.call_type || '')}</td>
      <td style="padding:6px 8px;font-weight:600;color:#f1f5f9">${esc(e.symbol || '')} <span style="color:${e.direction === 'LONG' ? '#10b981' : '#f43f5e'}">${esc(e.direction || '')}</span></td>
      <td style="padding:6px 8px;color:#cbd5e1">${e.latency_ms || 0} ms</td>
      <td style="padding:6px 8px"><span style="padding:2px 5px;border-radius:3px;font-size:10px;color:#fff;background:${e.status === 'SUCCESS' ? '#10b981' : '#f59e0b'}">${esc(e.status || 'OK')}</span></td>
      <td style="padding:6px 8px">
        <div style="font-size:11px;color:#cbd5e1">${esc(e.summary || '')}</div>
        ${e.why_it_worked ? `<div style="font-size:10px;color:#10b981;margin-top:2px">🟢 Worked: ${esc(e.why_it_worked.slice(0, 90))}...</div>` : ''}
        ${e.why_it_failed ? `<div style="font-size:10px;color:#f87171;margin-top:1px">🔴 Retraced: ${esc(e.why_it_failed.slice(0, 90))}...</div>` : ''}
      </td>
    </tr>
  `).join('');
}

async function startSimulator(){
  const btn = document.getElementById('sim-btn-start');
  const pauseBtn = document.getElementById('sim-btn-pause');
  const strategy = document.getElementById('sim-input-strategy')?.value || 'all';
  const years = parseFloat(document.getElementById('sim-input-years')?.value || '0.0082');
  const tp = parseFloat(document.getElementById('sim-input-tp')?.value || '2.2');
  const sl = parseFloat(document.getElementById('sim-input-sl')?.value || '1.5');
  const antiflip = parseInt(document.getElementById('sim-input-antiflip')?.value || '90');
  const stagnation = parseInt(document.getElementById('sim-input-stagnation')?.value || '60');
  const maxai = 150;
  const cycleStart = parseFloat(document.getElementById('sim-input-cycle-start')?.value || '25');
  const cycleTarget = parseFloat(document.getElementById('sim-input-cycle-target')?.value || '100');
  const cycleMargin = parseFloat(document.getElementById('sim-input-cycle-margin')?.value || '25');
  const cycleLev = parseFloat(document.getElementById('sim-input-cycle-lev')?.value || '10');
  const stepped = document.getElementById('sim-input-stepped')?.checked ?? true;
  const stepHours = parseFloat(document.getElementById('sim-input-step-hours')?.value || '1.0');
  const stepSeconds = parseFloat(document.getElementById('sim-input-step-seconds')?.value || '60.0');
  const geminiKey = '';
  const openrouterKey = '';
  const hfTokens = '';
  const aiProvider = document.getElementById('sim-input-ai-provider')?.value || 'none';

  saveSimulatorSettings();
  if(btn) btn.disabled = true;
  setText('sim-val-status', 'Starting...');

  try {
    const res = await fetch('/api/simulator/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        strategy: strategy,
        years: years,
        tp_r: tp,
        sl_r: sl,
        anti_flip: antiflip,
        stagnation: stagnation,
        max_ai_reviews: maxai,
        cycle_start: cycleStart,
        cycle_target: cycleTarget,
        cycle_margin_pct: cycleMargin,
        cycle_leverage: cycleLev,
        stepped_mode: stepped,
        step_market_hours: stepHours,
        step_seconds: stepSeconds,
        gemini_key: geminiKey,
        openrouter_key: openrouterKey,
        hf_tokens: hfTokens,
        ai_provider: aiProvider
      })
    });
    if(res.ok){
      if(pauseBtn) pauseBtn.style.display = 'inline-flex';
      if(btn) btn.style.display = 'none';
      pollSimulatorStatus();
      if(aiProvider !== 'none') fetchAIEvents();
    } else {
      alert('Error starting simulator');
    }
  } catch(e){
    console.error('start simulator error:', e);
  } finally {
    if(btn) btn.disabled = false;
  }
}

async function pauseSimulator(){
  try {
    await fetch('/api/simulator/pause', { method: 'POST' });
    const btn = document.getElementById('sim-btn-start');
    const pauseBtn = document.getElementById('sim-btn-pause');
    if(btn) btn.style.display = 'inline-flex';
    if(pauseBtn) pauseBtn.style.display = 'none';
    setText('sim-val-status', 'Paused');
  } catch(e){
    console.error('pause simulator error:', e);
  }
}

let _currentCycleFilter = 'ALL';
let _cachedCycleData = null;
let _selectedCycleId = null;
let _currentCycleLedgerPage = 1;

function filterCycleStatements(filter){
  _currentCycleFilter = filter;
  ['all', 'won', 'busted'].forEach(id => {
    const el = document.getElementById('cs-tab-' + id);
    if(el) el.classList.remove('active');
  });
  if(filter === 'ALL') document.getElementById('cs-tab-all')?.classList.add('active');
  if(filter === 'TARGET_REACHED') document.getElementById('cs-tab-won')?.classList.add('active');
  if(filter === 'BUSTED') document.getElementById('cs-tab-busted')?.classList.add('active');
  if(_cachedCycleData) renderCycleStatements(_cachedCycleData);
}

function onCyclePickerChange(cycleId){
  _selectedCycleId = cycleId;
  _currentCycleLedgerPage = 1;
  if(_cachedCycleData) renderSelectedCycleCard(_cachedCycleData, cycleId);
  if(cycleId) loadCycleLedger(cycleId, 1);
}

function renderCycleStatements(cc){
  if(!cc || !cc.cycles) return;
  _cachedCycleData = cc;
  const picker = document.getElementById('sim-cycle-picker');
  const badge = document.getElementById('sim-cycle-picker-badge');
  if(!picker) return;

  let cycles = cc.cycles;
  if(_currentCycleFilter !== 'ALL'){
    cycles = cycles.filter(c => c.status === _currentCycleFilter);
  }

  if(badge) badge.innerText = `${cycles.length} of ${cc.cycles.length} cycles`;

  if(!cycles.length){
    picker.innerHTML = '<option value="">-- No matching cycles --</option>';
    const container = document.getElementById('sim-cycle-statements-container');
    if(container) container.innerHTML = '<div class="empty">No cycles match the selected filter.</div>';
    return;
  }

  const prevVal = _selectedCycleId || picker.value;
  const exists = cycles.some(c => String(c.cycle_id) === String(prevVal));
  if(!exists) {
    _selectedCycleId = String(cycles[cycles.length - 1].cycle_id);
  }

  picker.innerHTML = cycles.slice().reverse().map(c => {
    const isWon = c.status === 'TARGET_REACHED';
    const isBust = c.status === 'BUSTED';
    const icon = isWon ? '🎯 WON' : (isBust ? '🛑 BUST' : '⏳ RUNNING');
    const pnlStr = (c.net_profit_usdt >= 0 ? '+' : '') + '$' + (c.net_profit_usdt != null ? c.net_profit_usdt : 0) + ' USDT';
    return `<option value="${c.cycle_id}" ${String(c.cycle_id) === String(_selectedCycleId) ? 'selected' : ''}>Cycle #${c.cycle_id} [${icon}] ${pnlStr} (${c.total_trades || c.transaction_count || 0} trades)</option>`;
  }).join('');

  renderSelectedCycleCard(cc, _selectedCycleId);
  loadCycleLedger(_selectedCycleId, _currentCycleLedgerPage || 1);
}

function renderSelectedCycleCard(cc, cycleId){
  const container = document.getElementById('sim-cycle-statements-container');
  if(!container || !cc || !cc.cycles) return;
  const c = cc.cycles.find(item => String(item.cycle_id) === String(cycleId));
  if(!c) return;

  const isWon = c.status === 'TARGET_REACHED';
  const isBust = c.status === 'BUSTED';
  const statusColor = isWon ? '#10b981' : (isBust ? '#ef4444' : '#38bdf8');
  const statusBadge = isWon ? '🎯 TARGET HIT ($100)' : (isBust ? '🛑 BUSTED ($0)' : '⏳ IN PROGRESS');

  container.innerHTML = `
    <div style="background:#0f172a;border:1px solid #334155;border-radius:8px;padding:14px;margin-bottom:12px">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:15px;font-weight:700;color:#f1f5f9">Cycle #${c.cycle_id}</span>
          <span style="font-size:11px;padding:3px 8px;border-radius:4px;font-weight:700;background:${isWon ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.15)'};color:${statusColor};border:1px solid ${statusColor}">
            ${statusBadge}
          </span>
          <span style="font-size:12px;color:#94a3b8">${esc(c.start_time || '')} ➔ ${esc(c.end_time || 'ongoing')} (${esc(c.duration_str || '')})</span>
        </div>
        <div style="font-size:14px;font-weight:700;color:${c.net_profit_usdt >= 0 ? '#10b981' : '#ef4444'}">
          Net PnL: ${c.net_profit_usdt >= 0 ? '+' : ''}$${c.net_profit_usdt} USDT
        </div>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;font-size:11px;color:#cbd5e1;background:#030712;padding:10px;border-radius:6px;border:1px solid #1e293b;margin-bottom:12px">
        <div>Starting Balance: <b style="color:#f1f5f9">$${c.starting_balance}</b></div>
        <div>Ending Balance: <b style="color:${isWon ? '#10b981' : '#ef4444'}">$${c.ending_balance}</b></div>
        <div>Peak Balance: <b style="color:#10b981">$${c.peak_balance}</b></div>
        <div>Max Drawdown: <b style="color:#ef4444">${c.max_drawdown_pct}%</b></div>
        <div>Win Rate: <b style="color:#38bdf8">${c.win_rate_pct}%</b> (${c.wins || 0}W / ${c.losses || 0}L)</div>
        <div>Total Fees Paid: <b style="color:#94a3b8">$${c.total_fees_usdt || 0}</b></div>
      </div>

      <div id="cycle-ledger-container">
        <div style="text-align:center;padding:16px;color:#64748b;font-size:12px">Loading cycle ledger...</div>
      </div>
    </div>
  `;
}

async function loadCycleLedger(cycleId, page = 1){
  _currentCycleLedgerPage = page;
  const ledgerEl = document.getElementById('cycle-ledger-container');
  if(!ledgerEl || !cycleId) return;

  try {
    const res = await fetch(`/api/simulator/cycle-ledger?cycle_id=${encodeURIComponent(cycleId)}&page=${page}&limit=15`);
    if(!res.ok) {
      ledgerEl.innerHTML = '<div style="color:#ef4444;font-size:11px;padding:8px">Error loading transactions.</div>';
      return;
    }
    const data = await res.json();
    renderCycleLedgerTable(cycleId, data);
  } catch(e){
    console.error('loadCycleLedger error:', e);
    ledgerEl.innerHTML = '<div style="color:#ef4444;font-size:11px;padding:8px">Network error loading ledger.</div>';
  }
}

function renderCycleLedgerTable(cycleId, data){
  const ledgerEl = document.getElementById('cycle-ledger-container');
  if(!ledgerEl) return;
  const txs = data.transactions || [];
  const page = data.page || 1;
  const totalPages = data.total_pages || 1;
  const totalTx = data.total_transactions || txs.length;

  if(!txs.length){
    ledgerEl.innerHTML = '<div style="color:#64748b;font-size:11px;padding:8px">No transaction records in this cycle.</div>';
    return;
  }

  ledgerEl.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:6px">
      <div style="font-size:12px;font-weight:700;color:#38bdf8">Ledger Transactions (${totalTx} total):</div>
      <div style="display:flex;align-items:center;gap:6px;font-size:11px">
        <button ${page <= 1 ? 'disabled style="opacity:0.4;cursor:default"' : ''} onclick="loadCycleLedger('${cycleId}', ${page - 1})" style="background:#1e293b;color:#cbd5e1;border:1px solid #334155;padding:3px 8px;border-radius:4px;cursor:pointer">◀ Prev</button>
        <span style="color:#94a3b8">Page <b>${page}</b> of <b>${totalPages}</b></span>
        <button ${page >= totalPages ? 'disabled style="opacity:0.4;cursor:default"' : ''} onclick="loadCycleLedger('${cycleId}', ${page + 1})" style="background:#1e293b;color:#cbd5e1;border:1px solid #334155;padding:3px 8px;border-radius:4px;cursor:pointer">Next ▶</button>
      </div>
    </div>
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:11px;text-align:left">
        <thead>
          <tr style="background:#1e293b;color:#94a3b8">
            <th style="padding:6px 8px">#</th>
            <th style="padding:6px 8px">Timestamp</th>
            <th style="padding:6px 8px">Symbol</th>
            <th style="padding:6px 8px">Side</th>
            <th style="padding:6px 8px">Entry / Exit</th>
            <th style="padding:6px 8px">Exit Reason</th>
            <th style="padding:6px 8px">Margin</th>
            <th style="padding:6px 8px">Lev</th>
            <th style="padding:6px 8px">Fee</th>
            <th style="padding:6px 8px">Net PnL ($)</th>
            <th style="padding:6px 8px">Return %</th>
            <th style="padding:6px 8px">Balance After</th>
          </tr>
        </thead>
        <tbody>
          ${txs.map((tx, idx) => `
            <tr style="border-bottom:1px solid #1e293b;background:${idx % 2 === 0 ? 'rgba(15,23,42,0.4)' : 'rgba(30,41,59,0.2)'}">
              <td style="padding:6px 8px;color:#64748b">${tx.tx_id}</td>
              <td style="padding:6px 8px;color:#94a3b8">${esc(tx.timestamp || '')}</td>
              <td style="padding:6px 8px;font-weight:700;color:#f1f5f9">${esc(tx.symbol || '')}</td>
              <td style="padding:6px 8px"><span style="padding:2px 5px;border-radius:3px;font-size:10px;color:#fff;background:${tx.direction === 'LONG' ? '#10b981' : '#f43f5e'}">${esc(tx.direction || '')}</span></td>
              <td style="padding:6px 8px;color:#cbd5e1">${tx.entry_price} ➔ ${tx.exit_price}</td>
              <td style="padding:6px 8px;color:#94a3b8">${esc(tx.exit_reason || '')}</td>
              <td style="padding:6px 8px;color:#cbd5e1">$${tx.margin_usdt}</td>
              <td style="padding:6px 8px;color:#94a3b8">${tx.leverage}x</td>
              <td style="padding:6px 8px;color:#94a3b8">$${tx.fee_usdt}</td>
              <td style="padding:6px 8px;font-weight:700;color:${tx.net_pnl_usdt >= 0 ? '#10b981' : '#ef4444'}">${tx.net_pnl_usdt >= 0 ? '+' : ''}$${tx.net_pnl_usdt}</td>
              <td style="padding:6px 8px;font-weight:700;color:${tx.pnl_pct_on_margin >= 0 ? '#10b981' : '#ef4444'}">${tx.pnl_pct_on_margin >= 0 ? '+' : ''}${tx.pnl_pct_on_margin}%</td>
              <td style="padding:6px 8px;font-weight:700;color:#38bdf8">$${tx.balance_after}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

let _cachedRecentTrades = [];
let _recentTradesPage = 1;

function renderRecentTrades(trades, page = 1){
  if(trades) _cachedRecentTrades = trades;
  _recentTradesPage = page;
  const streamEl = document.getElementById('sim-trade-stream');
  if(!streamEl) return;

  const allTrades = _cachedRecentTrades || [];
  if(!allTrades.length){
    streamEl.innerHTML = '<div class="empty">No simulator run active yet. Click "Run Simulator" to begin.</div>';
    return;
  }

  const limit = 10;
  const totalPages = Math.max(1, Math.ceil(allTrades.length / limit));
  const curPage = Math.min(Math.max(1, _recentTradesPage), totalPages);
  const start = (curPage - 1) * limit;
  const pageTrades = allTrades.slice(start, start + limit);

  const controls = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding:6px 10px;background:#090d16;border-radius:6px;border:1px solid #1e293b;font-size:11px">
      <span style="color:#94a3b8">Showing ${start + 1}-${Math.min(start + limit, allTrades.length)} of <b>${allTrades.length}</b> trades</span>
      <div style="display:flex;align-items:center;gap:6px">
        <button ${curPage <= 1 ? 'disabled style="opacity:0.4;cursor:default"' : ''} onclick="renderRecentTrades(null, ${curPage - 1})" style="background:#1e293b;color:#cbd5e1;border:1px solid #334155;padding:3px 8px;border-radius:4px;cursor:pointer">◀ Prev</button>
        <span style="color:#38bdf8">Page <b>${curPage}</b> / <b>${totalPages}</b></span>
        <button ${curPage >= totalPages ? 'disabled style="opacity:0.4;cursor:default"' : ''} onclick="renderRecentTrades(null, ${curPage + 1})" style="background:#1e293b;color:#cbd5e1;border:1px solid #334155;padding:3px 8px;border-radius:4px;cursor:pointer">Next ▶</button>
      </div>
    </div>
  `;

  const tradeCards = pageTrades.map(t => `
    <div style="background:#0f172a;border:1px solid #334155;border-radius:8px;padding:12px;margin-bottom:8px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <div>
          <b style="color:#f1f5f9">${esc(t.symbol)}</b>
          <span style="margin-left:6px;font-size:11px;padding:2px 6px;border-radius:4px;background:${t.direction === 'LONG' ? '#10b981' : '#f43f5e'};color:#fff">${esc(t.direction)}</span>
          ${t.source_model ? `<span style="margin-left:6px;font-size:10px;padding:1px 5px;border-radius:3px;background:#1e293b;color:#38bdf8;border:1px solid #334155">${esc(t.source_model)}</span>` : ''}
          <span style="margin-left:8px;font-size:11px;color:#94a3b8">${esc(t.entry_time)} ➔ ${esc(t.exit_time || 'running')}</span>
        </div>
        <div>
          <b style="color:${t.pnl_r >= 0 ? '#10b981' : '#ef4444'}">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct}% (${t.pnl_r >= 0 ? '+' : ''}${t.pnl_r}R)</b>
          <span style="font-size:11px;color:#64748b;margin-left:6px">${esc(t.exit_reason)}</span>
        </div>
      </div>
      <div style="font-size:11px;color:#cbd5e1;margin-bottom:6px">
        Peak Gain: <b style="color:#10b981">+${t.peak_gain_pct}%</b> &middot; Target Covered: <b>${t.target_pct_reached}%</b> &middot; Max Drawdown: <b style="color:#ef4444">${t.max_drawdown_pct}%</b>
      </div>
      ${t.why_it_worked ? `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:6px 0;font-size:11px">
        <div style="background:rgba(16,185,129,0.06);padding:6px;border-radius:4px;color:#a7f3d0"><b>Why Worked:</b> ${esc(t.why_it_worked)}</div>
        <div style="background:rgba(239,68,68,0.06);padding:6px;border-radius:4px;color:#fca5a5"><b>Why Failed:</b> ${esc(t.why_it_failed)}</div>
      </div>` : ''}
      ${t.thinking_trace ? `
      <details style="margin-top:6px;font-size:10.5px;color:#94a3b8">
        <summary style="cursor:pointer;color:#38bdf8">🧠 Heuristic / AI Trace</summary>
        <pre style="margin-top:4px;background:#030712;padding:8px;border-radius:4px;overflow-x:auto;color:#e2e8f0;white-space:pre-wrap">${esc(t.thinking_trace)}</pre>
      </details>` : ''}
    </div>
  `).join('');

  streamEl.innerHTML = controls + tradeCards + controls;
}

function renderSimulator(data){
  if(!data) return;
  const isRunning = data.is_running;
  const btn = document.getElementById('sim-btn-start');
  const pauseBtn = document.getElementById('sim-btn-pause');
  if(btn && pauseBtn){
    btn.style.display = isRunning ? 'none' : 'inline-flex';
    pauseBtn.style.display = isRunning ? 'inline-flex' : 'none';
  }

  setText('sim-val-status', isRunning ? 'Running' : (data.progress_pct >= 100 ? 'Completed' : 'Idle'));
  setText('sim-sub-status', data.current_symbol ? ('Active: ' + data.current_symbol) : (isRunning ? 'Simulating...' : 'Ready'));
  setText('sim-val-ticks', (data.ticks_processed || 0).toLocaleString());
  setText('sim-val-trades', (data.trades_simulated || 0).toLocaleString());
  setText('sim-sub-trades', `${data.won_count || 0} won · ${data.stopped_count || 0} lost · ${data.partial_count || 0} partial`);
  setText('sim-val-wr', (data.win_rate_pct || 0).toFixed(1) + '%');
  setText('sim-sub-pf', 'Profit Factor: ' + (data.profit_factor || 1.0).toFixed(2));
  setText('sim-val-hf', (data.hf_calls_succeeded || 0) + ' / ' + (data.hf_calls_dispatched || 0));
  setText('sim-val-time', fmtUptime(data.elapsed_seconds || 0));

  const pct = Math.min(100, Math.max(0, data.progress_pct || 0));
  const pBar = document.getElementById('sim-progress-bar');
  if(pBar) pBar.style.width = pct + '%';
  setText('sim-progress-label', `Simulation Progress: ${pct.toFixed(1)}%`);

  // Update Stepped Clock & Pacing Monitor
  if(data.current_market_time){
    setText('sim-clock-market', data.current_market_time + (data.total_steps ? (' (Step #' + (data.current_step || 1) + ' / ' + data.total_steps + ')') : ''));
  }
  if(data.is_overtime){
    setText('sim-clock-countdown', '⚠️ Overtime: ' + (data.step_seconds_elapsed || 0) + 's (Processing...)');
  } else if(data.step_budget_seconds){
    setText('sim-clock-countdown', '⏱️ ' + (data.step_seconds_elapsed || 0) + 's / ' + data.step_budget_seconds + 's (' + (data.step_countdown_remaining || 0) + 's remaining)');
  }

  // Update Multi-Provider AI Telemetry
  if(data.ai_telemetry && data.ai_telemetry.providers){
    const p = data.ai_telemetry.providers;
    if(p.groq) setText('sim-quota-groq', p.groq.rpm_used + ' / ' + p.groq.rpm_limit + ' RPM');
    if(p.gemini) setText('sim-quota-gemini', p.gemini.rpm_used + ' / ' + p.gemini.rpm_limit + ' RPM');
    if(p.openrouter) setText('sim-quota-or', p.openrouter.rpm_used + ' / ' + p.openrouter.rpm_limit + ' RPM');
    if(p.hf) setText('sim-quota-hf', p.hf.rpm_used + ' / ' + p.hf.rpm_limit + ' RPM');
  }
  if(isRunning && _currentSimView === 'events') fetchAIEvents();

  // Render Cycle Challenge Scorecard & Statements
  if(data.cycle_challenge && data.cycle_challenge.total_cycles != null){
    const cc = data.cycle_challenge;
    setText('sim-cycle-total', cc.total_cycles);
    setText('sim-cycle-won', cc.targets_hit);
    setText('sim-cycle-won-sub', (cc.targets_hit || 0) + ' cycles reached $' + (cc.target_capital || 100));
    setText('sim-cycle-busted', cc.busted);
    setText('sim-cycle-busted-sub', (cc.busted || 0) + ' cycles ruined to $0');
    setText('sim-cycle-wr', (cc.cycle_win_rate_pct || 0).toFixed(1) + '%');
    setText('sim-cycle-profit', (cc.total_net_profit_usdt >= 0 ? '+' : '') + '$' + (cc.total_net_profit_usdt || 0).toFixed(2));
    setText('sim-cycle-trades', (cc.avg_trades_per_cycle || 0).toFixed(1) + ' trades');
    renderCycleStatements(cc);
  }

  // Render recent trade cards with client pagination
  if(data.recent_trades && data.recent_trades.length){
    renderRecentTrades(data.recent_trades, _recentTradesPage || 1);
  }
}


// Both readings, because they answer different questions: the stamp says
// which row this was, the relative time says whether to care.
//
// It used to build the stamp itself, with two faults. `new Date(iso)` on a
// timestamp carrying no zone reads it as the viewer's local time rather than
// UTC, which is what the wire actually sends — so every signal rendered five
// and a half hours early. And the result was labelled "IST" while being
// formatted in whatever zone the browser happened to be in, so the label was
// right only by coincidence.
function fmtSignalTime(iso){
  if(!iso) return 'time unknown';
  const stamp = window.fmtStamp(iso);
  if(stamp === '—') return 'time unknown';
  return stamp + ' IST · ' + window.fmtAgo(iso);
}

function renderCryptoSignalCard(s){
  const name = CR_SIG_NAME[s.signal_type] || s.signal_type.replace(/_/g,' ');
  const long = s.direction === 'long';
  const entry = s.current_price, tp = s.target_price, sl = s.stop_loss;
  const move  = (tp && entry) ? Math.abs(tp - entry) / entry * 100 : 0;
  const risk  = (sl && entry) ? Math.abs(entry - sl) / entry * 100 : 0;
  const xcost = move / BREAK_EVEN_PCT;
  const viable = move >= MIN_TARGET_PCT;
  const mode = s.trade_mode || 'intraday';
  const isDelivery = mode === 'delivery';
  // No leverage on the badge: the paper book sizes each trade itself, so a 'suggested' leverage here
  // showed a number no trade actually used.
  const modeBadge = mode === 'swing'
    ? `<span class="pt-mode deliv">Swing ${s.timeframe || '4h'}</span>`
    : isDelivery
      ? `<span class="pt-mode deliv">📦 Delivery</span>`
      : `<span class="pt-mode intra">⚡ Intraday</span>`;
  const roe = move * PAPER_LEVERAGE;

  // The track runs stop -> target, so it reads left-to-right the same way for
  // a long and a short even though price moves the opposite way.
  const pos = p => (tp === sl) ? 50
    : Math.max(0, Math.min(100, (p - sl) / (tp - sl) * 100));
  const at = pos(entry);

  const rr = risk > 0 ? (move / risk) : null;
  const cls = viable ? (long ? 'long' : 'short') : 'unviable';

  return `<div class="sig ${cls}">
    <div class="sig-head">
      <span class="sig-sym">${esc(s.symbol)}</span>
      <span class="sig-dir ${long?'long':'short'}">${long?'Long':'Short'}</span>
      ${modeBadge}
      <div style="flex-grow:1"></div>
      <div class="sig-profit ${viable?'':'muted'}">
        <b>${viable?'+':''}${roe.toFixed(1)}%</b><span>expected</span></div>
    </div>

    <div class="sig-meta">
      <span class="sig-setup">${esc(name)}</span>
      <span>&middot;</span><span class="sig-horizon" title="How long this move should take at this market's own pace — derived from the distance and the recent range, not a fixed window">~${esc(s.timeframe)} to target</span>
      <span>&middot;</span><span>${s.confidence}% confidence</span>
      <div style="flex-grow:1"></div>
      <span class="sig-when">${fmtSignalTime(s.timestamp)}</span>
    </div>

    <div class="sig-levels">
      <div><label>Stop loss</label><b class="neg">${fmtPrice(sl)}</b><span>−${risk.toFixed(2)}%</span></div>
      <div class="mid"><label>Entry</label><b>${fmtPrice(entry)}</b><span>LTP</span></div>
      <div class="right"><label>Take profit</label><b class="pos">${fmtPrice(tp)}</b><span>+${move.toFixed(2)}%</span>${s.tp1_price ? `<div style="font-size:9.5px;color:var(--pos)">TP1: ${fmtPrice(s.tp1_price)}</div>` : ''}</div>
    </div>

    <div class="sig-track">
      <span class="cap sl"></span>
      <span class="cap tp"></span>
      <span class="fill" style="width:${at}%"></span>
      <span class="now" style="left:${at}%"></span>
    </div>

    ${trailPlan(entry, sl, long)}

    <div class="sig-foot">
      <span class="pill ${viable?'ok':'bad'}">${xcost.toFixed(1)}× cost</span>
      ${rr?`<span class="pill">${rr.toFixed(2)} reward:risk</span>`:''}
      <span class="pill">${move.toFixed(3)}% move</span>
      <div style="flex-grow:1"></div>
      <span class="sig-act ${!viable?'off':(s.veto_reason?'off':(s.skip_reason?'off':(long?'long':'short')))}">${
        !viable ? 'Refused' : (s.veto_reason ? 'Vetoed' : (s.skip_reason ? 'Not traded' : (long?'Buy / Long':'Sell / Short')))}</span>
    </div>

    ${s.veto_reason ? `<div class="sig-warn" style="border-left-color:var(--neg);background:rgba(248,113,113,0.08);color:var(--neg)">
      <b>🚫 Hard Veto:</b> ${esc(s.veto_reason)}
    </div>` : ''}

    ${(viable && !s.veto_reason && s.skip_reason)?`<div class="sig-warn">Fired, but did not become a paper
      trade: <b>${esc(s.skip_reason_text||s.skip_reason)}</b>. The cost/move numbers above
      passed — this is a different gate (confidence, session, protections, or the book
      already full) deciding it, not this card's own math.</div>`:''}

    ${viable?'':`<div class="sig-warn">Target is ${move.toFixed(3)}% away against a
      ${BREAK_EVEN_PCT.toFixed(3)}% round trip. ${xcost <= 1
        ? 'It costs more to open and close than the move can win, so this loses money when it succeeds.'
        : `It would keep only ${(100-100/xcost).toFixed(0)}% of what it earns.`}
      The bot will not take a trade under ${MIN_TARGET_PCT.toFixed(3)}%.</div>`}

    ${renderReviewTrail(s)}
  </div>`;
}

// Mirror review's per-signal expandable "Review trail" — same collapsed-by-
// default, click-to-open pattern as the rest of the Signals page's warning
// blocks, just toggled instead of always shown since a trail can run to
// six rows (primary + mirror, three rounds each) on top of an already
// dense card.
function renderReviewTrail(s){
  const trail = s.review_trail;
  if(!trail || !trail.length) return '';
  const domId = 'trail-' + s.id;
  const rows = trail.map(t => `<div class="trail-row">
      <span class="trail-role">${esc(t.label)}</span>
      <span class="trail-round">round ${t.review_round}</span>
      <span class="trail-conf">${t.confidence}% confidence</span>
      <span class="trail-when">${fmtSignalTime(t.timestamp)}</span>
      <span class="trail-status ${
        /^Traded/.test(t.status)?'ok':(/^Rejected/.test(t.status)?'bad':'')
      }">${esc(t.status)}</span>
    </div>`).join('');
  return `<div class="sig-trail-toggle"
      onclick="document.getElementById('${domId}').classList.toggle('open')">
      Review trail (${trail.length} rounds) ▾
    </div>
    <div class="sig-review-trail" id="${domId}">${rows}</div>`;
}

// The exit, in prices, because the venue's TP/SL box takes prices. A 2x
// reward:risk and a trail are one decision: the target alone caps the winner
// that pays for the losers, and a trail behind a 1R target never arms.
function trailPlan(entry, stop, long){
  const risk = Math.abs(entry - stop);
  if(!entry || !risk) return '';
  const sign = long ? 1 : -1;
  const arm = entry + sign * 0.75 * risk;
  const be  = entry * (1 + sign * BREAK_EVEN_PCT/100);
  return `<div class="sig-trail">
    <span class="sig-trail-k">Trail</span>
    <span>at <b>${fmtPrice(arm)}</b> move the stop to <b>${fmtPrice(be)}</b>,
      then keep it <b>${fmtDelta(risk, entry)}</b> behind the best price</span>
  </div>`;
}

function renderCommodities(rows){
  const sec=document.getElementById('cr-commodities-section');
  const el=document.getElementById('cr-commodities');
  if(!rows||!rows.length){sec.style.display='none';return;}
  sec.style.display='';
  el.innerHTML='<div class="cr-commodity">'+rows.map(r=>`<div class="cr-comm-card">
    <div class="cr-comm-name">${esc(r.name)}</div><div class="cr-comm-price">$${fmtPrice(r.price)}</div>
  </div>`).join('')+'</div>';
}
async function addCryptoSymbol(){
  const inp=document.getElementById('cr-add-input');
  const sym=inp.value.trim().toLowerCase();
  if(!sym) return;
  try {
    const res = await apiFetch('/api/crypto/watchlist/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym})});
    let body={}; try{ body=await res.json(); }catch(e){}
    if(!res.ok){
      alert(body.error || ('Could not add ' + sym.toUpperCase() + ': HTTP ' + res.status));
      return;
    }
    inp.value='';
    refresh();
  } catch(err) {
    alert('Failed to add ' + sym.toUpperCase() + ': ' + err.message);
  }
}
async function removeCryptoSymbol(sym){
  if(!confirm('Remove ' + sym.toUpperCase() + ' from watchlist?')) return;
  try {
    const res = await apiFetch('/api/crypto/watchlist/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym})});
    let body={}; try{ body=await res.json(); }catch(e){}
    if(!res.ok){
      alert(body.error || ('Could not remove ' + sym.toUpperCase() + ': HTTP ' + res.status));
      return;
    }
    refresh();
  } catch(err) {
    alert('Failed to remove ' + sym.toUpperCase() + ': ' + err.message);
  }
}

// ── MAIN ──────────────────────────────────────────────────────────────────────
// Fetch JSON that never rejects — a single failing endpoint must not blank the whole dashboard.
function jget(url,fallback){return fetch(url).then(r=>r.ok?r.json():fallback).catch(()=>fallback);}
function setText(id, value){
  const el = document.getElementById(id);
  if(el) el.textContent = value;
  return !!el;
}
function setHTML(id, value){
  const el = document.getElementById(id);
  if(el) el.innerHTML = value;
  return !!el;
}

// Which tabs need which payload. Rebuilding a panel nobody is looking at
// still costs a full style recalc, layout and paint — every thirty seconds,
// for markup that is display:none. The crypto cards alone were being torn
// down and rebuilt while the Dashboard was on screen, which is most of the
// jank this page had.
const TAB_NEEDS = {
  dashboard: ['coins','signals'],
  crypto:    ['coins','signals','commodities'],
  mirror:    ['mirror_signals'],
  simulator: ['simulator'],
  watchlist: ['coins'],
  paper:     ['paper'],
};
function _activeTab(){
  const el = document.querySelector('.tab-content.active');
  return el ? el.id.replace(/^tab-/, '') : 'dashboard';
}

async function refresh(){
  // Nothing on a backgrounded tab is worth a request. The browser throttles
  // the timer anyway; this stops the work the timer would still queue up.
  if(document.hidden) return;   // no label change: a backgrounded tab has no reader
  try{
    const need = TAB_NEEDS[_activeTab()] || [];
    const status = await jget('/api/status',{});
    // The sidebar footer is where uptime lives.
    setText('side-uptime', 'up ' + fmtUptime(status.uptime_seconds));
    setText('side-status', 'Running');

    if(need.includes('coins') || need.includes('signals') || need.includes('commodities') || need.includes('mirror_signals')){
      const [crCoins,crSignals,crCommodities, crMirrorSignals]=await Promise.all([
        need.includes('coins')       ? jget('/api/crypto/coins',[])   : Promise.resolve(null),
        need.includes('signals')     ? jget('/api/crypto/signals',[]) : Promise.resolve(null),
        need.includes('commodities') ? jget('/api/commodities',[])    : Promise.resolve(null),
        need.includes('mirror_signals') ? jget('/api/crypto/signals?mirror=1',[]) : Promise.resolve(null),
      ]);
      if(crCoins){ setText('stat-crypto-coins', crCoins.length); renderCryptoCoins(crCoins); }
      if(crSignals){ setText('stat-crypto-signals', crSignals.length); renderCryptoSignals(crSignals); }
      if(crCommodities) renderCommodities(crCommodities);
      if(crMirrorSignals){ setText('stat-mirror-signals', crMirrorSignals.length || (crMirrorSignals.signals && crMirrorSignals.signals.length)); renderMirrorSignals(crMirrorSignals); }
    }

    if(need.includes('simulator')){
      const simStatus = await jget('/api/simulator/status', {});
      renderSimulator(simStatus);
    }

    if(need.includes('paper')) await loadPaper();

    setText('last-updated', 'Updated ' + window.fmtStamp(new Date().toISOString(), {seconds:true}));
    setText('refresh-label', 'Next in 30s');
  }catch(e){
    console.error('refresh error:', e);
    setText('refresh-label', 'Error — retrying…');
  }
}

// Coming back to a tab that was paused should show current data at once,
// not whatever was on screen when it was hidden.
// iOS restores a backgrounded tab from cache without always firing
// visibilitychange, which left the page showing whatever it had when it went
// away. pageshow covers that path; both are cheap and idempotent.
document.addEventListener('visibilitychange', () => { if(!document.hidden) refresh(); });
window.addEventListener('pageshow', () => { if(!document.hidden) refresh(); });

switchTab((location.hash||'#dashboard').slice(1));
refresh();
setInterval(refresh,30000);
</script>
</body>
</html>"""


_DATA_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Database Dump — Crypto Signal Engine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh;padding-bottom:40px}
header{background:#1e293b;border-bottom:1px solid #334155;padding:14px 20px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
header h1{font-size:17px;font-weight:700;color:#f1f5f9;display:flex;align-items:center;gap:8px}
header a.nav-btn{background:#0ea5e9;color:#fff;font-size:13px;font-weight:600;padding:6px 14px;border-radius:8px;text-decoration:none;white-space:nowrap}
header a.nav-btn:hover{background:#0284c7}
.controls{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:13px;color:#94a3b8}
.controls input{width:70px;background:#0f172a;border:1px solid #334155;color:#e2e8f0;border-radius:6px;padding:4px 8px}
.controls button{background:#0ea5e9;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-weight:600;cursor:pointer}
.controls button:hover{background:#0284c7}
.toc{padding:12px 20px;display:flex;flex-wrap:wrap;gap:8px}
.toc a{font-size:12px;background:#1e293b;border:1px solid #334155;color:#cbd5e1;padding:4px 10px;border-radius:9999px;text-decoration:none}
.toc a:hover{border-color:#0ea5e9;color:#fff}
.toc a b{color:#38bdf8}
section{padding:8px 20px 20px}
.tbl-head{display:flex;align-items:baseline;gap:10px;margin:18px 0 8px;border-bottom:1px solid #334155;padding-bottom:6px}
.tbl-head h2{font-size:15px;font-weight:700;color:#f1f5f9}
.tbl-head .count{font-size:12px;color:#64748b}
.tbl-head .count b{color:#22c55e}
.scroll{overflow-x:auto;border:1px solid #334155;border-radius:8px}
table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}
th,td{border:1px solid #1e293b;padding:5px 9px;text-align:left;max-width:360px;overflow:hidden;text-overflow:ellipsis}
th{background:#1e293b;color:#94a3b8;position:sticky;top:0;font-weight:600}
tr:nth-child(even) td{background:#172033}
td.null{color:#475569;font-style:italic}
.empty{color:#475569;font-size:13px;padding:14px 0}
.err{color:#f87171;font-size:12px;padding:8px 0}
.note{color:#64748b;font-size:12px;padding:0 20px}
#status{color:#94a3b8;font-size:12px}

@media(max-width:640px){
  html{-webkit-text-size-adjust:100%}
  body{padding:12px}
  table{font-size:12px}
  th,td{padding:7px 8px}
  input,select,textarea,button{min-height:40px;font-size:16px}
  .grid,.cards{grid-template-columns:1fr !important}
  pre{font-size:11px;overflow-x:auto}
}
</style>
</head>
<body>
<header>
  <h1>🗄️ Database Dump</h1>
  <a class="nav-btn" href="/">← Home</a>
  <a class="nav-btn" href="/settings">⚙️ Settings</a>
  <div class="controls">
    <span id="status">loading…</span>
    <label>rows/table
      <input id="limit" type="number" min="1" max="2000" value="100">
    </label>
    <button onclick="load()">Reload</button>
  </div>
</header>
<div class="toc" id="toc"></div>
<div class="note">Newest rows first (by primary key). Increase rows/table to dump more — capped at 2000 per table.</div>
<div id="tables"></div>
<script>
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
// An ISO timestamp straight from the database reads as
// "2026-09-24T10:09:00.123456" — technically a date and a time, and in UTC,
// which is five and a half hours from the only clock the operator has. The
// raw value stays in the title, because this page exists for checking what is
// actually stored and a formatted-only view would hide it.
const _ISO = /^\\d{4}-\\d{2}-\\d{2}[T ]\\d{2}:\\d{2}/;
function cell(v){
  if(v===null||v===undefined) return '<td class="null">NULL</td>';
  if(typeof v === 'string' && _ISO.test(v)){
    return '<td title="'+esc(v)+'">'+esc(window.fmtStamp(v, {seconds:true}))+'</td>';
  }
  return '<td title="'+esc(v)+'">'+esc(v)+'</td>';
}
function renderTable(t){
  const head='<div class="tbl-head" id="t_'+esc(t.name)+'">'
    +'<h2>'+esc(t.name)+'</h2>'
    +'<span class="count">showing <b>'+t.shown+'</b> of '+t.total.toLocaleString()+' rows</span></div>';
  if(t.error) return head+'<div class="err">error: '+esc(t.error)+'</div>';
  if(!t.rows.length) return head+'<div class="pnl pnl-empty"><div class="pnl-t">No rows</div><div class="pnl-d">This table exists but nothing has written to it yet.</div></div>';
  let h='<div class="scroll"><table class="tbl"><thead><tr>';
  for(const c of t.columns) h+='<th>'+esc(c)+'</th>';
  h+='</tr></thead><tbody>';
  for(const row of t.rows){
    h+='<tr>';
    for(const v of row) h+=cell(v);
    h+='</tr>';
  }
  h+='</tbody></table></div>';
  return head+h;
}
async function load(){
  const lim=Math.max(1,Math.min(2000,parseInt(document.getElementById('limit').value)||100));
  document.getElementById('status').textContent='loading…';
  try{
    const resp=await fetch('/api/tables?limit='+lim);
    if(!resp.ok) throw new Error('server returned ' + resp.status);
    const data=await resp.json();
    const tables=data.tables||[];
    document.getElementById('toc').innerHTML=tables.map(t=>
      '<a href="#t_'+esc(t.name)+'">'+esc(t.name)+' <b>'+t.total.toLocaleString()+'</b></a>').join('');
    document.getElementById('tables').innerHTML=
      tables.map(t=>'<section>'+renderTable(t)+'</section>').join('');
    const totRows=tables.reduce((a,t)=>a+t.total,0);
    document.getElementById('status').textContent=
      tables.length+' tables · '+totRows.toLocaleString()+' rows total';
  }catch(e){
    document.getElementById('status').textContent='failed';
    Panel.error(document.getElementById('tables'), 'the tables', e, load);
  }
}
load();
</script>
</body>
</html>"""


_SECRET_WORDS = ("key", "token", "secret", "password", "salt", "auth", "credential")


def _looks_secret(name: str) -> bool:
    """A column or setting name that may hold a credential. Errs on the side of hiding."""
    n = name.lower()
    return any(w in n for w in _SECRET_WORDS) and n not in ("key",)


async def _api_tables(runner, request: web.Request) -> web.Response:
    """Dump every table in the database (reflected, so it covers all tables).

    Query params:
      limit — rows per table (default 100, max 2000)
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    from storage.database import engine
    from storage.models import SECRET_COLUMNS

    try:
        limit = max(1, min(int(request.query.get("limit", "100")), 2000))
    except ValueError:
        limit = 100

    out: dict = {"generated_at": _iso(datetime.now(UTC)), "limit": limit,
                 "tables": []}

    async with engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: sa_inspect(sync_conn).get_table_names()
        )
        pk_map = await conn.run_sync(
            lambda sync_conn: {
                t: sa_inspect(sync_conn).get_pk_constraint(t).get("constrained_columns", [])
                for t in table_names
            }
        )

        for name in sorted(table_names):
            try:
                total = (await conn.execute(
                    text(f'SELECT COUNT(*) FROM "{name}"'))).scalar() or 0
            except Exception:
                total = 0

            # Newest-first when there's a single-column primary key, else natural order.
            pks = pk_map.get(name) or []
            order = f' ORDER BY "{pks[0]}" DESC' if len(pks) == 1 else ""
            cols: list[str] = []
            rows: list[list] = []
            error = None
            try:
                result = await conn.execute(
                    text(f'SELECT * FROM "{name}"{order} LIMIT :lim'), {"lim": limit})
                cols = list(result.keys())
                rows = [list(r) for r in result.fetchall()]
                # This endpoint is unauthenticated, and admin_auth holds a live
                # session token: returning it handed anyone the operator's
                # login. Secrets are blanked here rather than trusted to a
                # caller check, so a future route change cannot re-expose them.
                secret = SECRET_COLUMNS.get(name, frozenset())
                hide = [i for i, c in enumerate(cols) if c in secret or _looks_secret(c)]
                for row in rows:
                    for i in hide:
                        row[i] = "[redacted]"
                # Key/value tables (app_settings) keep API keys as rows, not columns: the OpenRouter
                # key and the HF token were readable here by anyone with the server's address.
                if "key" in cols and "value" in cols:
                    ki, vi = cols.index("key"), cols.index("value")
                    for row in rows:
                        if _looks_secret(str(row[ki])):
                            row[vi] = "[redacted]"
            except Exception as exc:
                error = str(exc)

            out["tables"].append({
                "name": name,
                "columns": cols,
                "rows": rows,
                "shown": len(rows),
                "total": total,
                "error": error,
            })

    return web.Response(text=json.dumps(out, default=str),
                        content_type="application/json")


async def _data_page(request: web.Request) -> web.Response:
    return web.Response(text=_DATA_HTML, content_type="text/html")


async def _settings_page(request: web.Request) -> web.Response:
    return web.Response(text=_SETTINGS_HTML, content_type="text/html")


async def _api_auth_verify(runner, request: web.Request) -> web.Response:
    """
    POST /api/auth/verify — does this token work?

    The whole endpoint. Used exactly once by a new client (the iOS app
    entering a token for the first time, gated behind Face ID before it is
    written to Keychain) to confirm the token is right before trusting it for
    everything else. It carries a far tighter rate limit than the rest of the
    API (see scheduler/security.py) precisely because its job is accepting
    attempts at the shared secret.
    """
    from scheduler.security import check_bearer_auth
    denied = check_bearer_auth(request, _SETTINGS.api_auth_token)
    if denied is not None:
        return denied
    return _json_response({"ok": True})


ADMIN_COOKIE = "admin_session"
ADMIN_SESSION_DAYS = 7


def _session_token(request: web.Request) -> str:
    """The admin token from the header, a Bearer header, or the 7-day cookie."""
    token = request.headers.get("X-Settings-Token") or ""
    if not token:
        auth_hdr = request.headers.get("Authorization") or ""
        if auth_hdr.startswith("Bearer "):
            token = auth_hdr[7:].strip()
    return token or request.cookies.get(ADMIN_COOKIE, "")


async def _verify_admin_session(request: web.Request) -> bool:
    """
    Check X-Settings-Token or a Bearer header against the stored session.

    Headers only. The ?token= fallback that used to sit here was never used
    by the dashboard or the iOS app, and a session token in a URL is a
    session token in the access log, the browser history and whatever
    Referer gets sent to the next site the tab visits.
    """
    token = _session_token(request)
    if not token:
        return False
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    try:
        async with AsyncSessionFactory() as session:
            return await Repository(session).validate_session_token(token)
    except Exception:
        return False


async def _api_settings_auth_login(runner, request: web.Request) -> web.Response:
    try:
        body = await request.json()
        password = str(body.get("password") or "")
        if not password:
            return _json_response({"ok": False, "error": "Password required"}, status=400)
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            ok, token = await repo.verify_admin_password(password)
            if not ok or not token:
                return _json_response({"ok": False, "error": "Invalid password"}, status=401)
            # Logged in for 7 days in every tab and page of this browser: an
            # HttpOnly cookie (scripts cannot read it) that other sites cannot
            # send (SameSite=Strict). The token is still returned for the
            # iOS app and older pages that send it as a header.
            resp = _json_response({"ok": True, "token": token,
                                      "expires_days": ADMIN_SESSION_DAYS})
            resp.set_cookie(ADMIN_COOKIE, token, max_age=ADMIN_SESSION_DAYS * 86400,
                            httponly=True, samesite="Strict", path="/",
                            secure=request.secure)
            return resp
    except Exception as exc:
        return _json_response({"ok": False, "error": str(exc)}, status=500)


async def _api_settings_auth_status(runner, request: web.Request) -> web.Response:
    ok = await _verify_admin_session(request)
    return _json_response({"authenticated": ok})


async def _api_settings_auth_logout(runner, request: web.Request) -> web.Response:
    token = _session_token(request)
    if token:
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        try:
            async with AsyncSessionFactory() as session:
                await Repository(session).invalidate_session_token(token)
        except Exception:
            pass
    resp = _json_response({"ok": True})
    resp.del_cookie(ADMIN_COOKIE, path="/")
    return resp


async def _api_paper_config_get(runner, request: web.Request) -> web.Response:
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    async with AsyncSessionFactory() as session:
        cfg = await Repository(session).get_paper_config()
        return _json_response({
            "enabled": cfg.enabled,
            "starting_wallet": cfg.starting_wallet,
            "target_wallet": cfg.target_wallet,
            "leverage": cfg.leverage,
            "stop_pct_of_margin": cfg.stop_pct_of_margin,
            "reward_risk": cfg.reward_risk,
            "min_confidence": cfg.min_confidence,
            "max_concurrent": cfg.max_concurrent,
            "max_hold_minutes": cfg.max_hold_minutes,
            "scaled_sizing": cfg.scaled_sizing,
            "trailing_enabled": cfg.trailing_enabled,
            "scaled_leverage": cfg.scaled_leverage,
            "ladder_enabled": cfg.ladder_enabled,
            "ladder_tight": cfg.ladder_tight,
            "max_leverage": cfg.max_leverage,
            "usdt_inr": cfg.usdt_inr,
            "alert_telegram": cfg.alert_telegram,
            "sizing_floor_pct": cfg.sizing_floor_pct,
            "sizing_ceiling_pct": cfg.sizing_ceiling_pct,
        })


async def _api_paper_config_post(runner, request: web.Request) -> web.Response:
    is_admin = await _verify_admin_session(request)
    if not is_admin:
        return _json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            cfg = await repo.update_paper_config(
                enabled=bool(body["enabled"]) if "enabled" in body else None,
                starting_wallet=float(body["starting_wallet"]) if "starting_wallet" in body else None,
                target_wallet=float(body["target_wallet"]) if "target_wallet" in body else None,
                leverage=float(body["leverage"]) if "leverage" in body else None,
                stop_pct_of_margin=float(body["stop_pct_of_margin"]) if "stop_pct_of_margin" in body else None,
                reward_risk=float(body["reward_risk"]) if "reward_risk" in body else None,
                min_confidence=float(body["min_confidence"]) if "min_confidence" in body else None,
                max_concurrent=int(body["max_concurrent"]) if "max_concurrent" in body else None,
                max_hold_minutes=int(body["max_hold_minutes"]) if "max_hold_minutes" in body else None,
                scaled_sizing=bool(body["scaled_sizing"]) if "scaled_sizing" in body else None,
                trailing_enabled=bool(body["trailing_enabled"]) if "trailing_enabled" in body else None,
                scaled_leverage=bool(body["scaled_leverage"]) if "scaled_leverage" in body else None,
                ladder_enabled=bool(body["ladder_enabled"]) if "ladder_enabled" in body else None,
                ladder_tight=bool(body["ladder_tight"]) if "ladder_tight" in body else None,
                max_leverage=float(body["max_leverage"]) if "max_leverage" in body else None,
                usdt_inr=float(body["usdt_inr"]) if "usdt_inr" in body else None,
                alert_telegram=bool(body["alert_telegram"]) if "alert_telegram" in body else None,
                sizing_floor_pct=(float(body["sizing_floor_pct"])
                                  if "sizing_floor_pct" in body else None),
                sizing_ceiling_pct=(float(body["sizing_ceiling_pct"])
                                    if "sizing_ceiling_pct" in body else None),
            )
            # A config save must show up on /api/pipeline right away, not in
            # up to 4s — same discipline as /api/app-settings's own save.
            from scheduler import cache
            cache.invalidate("pipeline_db_read")
            return _json_response({"ok": True, "enabled": cfg.enabled, "starting_wallet": cfg.starting_wallet})
    except Exception as exc:
        return _json_response({"error": str(exc)}, status=400)


async def _api_strategy_config_get(runner, request: web.Request) -> web.Response:
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    async with AsyncSessionFactory() as session:
        cfg = await Repository(session).get_strategy_config()
        return _json_response({
            "crypto_min_confidence": cfg.crypto_min_confidence,
            "high_conviction_only": cfg.high_conviction_only,
            "crypto_volume_spike_enabled": cfg.crypto_volume_spike_enabled,
            "crypto_htf_filter_enabled": cfg.crypto_htf_filter_enabled,
            "binance_klines_enabled": cfg.binance_klines_enabled,
            "binance_oi_enabled": cfg.binance_oi_enabled,
            "orderflow_enabled": cfg.orderflow_enabled,
            "groq_signal_review_enabled": cfg.groq_signal_review_enabled,
            "groq_model": cfg.groq_model,
            "bank_size": cfg.bank_size,
            "min_confidence": cfg.min_confidence,
        })


async def _api_strategy_config_post(runner, request: web.Request) -> web.Response:
    is_admin = await _verify_admin_session(request)
    if not is_admin:
        return _json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            cfg = await repo.update_strategy_config(
                crypto_min_confidence=float(body["crypto_min_confidence"]) if "crypto_min_confidence" in body else None,
                high_conviction_only=bool(body["high_conviction_only"]) if "high_conviction_only" in body else None,
                crypto_volume_spike_enabled=bool(body["crypto_volume_spike_enabled"]) if "crypto_volume_spike_enabled" in body else None,
                crypto_htf_filter_enabled=bool(body["crypto_htf_filter_enabled"]) if "crypto_htf_filter_enabled" in body else None,
                binance_klines_enabled=bool(body["binance_klines_enabled"]) if "binance_klines_enabled" in body else None,
                binance_oi_enabled=bool(body["binance_oi_enabled"]) if "binance_oi_enabled" in body else None,
                orderflow_enabled=bool(body["orderflow_enabled"]) if "orderflow_enabled" in body else None,
                groq_signal_review_enabled=bool(body["groq_signal_review_enabled"]) if "groq_signal_review_enabled" in body else None,
                groq_model=str(body["groq_model"]) if "groq_model" in body else None,
                bank_size=float(body["bank_size"]) if "bank_size" in body else None,
                min_confidence=float(body["min_confidence"]) if "min_confidence" in body else None,
            )
            # The engine reads these from settings, not from this table: write
            # them through to the settings store so saving here takes effect.
            from config.overrides import LEGACY_STRATEGY
            from config.overrides import apply as _apply
            through = {k: body[k] for k in LEGACY_STRATEGY if k in body}
            if through:
                await repo.save_app_settings(through)
                _apply(_SETTINGS, through)
                if hasattr(runner, "on_settings_changed"):
                    runner.on_settings_changed(list(through))
            return _json_response({"ok": True, "groq_model": cfg.groq_model})
    except Exception as exc:
        return _json_response({"error": str(exc)}, status=400)


async def _api_collector_toggle(runner, request: web.Request) -> web.Response:
    """POST /api/settings/toggle  body: {"collector": "coindcx", "enabled": true}"""
    from scheduler.security import check_bearer_auth
    is_admin = await _verify_admin_session(request)
    if not is_admin and _SETTINGS.api_auth_token:
        denied = check_bearer_auth(request, _SETTINGS.api_auth_token)
        if denied is not None:
            return denied
    try:
        body = await request.json()
        collector = str(body.get("collector", ""))
        enabled = bool(body.get("enabled", True))
        if collector not in runner.collector_enabled:
            return web.Response(
                text=json.dumps({"error": f"unknown collector: {collector}"}),
                content_type="application/json", status=400,
            )
        # Saved, not just set in memory: the switches used to reset to their
        # defaults on every restart.
        key = {"coindcx": "coindcx_enabled", "coingecko": "coingecko_enabled",
               "binance_ws": "binance_ws_enabled",
               "twelvedata_ws": "twelvedata_enabled"}.get(collector)
        if key is None:
            runner.collector_enabled[collector] = enabled      # no stored setting
        else:
            from config.overrides import apply as _apply
            from storage.database import AsyncSessionFactory
            from storage.repository import Repository
            async with AsyncSessionFactory() as session:
                await Repository(session).save_app_settings({key: enabled})
            _apply(_SETTINGS, {key: enabled})
            if hasattr(runner, "on_settings_changed"):
                runner.on_settings_changed([key])
            else:
                runner.collector_enabled[collector] = enabled
        import structlog as _slog
        _slog.get_logger().info("collector_toggled", collector=collector, enabled=enabled)
        return web.Response(
            text=json.dumps({"collector": collector, "enabled": enabled, "ok": True}),
            content_type="application/json",
        )
    except Exception as exc:
        return web.Response(
            text=json.dumps({"error": str(exc)}),
            content_type="application/json", status=500,
        )


async def _api_collector_states(runner, request: web.Request) -> web.Response:
    """GET /api/settings — returns collector enabled/disabled states with quota info."""
    try:
        from config.settings import settings as _settings
        states = {}
        for name, enabled in runner.collector_enabled.items():
            states[name] = {"enabled": enabled}

        # Enrich with quota / key info — safely access with getattr
        if hasattr(runner, "coindcx"):
            states["coindcx"].update({
                "consecutive_failures": runner.coindcx._consecutive_failures,
                "matched_symbols": len(runner.coindcx.last_matched_symbols),
                "watchlist_size": len(await runner.crypto_store.get_symbols()),
            })
        if hasattr(runner, "coingecko"):
            states["coingecko"].update({
                "consecutive_failures": runner.coingecko._consecutive_failures,
                "watchlist_size": len(await runner.crypto_store.get_symbols()),
            })
        if hasattr(runner, "binance_ws"):
            states["binance_ws"].update({
                "connected": runner.binance_ws._running and runner.binance_ws._consecutive_failures == 0,
                "messages_received": runner.binance_ws._total_messages_received,
            })
        if hasattr(runner, "twelvedata_ws"):
            states["twelvedata_ws"].update({"key_set": bool(_settings.twelvedata_api_key)})

        return web.Response(text=json.dumps(states), content_type="application/json")
    except Exception as e:
        import traceback
        return web.Response(
            text=json.dumps({"error": str(e), "detail": traceback.format_exc()[-200:]}),
            content_type="application/json",
            status=500,
        )


_SETTINGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>System Settings — Signal Engine</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}
.topbar{background:#161b22;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:100}
.topbar a{color:#58a6ff;text-decoration:none;font-size:13.5px;padding:6px 12px;border-radius:6px;border:1px solid #30363d;transition:background .2s}
.topbar a:hover{background:#21262d}
.topbar h1{font-size:16px;font-weight:600;color:#e6edf3;margin-left:4px}
.btn-lock{margin-left:auto;background:#21262d;border:1px solid #30363d;color:#f85149;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;transition:all .2s;display:flex;align-items:center;gap:6px}
.btn-lock:hover{background:#3d0a0a;border-color:#f85149}
.container{max-width:820px;margin:32px auto;padding:0 16px;padding-bottom:60px}
h2{font-size:19px;font-weight:700;margin-bottom:6px;display:flex;align-items:center;gap:8px}
.subtitle{color:#8b949e;font-size:13px;margin-bottom:22px;line-height:1.5}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:22px 24px;margin-bottom:24px;transition:border-color .2s}
.card.active{border-color:#238636}
.card.paused{border-color:#f85149;opacity:.85}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.card-title{display:flex;align-items:center;gap:10px}
.card-name{font-size:15.5px;font-weight:600}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600}
.badge-green{background:#0d4429;color:#3fb950}
.badge-red{background:#3d0a0a;color:#f85149}
.badge-yellow{background:#3d2b00;color:#e3b341}
.card-meta{color:#8b949e;font-size:13px;line-height:1.6}
.meta-row{display:flex;justify-content:space-between;margin-top:6px}
.meta-label{color:#8b949e}
.meta-value{color:#e6edf3;font-weight:500}
.quota-bar{height:6px;background:#21262d;border-radius:3px;margin-top:10px;overflow:hidden}
.quota-fill{height:100%;border-radius:3px;transition:width .4s}
.quota-fill.safe{background:#238636}
.quota-fill.warn{background:#e3b341}
.quota-fill.danger{background:#f85149}
/* Form Controls */
.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px;margin-top:16px}
.form-group{display:flex;flex-direction:column;gap:6px}
.form-group label{font-size:11.5px;color:#8b949e;text-transform:uppercase;font-weight:700;letter-spacing:0.5px}
.form-group input,.form-group select{background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;padding:10px 12px;font-size:13.5px;outline:none;transition:border-color .2s}
.form-group input:focus,.form-group select:focus{border-color:#58a6ff}
.toggle-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:20px;padding-top:16px;border-top:1px solid #21262d}
.toggle-item{display:flex;align-items:center;justify-content:space-between;background:#0d1117;padding:12px 14px;border-radius:8px;border:1px solid #21262d}
.toggle-item span{font-size:13px;font-weight:500;color:#e6edf3}
/* Toggle Switch */
.toggle-wrap{display:flex;align-items:center;gap:8px}
.toggle-label{font-size:12px;color:#8b949e;min-width:36px;text-align:right}
.toggle{position:relative;width:44px;height:24px;cursor:pointer}
.toggle input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:#30363d;border-radius:24px;transition:.3s}
.slider:before{content:'';position:absolute;width:18px;height:18px;left:3px;bottom:3px;background:#e6edf3;border-radius:50%;transition:.3s}
input:checked+.slider{background:#238636}
input:checked+.slider:before{transform:translateX(20px)}
.save-btn{background:#238636;border:none;color:#fff;font-size:14px;font-weight:600;padding:11px 24px;border-radius:8px;cursor:pointer;margin-top:20px;width:100%;transition:background .2s}
.save-btn:hover{background:#2ea043}
.toast{position:fixed;bottom:24px;right:24px;background:#238636;color:#fff;padding:12px 20px;border-radius:8px;font-size:14px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:9999}
.toast.show{opacity:1}
.toast.err{background:#f85149}
.theme-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:32px}
.theme-sw{background:#161b22;border:1px solid #30363d;color:#c9d1d9;padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:6px;transition:all .2s}
.theme-sw:hover{border-color:#58a6ff;color:#fff}
.theme-sw.on{border-color:#58a6ff;background:#1f242c;color:#fff}
.theme-sw .sw{width:10px;height:10px;border-radius:50%;display:inline-block}
/* Lock Overlay */
.lock-overlay{position:fixed;inset:0;background:rgba(13,17,23,0.96);backdrop-filter:blur(10px);display:flex;align-items:center;justify-content:center;z-index:10000}
.lock-box{background:#161b22;border:1px solid #30363d;border-radius:16px;padding:36px 32px;max-width:380px;width:90%;text-align:center;box-shadow:0 20px 40px rgba(0,0,0,0.6)}
.lock-box input{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;padding:12px 14px;font-size:16px;margin-top:18px;text-align:center;letter-spacing:3px;outline:none}
.lock-box input:focus{border-color:#58a6ff}
.lock-err{color:#f85149;font-size:13px;margin-top:12px;display:none}
@media(max-width:640px){
  .form-grid{grid-template-columns:1fr !important}
  .toggle-grid{grid-template-columns:1fr !important}
  /* Keep the bottom clearance the desktop rule had; `padding:12px` on its
     own dropped it, so the save button sat against the edge of the screen. */
  .container{margin:14px auto;padding:0 12px 48px}

  /* The bar was one unwrapping flex row: two links, a title and the lock
     button held right by margin-left:auto. At 360px the button went off the
     side of the screen, which is a bad place for the only way to log out.
     Wrap, and let the title take its own line. */
  .topbar{flex-wrap:wrap;padding:10px 12px;gap:8px}
  .topbar h1{order:-1;flex:1 0 100%;margin-left:0;font-size:15px}
  .btn-lock{margin-left:auto}

  /* Safari zooms the whole page in when a focused field is under 16px, and
     nothing zooms it back — every tap on a number left the page enlarged and
     the user pinching. 16px is the threshold, not a preference. */
  .form-group input,.form-group select{font-size:16px;padding:11px 12px}

  /* Label and value collided when both were long, because space-between
     gives no minimum to either. Stack instead. */
  .meta-row{flex-direction:column;gap:1px;margin-top:9px}
  .meta-value{font-weight:600}

  /* A toggle row is a tap target; 12px of padding and a 44px switch is not
     enough of one at arm's length on a moving train. */
  .toggle-item{padding:14px}
  .theme-row{gap:7px}
  .theme-sw{padding:8px 12px}
  .toast{left:12px;right:12px;bottom:12px;text-align:center}
}
</style>
</head>
<body>

<div id="lock-screen" class="lock-overlay">
  <div class="lock-box">
    <div style="font-size:44px;margin-bottom:12px">🔒</div>
    <h2 style="justify-content:center;font-size:21px">Settings Locked</h2>
    <p style="color:#8b949e;font-size:13px;line-height:1.5;margin-top:6px">Enter your administrator password to unlock and manage configuration.</p>
    <input type="password" id="admin-pwd" placeholder="Enter password" autocomplete="off" onkeydown="if(event.key==='Enter')login()">
    <div id="login-err" class="lock-err"></div>
    <button class="save-btn" style="margin-top:18px" onclick="login()">Unlock Settings</button>
  </div>
</div>

<div class="topbar">
  <a href="/">← Dashboard</a>
  <a href="/data">📊 History</a>
  <h1>⚙️ System Settings</h1>
  <button class="btn-lock" onclick="logout()"><span>🔒</span> Lock & Logout</button>
</div>

<div class="container" id="settings-content" style="display:none">
  <h2>🎨 Site Theme</h2>
  <p class="subtitle">Applies instantly across all pages — saved in this browser.</p>
  <div class="theme-row">
    <button class="theme-sw" data-t="amber" onclick="setSiteTheme('amber')"><span class="sw" style="background:#f59e0b"></span>Amber Terminal</button>
    <button class="theme-sw" data-t="carbon" onclick="setSiteTheme('carbon')"><span class="sw" style="background:#a3e635"></span>Carbon Lime</button>
    <button class="theme-sw" data-t="crimson" onclick="setSiteTheme('crimson')"><span class="sw" style="background:#fb7185"></span>Crimson</button>
    <button class="theme-sw" data-t="navy" onclick="setSiteTheme('navy')"><span class="sw" style="background:#0ea5e9"></span>Deep Navy</button>
    <button class="theme-sw" data-t="light" onclick="setSiteTheme('light')"><span class="sw" style="background:#eef2f7"></span>Polar White</button>
    <button class="theme-sw" data-t="violet" onclick="setSiteTheme('violet')"><span class="sw" style="background:#6d28d9"></span>Violet Night</button>
    <button class="theme-sw" data-t="emerald" onclick="setSiteTheme('emerald')"><span class="sw" style="background:#15803d"></span>Emerald Court</button>
  </div>
  <script>setSiteTheme(localStorage.getItem('site_theme')||'amber');</script>

  <h2>📈 Paper Trading Configuration</h2>
  <p class="subtitle">Stored directly in database — updates apply live to simulator cycle without redeployment.</p>
  <div class="card">
    <div style="background: rgba(30, 41, 59, 0.7); border: 1px solid #334155; padding: 14px 18px; border-radius: 8px; margin-bottom: 18px; display: flex; align-items: center; justify-content: space-between;">
      <div>
        <div style="font-weight: 600; font-size: 15px; color: #f8fafc;">🚀 Paper Trading Simulator Master Switch</div>
        <div style="font-size: 12px; color: #94a3b8;">When active, signals matching conviction thresholds will open paper positions and simulate live trades.</div>
      </div>
      <label class="toggle"><input type="checkbox" id="p-enabled"><span class="slider"></span></label>
    </div>
    <div class="form-grid">
      <div class="form-group">
        <label>Starting Wallet (₹)</label>
        <input type="number" id="p-starting-wallet" step="100">
      </div>
      <div class="form-group">
        <label>Target Wallet (₹)</label>
        <input type="number" id="p-target-wallet" step="500">
      </div>
      <div class="form-group">
        <label>USDT / INR Rate (₹)</label>
        <input type="number" id="p-usdt-inr" step="0.1">
      </div>
      <div class="form-group">
        <label>Base Leverage (x)</label>
        <input type="number" id="p-leverage" step="1" min="1" max="100">
      </div>
      <div class="form-group">
        <label>Max Leverage (x)</label>
        <input type="number" id="p-max-leverage" step="1" min="1" max="100">
      </div>
      <div class="form-group">
        <label>Stop Loss (% of Margin)</label>
        <input type="number" id="p-stop-pct" step="0.01" min="0.01" max="1.0">
      </div>
      <div class="form-group">
        <label>Reward / Risk Ratio</label>
        <input type="number" id="p-reward-risk" step="0.1" min="0.5">
      </div>
      <div class="form-group">
        <label>Min Confidence (0.50 - 0.95)</label>
        <input type="number" id="p-min-confidence" step="0.01" min="0.50" max="0.99">
      </div>
      <div class="form-group">
        <label>Max Concurrent Trades</label>
        <input type="number" id="p-max-concurrent" step="1" min="1" max="20">
      </div>
      <div class="form-group">
        <label>Max Hold Duration (mins)</label>
        <input type="number" id="p-max-hold" step="10" min="10">
      </div>
    </div>

    <div class="toggle-grid">
      <div class="toggle-item">
        <span>Scaled Position Sizing (Tiering)</span>
        <label class="toggle"><input type="checkbox" id="p-scaled-sizing"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Trailing Stop Loss</span>
        <label class="toggle"><input type="checkbox" id="p-trailing"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Dynamic Conviction Leverage</span>
        <label class="toggle"><input type="checkbox" id="p-scaled-leverage"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Profit Ladder Ratchet</span>
        <label class="toggle"><input type="checkbox" id="p-ladder"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Tight Ladder Mode</span>
        <label class="toggle"><input type="checkbox" id="p-ladder-tight"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Telegram Fill & Exit Alerts</span>
        <label class="toggle"><input type="checkbox" id="p-telegram"><span class="slider"></span></label>
      </div>
    </div>
    <button class="save-btn" onclick="savePaperConfig()">💾 Save Paper Trading Configuration</button>
  </div>

  <h2>🤖 Strategy, AI Review & Market Data</h2>
  <p class="subtitle">Core engine conviction thresholds, Groq pre-signal review, and institutional orderflow feeds.</p>
  <div class="card">
    <div class="form-grid">
      <div class="form-group">
        <label>Groq AI Model Selection</label>
        <select id="s-groq-model" style="background:#0f172a; color:#f8fafc; border:1px solid #334155; padding:8px 10px; border-radius:6px; width:100%; font-size:13px;">
          <option value="qwen/qwen3.8-27b">Qwen 3.8 27B (Recommended - Fast & Analytical)</option>
          <option value="llama-3.3-70b-versatile">Llama 3.3 70B Versatile (Deep Reasoning)</option>
          <option value="llama-3.1-8b-instant">Llama 3.1 8B Instant (Ultra-fast)</option>
          <option value="mixtral-8x7b-32768">Mixtral 8x7B (High Context)</option>
          <option value="deepseek-r1-distill-llama-70b">DeepSeek R1 Distill Llama 70B (Math & Logic)</option>
        </select>
      </div>
      <div class="form-group">
        <label>Crypto Min Confidence (0.50 - 0.95)</label>
        <input type="number" id="s-min-confidence" step="0.01" min="0.50" max="0.99">
      </div>
      <div class="form-group">
        <label>Scalp Bank Size (₹)</label>
        <input type="number" id="s-bank-size" step="500">
      </div>
    </div>

    <div class="toggle-grid">
      <div class="toggle-item">
        <span>Groq Pre-Signal AI Review</span>
        <label class="toggle"><input type="checkbox" id="s-groq-review"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>High Conviction Mode (5x Edge)</span>
        <label class="toggle"><input type="checkbox" id="s-high-conviction"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Binance Futures Open Interest (OI)</span>
        <label class="toggle"><input type="checkbox" id="s-binance-oi"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Order Flow & CVD Absorption</span>
        <label class="toggle"><input type="checkbox" id="s-orderflow"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Binance 1m True OHLC Klines</span>
        <label class="toggle"><input type="checkbox" id="s-binance-klines"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>Volume Spike Confirmation</span>
        <label class="toggle"><input type="checkbox" id="s-volume-spike"><span class="slider"></span></label>
      </div>
      <div class="toggle-item">
        <span>HTF Trend Filter</span>
        <label class="toggle"><input type="checkbox" id="s-htf-filter"><span class="slider"></span></label>
      </div>
    </div>
    <button class="save-btn" onclick="saveStrategyConfig()">💾 Save Strategy & AI Configuration</button>
  </div>

  <h2>📡 Collector Data Sources</h2>
  <p class="subtitle">Toggle individual third-party feeds on/off to conserve external quota.</p>
  <div id="cards">Loading collectors...</div>
</div>

<div class="toast" id="toast"></div>

<script>
const SOURCES = [
  {id:'coindcx',name:'CoinDCX',icon:'🪙',desc:'Crypto price polling — preferred source, exact exchange prices',quota_total:null},
  {id:'coingecko',name:'CoinGecko',icon:'🦎',desc:'Crypto price polling — fallback for unlisted symbols',quota_total:null},
  {id:'binance_ws',name:'Binance WebSocket',icon:'🚫',desc:'Real-time crypto streaming (OFF by default)',quota_total:null},
  {id:'twelvedata_ws',name:'Twelve Data',icon:'🥇',desc:'Gold / Silver / Crude Oil live prices',quota_total:null},
];

let states = {};

async function apiFetch(url, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers);
  const token = sessionStorage.getItem('settings_token');
  if (token) {
    opts.headers['X-Settings-Token'] = token;
    opts.headers['Authorization'] = 'Bearer ' + token;
  }
  const res = await fetch(url, opts);
  if (res.status === 401) {
    sessionStorage.removeItem('settings_token');
    showLockScreen('Session expired or unauthorized. Please re-enter password.');
  }
  return res;
}

function showLockScreen(errMsg) {
  document.getElementById('lock-screen').style.display = 'flex';
  document.getElementById('settings-content').style.display = 'none';
  const errEl = document.getElementById('login-err');
  if (errMsg) {
    errEl.textContent = errMsg;
    errEl.style.display = 'block';
  } else {
    errEl.style.display = 'none';
  }
  const pwdInput = document.getElementById('admin-pwd');
  pwdInput.value = '';
  setTimeout(() => pwdInput.focus(), 100);
}

function unlockScreen() {
  document.getElementById('lock-screen').style.display = 'none';
  document.getElementById('settings-content').style.display = 'block';
  loadAll();
}

async function login() {
  const pwd = document.getElementById('admin-pwd').value.trim();
  const errEl = document.getElementById('login-err');
  if (!pwd) {
    errEl.textContent = 'Please enter password';
    errEl.style.display = 'block';
    return;
  }
  try {
    const res = await fetch('/api/settings/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: pwd}),
    });
    const data = await res.json();
    if (data.ok && data.token) {
      sessionStorage.setItem('settings_token', data.token);
      unlockScreen();
      showToast('Settings unlocked ✓');
    } else {
      errEl.textContent = data.error || 'Invalid password';
      errEl.style.display = 'block';
    }
  } catch(e) {
    errEl.textContent = 'Network connection failed';
    errEl.style.display = 'block';
  }
}

async function logout() {
  try {
    await apiFetch('/api/settings/auth/logout', {method: 'POST'});
  } catch(e){}
  sessionStorage.removeItem('settings_token');
  showLockScreen();
  showToast('Settings locked');
}

async function checkAuth() {
  const token = sessionStorage.getItem('settings_token');
  if (!token) {
    showLockScreen();
    return;
  }
  try {
    const res = await fetch('/api/settings/auth/status', {
      headers: {'X-Settings-Token': token}
    });
    const data = await res.json();
    if (data.authenticated) {
      unlockScreen();
    } else {
      sessionStorage.removeItem('settings_token');
      showLockScreen();
    }
  } catch(e) {
    showLockScreen();
  }
}

async function loadAll() {
  loadCollectors();
  loadPaperConfig();
  loadStrategyConfig();
}

async function loadCollectors() {
  try {
    const r = await fetch('/api/settings');
    states = await r.json();
    renderCollectors();
  } catch(e) {
    document.getElementById('cards').innerHTML = '<p style="color:#f85149">Failed to load collectors</p>';
  }
}

function renderCollectors() {
  const el = document.getElementById('cards');
  el.innerHTML = SOURCES.map(src => {
    const st = states[src.id] || {};
    const enabled = st.enabled !== false;
    const cardCls = enabled ? 'card active' : 'card paused';
    const badgeTxt = enabled ? 'ACTIVE' : 'PAUSED';
    const badgeCls = enabled ? 'badge badge-green' : 'badge badge-red';
    return `
    <div class="${cardCls}" id="card-${src.id}" style="margin-bottom:12px">
      <div class="card-header" style="margin-bottom:0">
        <div class="card-title">
          <span style="font-size:22px">${src.icon}</span>
          <div>
            <div class="card-name">${src.name} <span class="${badgeCls}">${badgeTxt}</span></div>
            <div style="font-size:12px;color:#8b949e;margin-top:2px">${src.desc}</div>
          </div>
        </div>
        <div class="toggle-wrap">
          <span class="toggle-label" id="lbl-${src.id}">${enabled ? 'ON' : 'OFF'}</span>
          <label class="toggle">
            <input type="checkbox" id="tog-${src.id}" ${enabled ? 'checked' : ''} onchange="toggleCollector('${src.id}')">
            <span class="slider"></span>
          </label>
        </div>
      </div>
    </div>`;
  }).join('');
}

async function toggleCollector(id) {
  const cb = document.getElementById('tog-' + id);
  const enabled = cb.checked;
  try {
    const r = await apiFetch('/api/settings/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({collector: id, enabled}),
    });
    const data = await r.json();
    if (data.ok) {
      states[id] = {...(states[id] || {}), enabled};
      renderCollectors();
      showToast(enabled ? id + ' enabled ✓' : id + ' paused', !enabled);
    } else {
      showToast('Error: ' + (data.error || 'unknown'), true);
      cb.checked = !enabled;
    }
  } catch(e) {
    showToast('Network error', true);
    cb.checked = !enabled;
  }
}

async function loadPaperConfig() {
  try {
    const r = await fetch('/api/paper/config');
    const c = await r.json();
    document.getElementById('p-enabled').checked = c.enabled !== false;
    document.getElementById('p-starting-wallet').value = c.starting_wallet;
    document.getElementById('p-target-wallet').value = c.target_wallet;
    document.getElementById('p-usdt-inr').value = c.usdt_inr;
    document.getElementById('p-leverage').value = c.leverage;
    document.getElementById('p-max-leverage').value = c.max_leverage;
    document.getElementById('p-stop-pct').value = c.stop_pct_of_margin;
    document.getElementById('p-reward-risk').value = c.reward_risk;
    document.getElementById('p-min-confidence').value = c.min_confidence;
    document.getElementById('p-max-concurrent').value = c.max_concurrent;
    document.getElementById('p-max-hold').value = c.max_hold_minutes;
    document.getElementById('p-scaled-sizing').checked = c.scaled_sizing;
    document.getElementById('p-trailing').checked = c.trailing_enabled;
    document.getElementById('p-scaled-leverage').checked = c.scaled_leverage;
    document.getElementById('p-ladder').checked = c.ladder_enabled;
    document.getElementById('p-ladder-tight').checked = c.ladder_tight;
    document.getElementById('p-telegram').checked = c.alert_telegram;
  } catch(e) {
    console.error('Failed to load paper config', e);
  }
}

async function savePaperConfig() {
  const payload = {
    enabled: document.getElementById('p-enabled').checked,
    starting_wallet: parseFloat(document.getElementById('p-starting-wallet').value),
    target_wallet: parseFloat(document.getElementById('p-target-wallet').value),
    usdt_inr: parseFloat(document.getElementById('p-usdt-inr').value),
    leverage: parseFloat(document.getElementById('p-leverage').value),
    max_leverage: parseFloat(document.getElementById('p-max-leverage').value),
    stop_pct_of_margin: parseFloat(document.getElementById('p-stop-pct').value),
    reward_risk: parseFloat(document.getElementById('p-reward-risk').value),
    min_confidence: parseFloat(document.getElementById('p-min-confidence').value),
    max_concurrent: parseInt(document.getElementById('p-max-concurrent').value),
    max_hold_minutes: parseInt(document.getElementById('p-max-hold').value),
    scaled_sizing: document.getElementById('p-scaled-sizing').checked,
    trailing_enabled: document.getElementById('p-trailing').checked,
    scaled_leverage: document.getElementById('p-scaled-leverage').checked,
    ladder_enabled: document.getElementById('p-ladder').checked,
    ladder_tight: document.getElementById('p-ladder-tight').checked,
    alert_telegram: document.getElementById('p-telegram').checked,
  };
  try {
    const res = await apiFetch('/api/paper/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await res.json();
    if (d.ok) showToast('Paper Trading configuration updated ✓');
    else showToast('Save failed: ' + (d.error || 'unknown'), true);
  } catch(e) {
    showToast('Network error saving config', true);
  }
}

async function loadStrategyConfig() {
  try {
    const r = await fetch('/api/strategy/config');
    const c = await r.json();
    document.getElementById('s-groq-model').value = c.groq_model || 'qwen/qwen3.8-27b';
    document.getElementById('s-min-confidence').value = c.crypto_min_confidence;
    document.getElementById('s-bank-size').value = c.bank_size;
    document.getElementById('s-groq-review').checked = c.groq_signal_review_enabled;
    document.getElementById('s-high-conviction').checked = c.high_conviction_only;
    document.getElementById('s-binance-oi').checked = c.binance_oi_enabled;
    document.getElementById('s-orderflow').checked = c.orderflow_enabled;
    document.getElementById('s-binance-klines').checked = c.binance_klines_enabled;
    document.getElementById('s-volume-spike').checked = c.crypto_volume_spike_enabled;
    document.getElementById('s-htf-filter').checked = c.crypto_htf_filter_enabled;
  } catch(e) {
    console.error('Failed to load strategy config', e);
  }
}

async function saveStrategyConfig() {
  const payload = {
    groq_model: document.getElementById('s-groq-model').value.trim(),
    crypto_min_confidence: parseFloat(document.getElementById('s-min-confidence').value),
    bank_size: parseFloat(document.getElementById('s-bank-size').value),
    groq_signal_review_enabled: document.getElementById('s-groq-review').checked,
    high_conviction_only: document.getElementById('s-high-conviction').checked,
    binance_oi_enabled: document.getElementById('s-binance-oi').checked,
    orderflow_enabled: document.getElementById('s-orderflow').checked,
    binance_klines_enabled: document.getElementById('s-binance-klines').checked,
    crypto_volume_spike_enabled: document.getElementById('s-volume-spike').checked,
    crypto_htf_filter_enabled: document.getElementById('s-htf-filter').checked,
  };
  try {
    const res = await apiFetch('/api/strategy/config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await res.json();
    if (d.ok) showToast('Strategy & AI configuration updated ✓');
    else showToast('Save failed: ' + (d.error || 'unknown'), true);
  } catch(e) {
    showToast('Network error saving strategy config', true);
  }
}

function showToast(msg, isErr=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (isErr ? ' err' : '');
  setTimeout(() => t.className = 'toast', 2800);
}

window.addEventListener('DOMContentLoaded', () => {
  checkAuth();
  loadSimulatorSettings();
});
</script>
</body>
</html>"""


# ── Site-wide theme system ────────────────────────────────────────────────────
# Injected into every page <head>. Theme stored in localStorage ('site_theme'),
# applied as html[data-theme=...]; 'navy' (default) = no attribute, no overrides.
async def _audit_page(request: web.Request) -> web.Response:
    return web.Response(text=_AUDIT_HTML, content_type="text/html")


# The signal post-mortem. Its own route rather than another sidebar tab: this
# is a workbench, it wants the full width for a wide table, and it should be
# reloadable without disturbing whatever the main dashboard was showing.
_AUDIT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Audit — what worked, what did not</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg);color:var(--text);min-height:100vh;padding-bottom:60px;font-size:13px}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:12px 18px;
  display:flex;align-items:center;gap:12px;flex-wrap:wrap;position:sticky;top:0;z-index:20}
header h1{font-size:16px;font-weight:700;color:var(--text-strong);display:flex;align-items:center;gap:8px}
header .sub{font-size:12px;color:var(--muted)}
a.nav-btn{background:var(--acc-t);border:1px solid var(--acc-t2);color:var(--accent);
  font-size:12px;font-weight:600;padding:5px 12px;border-radius:8px;text-decoration:none;white-space:nowrap}
a.nav-btn:hover{background:var(--acc-t2)}
.spacer{margin-left:auto}
main{padding:16px 18px;max-width:1500px;margin:0 auto}
h2{font-size:14px;font-weight:700;color:var(--text-strong);margin:26px 0 10px;
  display:flex;align-items:baseline;gap:9px}
h2 .hint{font-size:11.5px;font-weight:400;color:var(--muted)}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
.card .t{font-size:10.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted2)}
.card .v{font-size:21px;font-weight:700;color:var(--text-strong);margin-top:3px;line-height:1.15}
.card .s{font-size:11px;color:var(--muted);margin-top:2px}
.pos{color:var(--pos)}.neg{color:var(--neg)}.acc{color:var(--accent)}

.filters{background:var(--panel2);border:1px solid var(--line2);border-radius:10px;
  padding:11px 13px;display:flex;flex-wrap:wrap;gap:9px;align-items:flex-end;margin-top:14px}
.f{display:flex;flex-direction:column;gap:3px}
.f label{font-size:10.5px;text-transform:uppercase;letter-spacing:.4px;color:var(--muted2)}
.f select,.f input{background:var(--sunk);border:1px solid var(--line);color:var(--text);
  border-radius:7px;padding:5px 8px;font-size:12.5px;font-family:inherit;min-width:112px}
.f input[type=search]{min-width:170px}
button.btn{background:var(--acc-t);border:1px solid var(--acc-t2);color:var(--accent);
  border-radius:7px;padding:6px 13px;font-weight:600;font-size:12.5px;cursor:pointer;font-family:inherit}
button.btn:hover{background:var(--acc-t2)}
button.btn.ghost{background:transparent;border-color:var(--line);color:var(--muted)}

.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}
th,td{padding:6px 10px;text-align:left;border-bottom:1px solid var(--line2)}
th{background:var(--panel2);color:var(--muted);font-weight:600;position:sticky;top:0;
  font-size:10.5px;text-transform:uppercase;letter-spacing:.4px;cursor:pointer;user-select:none}
th:hover{color:var(--text-strong)}
th.sorted::after{content:'';margin-left:5px;color:var(--accent)}
th.asc::after{content:'\\2191';margin-left:5px;color:var(--accent)}
th.desc::after{content:'\\2193';margin-left:5px;color:var(--accent)}
tbody tr:hover td{background:var(--panel2)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
th.num{text-align:right}
.empty{color:var(--muted2);padding:16px;font-size:12.5px}

.pill{display:inline-block;padding:1px 8px;border-radius:9999px;font-size:10.5px;font-weight:700;
  letter-spacing:.3px}
.pill.won{background:var(--pos-t);color:var(--pos);border:1px solid var(--pos-t2)}
.pill.lost{background:var(--neg-t);color:var(--neg);border:1px solid var(--neg-t2)}
.pill.expired{background:var(--mut-t);color:var(--muted);border:1px solid var(--line)}
.pill.pending{background:var(--acc-t);color:var(--accent);border:1px solid var(--acc-t2)}
.pill.long{background:var(--pos-t);color:var(--pos)}
.pill.short{background:var(--neg-t);color:var(--neg)}

.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
.tab{background:var(--panel2);border:1px solid var(--line2);color:var(--muted);
  padding:5px 12px;border-radius:9999px;font-size:12px;cursor:pointer;font-weight:600;font-family:inherit}
.tab.on{background:var(--acc-t);border-color:var(--acc-t2);color:var(--accent)}

.bar{position:relative;background:var(--sunk);border-radius:4px;height:16px;min-width:90px;overflow:hidden}
.bar i{position:absolute;left:0;top:0;bottom:0;background:var(--pos-t2);display:block}
.bar span{position:relative;font-size:10.5px;line-height:16px;padding-left:5px;color:var(--text)}

.stage{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-bottom:9px;overflow:hidden}
.stage-hd{padding:10px 13px;display:flex;align-items:center;gap:10px;cursor:pointer;background:var(--panel2)}
.stage-hd b{font-size:13px;color:var(--text-strong)}
.stage-hd .d{font-size:11.5px;color:var(--muted);flex:1;min-width:0}
.stage-hd .n{font-size:11px;color:var(--accent);font-weight:700}
.stage-body{display:none;padding:4px 0}
.stage.open .stage-body{display:block}
.m{padding:8px 13px;border-top:1px solid var(--line2)}
.m code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
  color:var(--accent-soft);word-break:break-word}
.m .kind{font-size:9.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted2);
  border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-right:6px}
.m .sum{font-size:12px;color:var(--muted);margin-top:3px;white-space:normal}
.m .src{font-size:10.5px;color:var(--muted2);margin-top:3px;font-family:ui-monospace,monospace}
.m .mem{font-size:10.5px;color:var(--muted2);margin-top:3px}
.m pre{display:none;white-space:pre-wrap;font-size:11px;color:var(--muted);
  background:var(--sunk);border:1px solid var(--line2);border-radius:7px;padding:9px;margin-top:6px}
.m.open pre{display:block}
.note{font-size:11.5px;color:var(--muted2);margin-top:8px;white-space:normal;line-height:1.55}

@media(max-width:720px){
  html{-webkit-text-size-adjust:100%}
  main{padding:12px}
  header{padding:10px 12px}
  .f select,.f input{min-width:0;width:100%;font-size:16px;min-height:38px}
  .f{flex:1 1 130px}
  .f input[type=search]{min-width:0}
  .cards{grid-template-columns:repeat(auto-fit,minmax(128px,1fr))}
  .card .v{font-size:18px}
  table{font-size:11.5px}
  th,td{padding:6px 7px}
}
</style>
</head>
<body>
<header>
  <h1>&#129514; Signal Audit</h1>
  <span class="sub" id="hdr-sub">loading&hellip;</span>
  <span class="spacer"></span>
  <a class="nav-btn" href="/">&larr; Dashboard</a>
  <a class="nav-btn" href="/data">Database</a>
  <a class="nav-btn" href="/settings">Settings</a>
</header>
<main>

  <div class="cards" id="cards"></div>

  <div class="filters">
    <div class="f"><label>Window</label><select id="f-days" onchange="reload()">
      <option value="1">Last 24h</option><option value="7">Last 7 days</option>
      <option value="30" selected>Last 30 days</option><option value="90">Last 90 days</option>
      <option value="365">Everything</option></select></div>
    <div class="f"><label>Symbol</label><select id="f-symbol" onchange="render()"></select></div>
    <div class="f"><label>Setup</label><select id="f-setup" onchange="render()"></select></div>
    <div class="f"><label>Horizon</label><select id="f-horizon" onchange="render()"></select></div>
    <div class="f"><label>Direction</label><select id="f-direction" onchange="render()"></select></div>
    <div class="f"><label>Result</label><select id="f-outcome" onchange="render()">
      <option value="">All</option><option value="won">Won</option><option value="lost">Lost</option>
      <option value="expired">Expired</option><option value="pending">Pending</option>
      <option value="decided">Decided only</option>
      <option value="netwin">Profitable after costs</option>
      <option value="netloss">Lost money after costs</option></select></div>
    <div class="f"><label>Min &times; cost</label><select id="f-xcost" onchange="render()">
      <option value="0">Any</option><option value="1">1&times;+</option><option value="2">2&times;+</option>
      <option value="3">3&times;+</option><option value="5">5&times;+</option></select></div>
    <div class="f"><label>Min confidence</label><select id="f-conf" onchange="render()">
      <option value="0">Any</option><option value="60">60%+</option><option value="70">70%+</option>
      <option value="80">80%+</option><option value="90">90%+</option></select></div>
    <div class="f"><label>Search</label><input type="search" id="f-q" placeholder="symbol or setup"
      oninput="render()"></div>
    <button class="btn ghost" onclick="clearFilters()">Reset</button>
    <button class="btn" onclick="reload()">Refresh</button>
  </div>

  <h2>Predictions <span class="hint" id="tbl-count"></span></h2>
  <div class="scroll"><table id="tbl" class="tbl">
    <thead><tr>
      <th data-k="timestamp">Fired</th>
      <th data-k="symbol">Symbol</th>
      <th data-k="signal_type">Setup</th>
      <th data-k="direction">Dir</th>
      <th data-k="horizon">Window</th>
      <th class="num" data-k="confidence_pct">Conf</th>
      <th class="num" data-k="entry">Entry</th>
      <th class="num" data-k="target">Target</th>
      <th class="num" data-k="stop">Stop</th>
      <th class="num" data-k="move_pct">Move</th>
      <th class="num" data-k="reward_risk">R:R</th>
      <th class="num" data-k="x_cost">&times; cost</th>
      <th data-k="outcome">Result</th>
      <th class="num" data-k="pnl_pct">Gross</th>
      <th class="num" data-k="net_pnl_pct">Net of fees</th>
    </tr></thead><tbody id="tbody"></tbody>
  </table></div>
  <div class="note">Gross is the price move the signal captured. Net subtracts the round trip
    for that market &mdash; fees, spread and slippage &mdash; which is the number that reaches the
    wallet. A row that is green on gross and red on net is a win that cost money.</div>

  <h2>Where it works and where it does not
    <span class="hint">same signals, sliced by the thing you suspect</span></h2>
  <div class="tabs" id="slice-tabs"></div>
  <div class="scroll"><table class="tbl">
    <thead><tr>
      <th id="slice-hd">Group</th>
      <th class="num">Fired</th><th class="num">Won</th><th class="num">Lost</th>
      <th class="num">Expired</th><th class="num">Pending</th>
      <th>Hit rate (gross)</th>
      <th class="num">Hit rate net</th>
      <th class="num">Expectancy</th>
      <th class="num">Avg move</th><th class="num">Avg &times; cost</th><th class="num">Avg conf</th>
    </tr></thead><tbody id="slice-body"></tbody>
  </table></div>
  <div class="note">Expectancy is the average net move per signal fired. Positive means the slice
    pays for its own costs; negative means every extra signal there is a slow leak, and no hit
    rate rescues it. Slices with nothing resolved show &mdash; rather than 0% &mdash; an untested
    slice is not a losing one.</div>

  <h2>How a signal is made <span class="hint">every method on the path, read from the code</span></h2>
  <div class="filters" style="margin-top:0">
    <div class="f"><label>Find a method</label>
      <input type="search" id="m-q" placeholder="rsi, atr, vote, tick&hellip;" oninput="renderMethods()"></div>
    <button class="btn ghost" onclick="toggleAllStages(true)">Expand all</button>
    <button class="btn ghost" onclick="toggleAllStages(false)">Collapse all</button>
  </div>
  <div id="methods"></div>
  <div class="note">Signatures, descriptions and line numbers are read out of the modules at
    request time, so this list cannot drift from the code that actually ran.</div>

</main>
<script>
const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const _IST = {timeZone:'Asia/Kolkata'};
let DATA = null, METHODS = null, SLICE = 'by_symbol', SORT = {k:'timestamp', dir:-1};

const SETUP_NAME = {
  rsi_divergence:'Momentum Reversal', volume_spike:'Volume Surge',
  bollinger_squeeze:'Breakout Setup', sentiment_shift:'News Catalyst',
  confluence:'Confluence',
};
const SLICES = [
  ['by_symbol','Symbol'], ['by_setup','Setup'], ['by_horizon','Window'],
  ['by_direction','Direction'], ['by_confidence','Confidence band'],
  ['by_edge','Target vs cost'],
];

function fmtTime(iso){ return iso ? esc(window.fmtStamp(iso)) : ''; }
function px(v){
  if(v==null) return '&mdash;';
  const a = Math.abs(v);
  return a>=1000 ? v.toLocaleString('en-IN',{maximumFractionDigits:1})
       : a>=1 ? v.toFixed(2) : v.toFixed(4);
}
function pct(v,d){ return v==null ? '&mdash;' : v.toFixed(d==null?2:d)+'%'; }
function signed(v,d){
  if(v==null) return '<span class="sub">&mdash;</span>';
  const cls = v>0?'pos':(v<0?'neg':'');
  return `<span class="${cls}">${v>0?'+':''}${v.toFixed(d==null?3:d)}%</span>`;
}

async function reload(){
  const days = document.getElementById('f-days').value;
  document.getElementById('hdr-sub').textContent = 'loading…';
  try{
    const [a,m] = await Promise.all([
      fetch('/api/audit?days='+days).then(r=>{
        if(!r.ok) throw new Error('audit returned ' + r.status); return r.json(); }),
      METHODS ? Promise.resolve({stages:METHODS})
              : fetch('/api/audit/methods').then(r=>{
                  if(!r.ok) throw new Error('methods returned ' + r.status);
                  return r.json(); }),
    ]);
    DATA = a; METHODS = m.stages;
    buildFilterOptions();
    renderCards(); render(); renderMethods();
    document.getElementById('hdr-sub').textContent =
      `${a.records.length} signals · last ${a.days}d · round trip ${a.cost_model.round_trip_pct}%`;
  }catch(e){
    // The subtitle alone left an empty page, which reads as "no signals yet"
    // rather than "the request died".
    document.getElementById('hdr-sub').textContent = 'failed to load';
    Panel.error(document.getElementById('slice-body'), 'the audit', e, reload);
    const tb = document.getElementById('tbody');
    if(tb) tb.innerHTML =
      '<tr><td colspan="99" style="padding:0">'
      + '<div class="pnl pnl-err"><div class="pnl-t">Could not load the audit</div>'
      + '<div class="pnl-d">' + (e && e.message ? e.message : e) + '</div></div></td></tr>';
  }
}

// Options come from the data, not a hardcoded list, so a coin added to the
// watchlist shows up here without a code change.
function fillSelect(id, values, allLabel, pretty){
  const el = document.getElementById(id), keep = el.value;
  el.innerHTML = `<option value="">${allLabel}</option>` +
    values.map(v=>`<option value="${esc(v)}">${esc(pretty?pretty(v):v)}</option>`).join('');
  if(values.includes(keep)) el.value = keep;
}
function buildFilterOptions(){
  const r = DATA.records, uniq = k => [...new Set(r.map(x=>x[k]).filter(Boolean))].sort();
  fillSelect('f-symbol', uniq('symbol'), 'All coins');
  fillSelect('f-setup', uniq('signal_type'), 'All setups', v=>SETUP_NAME[v]||v);
  fillSelect('f-horizon', uniq('horizon'), 'All windows');
  fillSelect('f-direction', uniq('direction'), 'Both ways', v=>v.toUpperCase());
}
function clearFilters(){
  ['f-symbol','f-setup','f-horizon','f-direction','f-outcome'].forEach(i=>document.getElementById(i).value='');
  ['f-xcost','f-conf'].forEach(i=>document.getElementById(i).value='0');
  document.getElementById('f-q').value='';
  render();
}

function filtered(){
  const g = id => document.getElementById(id).value;
  const sym=g('f-symbol'), setup=g('f-setup'), hz=g('f-horizon'), dir=g('f-direction'),
        out=g('f-outcome'), xc=parseFloat(g('f-xcost'))||0, cf=parseFloat(g('f-conf'))||0,
        q=g('f-q').trim().toLowerCase();
  return DATA.records.filter(r=>{
    if(sym && r.symbol!==sym) return false;
    if(setup && r.signal_type!==setup) return false;
    if(hz && r.horizon!==hz) return false;
    if(dir && r.direction!==dir) return false;
    if(out==='decided'){ if(r.outcome!=='won'&&r.outcome!=='lost') return false; }
    else if(out==='netwin'){ if(!r.net_won) return false; }
    else if(out==='netloss'){ if(r.outcome==='pending'||r.net_won) return false; }
    else if(out && r.outcome!==out) return false;
    if(r.x_cost < xc) return false;
    if(r.confidence_pct < cf) return false;
    if(q && !(r.symbol.toLowerCase().includes(q) ||
              (SETUP_NAME[r.signal_type]||r.signal_type).toLowerCase().includes(q))) return false;
    return true;
  });
}

function renderCards(){
  const t = DATA.totals, c = DATA.cost_model;
  document.getElementById('cards').innerHTML = [
    ['Signals fired', t.n, `${t.decided} decided · ${t.pending} still open`,''],
    ['Hit rate, gross', t.hit_rate_pct==null?'&mdash;':t.hit_rate_pct+'%',
      t.decided?`${t.won} won / ${t.lost} lost`:'nothing resolved yet','acc'],
    ['Hit rate, net of fees', t.net_hit_rate_pct==null?'&mdash;':t.net_hit_rate_pct+'%',
      'finished ahead after the round trip',
      t.net_hit_rate_pct!=null && t.net_hit_rate_pct>=50?'pos':'neg'],
    ['Expectancy', t.expectancy_pct==null?'&mdash;':(t.expectancy_pct>0?'+':'')+t.expectancy_pct+'%',
      'avg net move per signal',
      t.expectancy_pct==null?'':(t.expectancy_pct>0?'pos':'neg')],
    ['Avg target', t.avg_move_pct==null?'&mdash;':t.avg_move_pct.toFixed(3)+'%',
      t.avg_x_cost==null?'':t.avg_x_cost.toFixed(1)+'× the round trip',''],
    ['Round trip', c.round_trip_pct+'%',
      `floor ${c.min_target_pct}% at ${c.min_edge_multiple}× cost`,'neg'],
  ].map(([t_,v,s,cls])=>
    `<div class="card"><div class="t">${t_}</div><div class="v ${cls||''}">${v}</div>
     <div class="s">${s||''}</div></div>`).join('');
}

function render(){
  if(!DATA) return;
  const rows = filtered();
  const k = SORT.k;
  rows.sort((a,b)=>{
    const x=a[k], y=b[k];
    if(x===y) return 0;
    if(x==null) return 1;
    if(y==null) return -1;
    return (x>y?1:-1) * SORT.dir;
  });
  document.getElementById('tbl-count').textContent =
    `${rows.length} of ${DATA.records.length} shown`;

  document.getElementById('tbody').innerHTML = rows.length ? rows.map(r=>`<tr>
    <td>${fmtTime(r.timestamp)}</td>
    <td><b>${esc(r.symbol)}</b></td>
    <td>${esc(SETUP_NAME[r.signal_type]||r.signal_type)}</td>
    <td><span class="pill ${esc(r.direction)}">${esc(r.direction.toUpperCase())}</span></td>
    <td>${esc(r.horizon||'—')}</td>
    <td class="num">${r.confidence_pct}%</td>
    <td class="num">${px(r.entry)}</td>
    <td class="num">${px(r.target)}</td>
    <td class="num">${px(r.stop)}</td>
    <td class="num">${pct(r.move_pct,3)}</td>
    <td class="num">${r.reward_risk?r.reward_risk.toFixed(2):'—'}</td>
    <td class="num ${r.x_cost>=3?'pos':(r.x_cost<1?'neg':'')}">${r.x_cost.toFixed(1)}×</td>
    <td><span class="pill ${esc(r.outcome)}">${esc(r.outcome.toUpperCase())}</span></td>
    <td class="num">${r.outcome==='pending'?'<span>—</span>':signed(r.pnl_pct)}</td>
    <td class="num">${r.outcome==='pending'?'<span>—</span>':signed(r.net_pnl_pct)}</td>
  </tr>`).join('') : '<tr><td colspan="15" class="empty">No signals match these filters</td></tr>';

  document.querySelectorAll('#tbl th').forEach(th=>{
    th.classList.toggle('asc', th.dataset.k===k && SORT.dir===1);
    th.classList.toggle('desc', th.dataset.k===k && SORT.dir===-1);
  });
  renderSlices();
}

document.addEventListener('click', e=>{
  const th = e.target.closest('#tbl th');
  if(!th || !th.dataset.k) return;
  if(SORT.k===th.dataset.k) SORT.dir = -SORT.dir; else SORT = {k:th.dataset.k, dir:-1};
  render();
});

function renderSlices(){
  document.getElementById('slice-tabs').innerHTML = SLICES.map(([k,label])=>
    `<button class="tab ${k===SLICE?'on':''}" onclick="SLICE='${k}';renderSlices()">${label}</button>`).join('');
  const label = (SLICES.find(s=>s[0]===SLICE)||[])[1] || 'Group';
  document.getElementById('slice-hd').textContent = label;
  const rows = DATA[SLICE] || [];
  const max = Math.max(1, ...rows.map(r=>r.n));
  document.getElementById('slice-body').innerHTML = rows.length ? rows.map(r=>`<tr>
    <td><b>${esc(SETUP_NAME[r.label]||r.label)}</b></td>
    <td class="num">
      <div class="bar"><i style="width:${r.n/max*100}%"></i><span>${r.n}</span></div></td>
    <td class="num pos">${r.won}</td>
    <td class="num neg">${r.lost}</td>
    <td class="num">${r.expired}</td>
    <td class="num">${r.pending}</td>
    <td>${r.hit_rate_pct==null?'<span class="empty" style="padding:0">not resolved yet</span>'
      :`<div class="bar"><i style="width:${r.hit_rate_pct}%"></i><span>${r.hit_rate_pct}% of ${r.decided}</span></div>`}</td>
    <td class="num">${r.net_hit_rate_pct==null?'&mdash;':r.net_hit_rate_pct+'%'}</td>
    <td class="num">${r.expectancy_pct==null?'&mdash;':signed(r.expectancy_pct)}</td>
    <td class="num">${r.avg_move_pct==null?'&mdash;':r.avg_move_pct.toFixed(3)+'%'}</td>
    <td class="num ${r.avg_x_cost>=3?'pos':(r.avg_x_cost<1?'neg':'')}">${r.avg_x_cost==null?'&mdash;':r.avg_x_cost.toFixed(1)+'×'}</td>
    <td class="num">${r.avg_confidence_pct==null?'&mdash;':r.avg_confidence_pct+'%'}</td>
  </tr>`).join('') : '<tr><td colspan="12" style="padding:0">'
      + '<div class="pnl pnl-empty"><div class="pnl-t">Nothing in this window</div>'
      + '<div class="pnl-d">No signals fired over this period, or none match the '
      + 'filters above. Widen the day range to look further back.</div></div></td></tr>';
}

function renderMethods(){
  if(!METHODS) return;
  const q = (document.getElementById('m-q').value||'').trim().toLowerCase();
  const host = document.getElementById('methods');
  host.innerHTML = METHODS.map((st,i)=>{
    const ms = st.methods.filter(m=> !q ||
      m.name.toLowerCase().includes(q) || m.module.toLowerCase().includes(q) ||
      m.summary.toLowerCase().includes(q));
    if(q && !ms.length) return '';
    return `<div class="stage ${q||i<1?'open':''}">
      <div class="stage-hd" onclick="this.parentNode.classList.toggle('open')">
        <b>${esc(st.stage)}</b>
        <span class="d">${esc(st.description)}</span>
        <span class="n">${ms.length}${ms.length!==st.count?' / '+st.count:''}</span>
      </div>
      <div class="stage-body">${ms.map(m=>`
        <div class="m" onclick="this.classList.toggle('open')">
          <div><span class="kind">${esc(m.kind)}</span><code>${esc(m.signature)}</code></div>
          ${m.summary?`<div class="sum">${esc(m.summary)}</div>`:''}
          ${m.members.length?`<div class="mem">members: ${m.members.map(esc).join(', ')}</div>`:''}
          <div class="src">${esc(m.source)}</div>
          ${m.doc && m.doc!==m.summary?`<pre>${esc(m.doc)}</pre>`:''}
        </div>`).join('')}</div>
    </div>`;
  }).join('') || '<div class="empty">No method matches that search</div>';
}
function toggleAllStages(open){
  document.querySelectorAll('.stage').forEach(s=>s.classList.toggle('open', open));
}

reload();
</script>
</body>
</html>
"""


async def _predict_page(request: web.Request) -> web.Response:
    return web.Response(text=_PREDICT_HTML, content_type="text/html")


# The board. Its own route because it answers a different question from the
# signal feed: signals say "is there a trade worth its costs right now", and
# the honest answer is usually no. This says something about every market all
# the time, and is explicit about how much of it is knowable.
_PREDICT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Price Outlook</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg);color:var(--text);min-height:100vh;padding-bottom:60px;font-size:13px}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:11px 18px;
  display:flex;align-items:center;gap:13px;flex-wrap:wrap;position:sticky;top:0;z-index:30}
header h1{font-size:16px;font-weight:700;color:var(--text-strong);display:flex;gap:8px;align-items:center}
a.nav-btn{background:var(--acc-t);border:1px solid var(--acc-t2);color:var(--accent);font-size:12px;
  font-weight:600;padding:5px 12px;border-radius:8px;text-decoration:none;white-space:nowrap}
.spacer{margin-left:auto}
.tick{font-size:11.5px;color:var(--muted)}
main{padding:16px 18px;max-width:1400px;margin:0 auto}

.surges{display:flex;flex-direction:column;gap:8px;margin-bottom:16px}
.surge{display:flex;align-items:center;gap:11px;background:var(--acc-t);
  border:1px solid var(--acc-t2);border-radius:10px;padding:10px 14px;font-size:13px}
.surge.up{background:var(--pos-t);border-color:var(--pos-t2)}
.surge.down{background:var(--neg-t);border-color:var(--neg-t2)}
.surge b{color:var(--text-strong)}
.surge .tag{font-size:9.5px;letter-spacing:.09em;text-transform:uppercase;font-weight:700;
  padding:3px 8px;border-radius:4px;background:var(--panel);color:var(--accent);white-space:nowrap}
.surge.up .tag{color:var(--pos)} .surge.down .tag{color:var(--neg)}

.note{font-size:12px;color:var(--muted2);line-height:1.6;margin-bottom:14px;max-width:88ch}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}
th,td{padding:11px 14px;text-align:left;border-bottom:1px solid var(--line2)}
th{background:var(--panel2);color:var(--muted2);font-size:10px;letter-spacing:.09em;
  text-transform:uppercase;font-weight:700;position:sticky;top:0}
tbody tr:last-child td{border-bottom:none}
tbody tr:hover td{background:var(--panel2)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.sym{font-weight:700;color:var(--text-strong);font-size:14px}
.sub{font-size:11px;color:var(--muted2)}
.pos{color:var(--pos)}.neg{color:var(--neg)}.muted{color:var(--muted)}

/* the forecast cell: a band with the centre marked */
.fc{min-width:160px}
.fc .mid{font-variant-numeric:tabular-nums;font-weight:600;color:var(--text-strong)}
.fc .rng{font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.bar{position:relative;height:6px;border-radius:3px;background:var(--sunk);margin-top:5px}
.bar i{position:absolute;top:0;bottom:0;border-radius:3px;opacity:.5}
.bar .now{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--text-strong)}
.bar i.up{background:var(--pos)} .bar i.down{background:var(--neg)} .bar i.flat{background:var(--muted2)}

.pill{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:10.5px;font-weight:700}
.pill.up{background:var(--pos-t);color:var(--pos)}
.pill.down{background:var(--neg-t);color:var(--neg)}
.pill.flat{background:var(--mut-t);color:var(--muted)}
.pill.stale{background:var(--neg-t);color:var(--neg)}
.empty{color:var(--muted2);padding:20px;font-size:13px}

@media(max-width:820px){
  main{padding:12px}
  th,td{padding:9px 10px}
  .h-24h{display:none}
  .fc{min-width:128px}
}
</style>
</head>
<body>
<header>
  <h1>&#128200; Price Outlook</h1>
  <span class="tick" id="tick">loading&hellip;</span>
  <span class="spacer"></span>
  <a class="nav-btn" href="/">&larr; Dashboard</a>
  <a class="nav-btn" href="/audit">Audit</a>
  <a class="nav-btn" href="/api/debug/signals">Diagnostics</a>
</header>
<main>
  <div class="surges" id="surges"></div>
  <div class="note" id="note"></div>
  <div class="scroll"><table class="tbl">
    <thead><tr>
      <th>Market</th>
      <th class="num">Price</th>
      <th class="num">24h</th>
      <th class="num">Range</th>
      <th class="num">Volume</th>
      <th>Lean</th>
      <th>In 1 hour</th>
      <th>In 4 hours</th>
      <th class="h-24h">In 24 hours</th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="9" class="empty">loading&hellip;</td></tr></tbody>
  </table></div>
</main>
<script>
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function px(v){
  if(v==null) return '—';
  const a=Math.abs(v);
  return a>=1000?v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})
       : a>=1 ? v.toFixed(2) : v.toFixed(4);
}
function signed(v,d){
  if(v==null) return '<span class="muted">—</span>';
  const cls=v>0?'pos':(v<0?'neg':'muted');
  return `<span class="${cls}">${v>0?'+':''}${v.toFixed(d==null?2:d)}%</span>`;
}

// One cell: the band, the centre, and where price sits inside it right now.
function cell(f, price){
  if(!f) return '<td class="muted">—</td>';
  const span=f.high-f.low;
  const at=span>0?Math.max(0,Math.min(100,(price-f.low)/span*100)):50;
  return `<td class="fc">
    <div class="mid">${px(f.centre)} ${signed(f.change_pct)}</div>
    <div class="rng">${px(f.low)} – ${px(f.high)}</div>
    <div class="bar"><i class="${f.direction}" style="left:0;right:0"></i>
      <span class="now" style="left:${at}%"></span></div></td>`;
}

async function load(){
  let d;
  try{
    const r = await fetch('/api/predict');
    if(!r.ok) throw new Error('server returned ' + r.status);
    d = await r.json();
  }catch(e){
    // The ticker line alone left the table sitting on "loading…", which
    // reads as a quiet market rather than a dead request.
    document.getElementById('tick').textContent = 'failed to update';
    Panel.error(document.getElementById('rows'), 'the outlook', e, load);
    return;
  }

  document.getElementById('tick').textContent=
    'updated ' + window.fmtStamp(d.generated_at) + ' · ' + window.fmtAgo(d.generated_at)
    +' IST · refreshes every 20s';
  document.getElementById('note').textContent=d.note;

  document.getElementById('surges').innerHTML=(d.surges||[]).map(s=>{
    const dir=s.direction>0?'up':(s.direction<0?'down':'');
    return `<div class="surge ${dir}"><span class="tag">${esc(s.kind)} surge</span>
      <span><b>${esc(s.symbol)}</b> — ${esc(s.detail)}</span></div>`;
  }).join('');

  const rows=d.markets||[];
  document.getElementById('rows').innerHTML = rows.length ? rows.map(m=>{
    const f=Object.fromEntries((m.forecasts||[]).map(x=>[x.label,x]));
    const dir=m.direction||'flat';
    return `<tr>
      <td><span class="sym">${esc(m.symbol.replace('USDT',''))}</span>
        <div class="sub">${esc(m.symbol)}${m.stale
          ? ' <span class="pill stale">stale '+m.data_age_minutes+'m</span>':''}</div></td>
      <td class="num">${px(m.price)}</td>
      <td class="num">${signed(m.change_24h_pct)}</td>
      <td class="num">${m.atr_pct==null?'—':m.atr_pct.toFixed(3)+'%'}
        <div class="sub">per minute</div></td>
      <td class="num">${m.relative_volume==null?'—':'×'+m.relative_volume.toFixed(2)}</td>
      <td><span class="pill ${dir}">${dir.toUpperCase()}</span>
        <div class="sub">${m.confidence==null?'':Math.round(m.confidence*100)+'% agreement'}</div></td>
      ${cell(f['1h'],m.price)}${cell(f['4h'],m.price)}
      <td class="h-24h" style="padding:0">${cell(f['24h'],m.price).replace(/^<td[^>]*>/,'<div class="fc" style="padding:11px 14px">').replace(/<\\/td>$/,'</div>')}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="9" class="empty">No markets with enough history yet.</td></tr>';
}
load(); setInterval(load, 20000);
</script>
</body>
</html>
"""


_MOVES_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Moves</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);
  min-height:100vh;padding-bottom:60px;font-size:14px;line-height:1.5}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:11px 18px;display:flex;align-items:center;
  gap:10px;flex-wrap:wrap;position:sticky;top:0;z-index:30}
header h1{font-size:16px;font-weight:700;color:var(--text-strong)}
.spacer{margin-left:auto}
.tick{font-size:12px;color:var(--muted)}
a.nav-btn,button.nav-btn{background:var(--acc-t);border:1px solid var(--acc-t2);color:var(--accent);font-size:12px;
  font-weight:600;padding:6px 12px;border-radius:8px;text-decoration:none;white-space:nowrap;cursor:pointer;font-family:inherit}
button.nav-btn:disabled{opacity:.6;cursor:default}
main{padding:18px;max-width:1180px;margin:0 auto;display:grid;gap:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px 18px}
.eyebrow{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;font-weight:700;color:var(--muted2);margin-bottom:8px}
h2{font-size:15px;color:var(--text-strong);margin-bottom:10px}
.hero{display:grid;grid-template-columns:1fr 220px;gap:18px}
.hero p{font-size:15px;color:var(--text-strong);max-width:75ch}
.meta{font-size:12px;color:var(--muted);margin-top:10px}
.tone{border-left:1px solid var(--line);padding-left:18px}
.tone .val{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.gauge{position:relative;height:8px;border-radius:4px;margin:10px 0 6px;
  background:linear-gradient(90deg,var(--neg) 0%,var(--mut-t) 50%,var(--pos) 100%)}
.gauge i{position:absolute;top:-4px;width:3px;height:16px;border-radius:2px;background:var(--text-strong)}
.gauge-l{display:flex;justify-content:space-between;font-size:11px;color:var(--muted2)}
.drivers{display:grid;gap:10px}
.driver{display:grid;grid-template-columns:28px 1fr auto;gap:10px;align-items:start;padding:10px 12px;
  border:1px solid var(--line2);border-radius:10px;background:var(--panel2)}
.arrow{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;font-weight:700}
.arrow.up{background:var(--pos-t);color:var(--pos)} .arrow.down{background:var(--neg-t);color:var(--neg)}
.arrow.mixed{background:var(--mut-t);color:var(--muted)}
.driver .ev{color:var(--text-strong);font-weight:600}
.driver .sub{font-size:12px;color:var(--muted)}
.conf{width:90px;font-size:11px;color:var(--muted);text-align:right}
.conf .bar{height:5px;border-radius:3px;background:var(--sunk);margin-top:4px;overflow:hidden}
.conf .bar b{display:block;height:100%;background:var(--accent)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.coin{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;display:grid;gap:10px;
  border-top:3px solid var(--line)}
.coin.news{border-top-color:var(--accent)} .coin.market_wide{border-top-color:var(--muted2)}
.coin.coin_specific{border-top-color:var(--pos)} .coin.no_clear_cause{border-top-color:var(--neg)}
.coin-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.sym{font-size:17px;font-weight:700;color:var(--text-strong)}
.px{font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.chg{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.chg div{background:var(--panel2);border-radius:8px;padding:6px 8px;text-align:center}
.chg span{display:block;font-size:10px;color:var(--muted2);text-transform:uppercase;letter-spacing:.06em}
.chg b{font-size:14px;font-variant-numeric:tabular-nums}
.pos{color:var(--pos)} .neg{color:var(--neg)} .muted{color:var(--muted)}
.badge{display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;
  padding:3px 8px;border-radius:6px;background:var(--mut-t);color:var(--muted)}
.badge.news{background:var(--acc-t);color:var(--accent)} .badge.coin_specific{background:var(--pos-t);color:var(--pos)}
.badge.no_clear_cause{background:var(--neg-t);color:var(--neg)}
.cause{font-weight:600;color:var(--text-strong)}
.reason{font-size:13.5px;color:var(--text)}
.sigs{border-top:1px dashed var(--line);padding-top:8px;display:grid;gap:6px}
.sigs .lbl{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted2);font-weight:700}
.sig{display:flex;gap:6px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--muted)}
.pill{padding:2px 7px;border-radius:999px;font-size:10.5px;font-weight:700;background:var(--mut-t);color:var(--muted)}
.pill.won,.pill.long{background:var(--pos-t);color:var(--pos)} .pill.lost,.pill.short{background:var(--neg-t);color:var(--neg)}
.review{font-size:13px;color:var(--text);font-style:italic}
.verdict{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.lesson{background:var(--acc-t);border:1px solid var(--acc-t2);border-radius:10px;padding:12px 14px}
.lesson b{color:var(--accent)}
.facts{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.facts span{font-size:12px;background:var(--acc-t);border:1px solid var(--acc-t2);border-radius:8px;padding:3px 8px}
.watch{margin-top:10px;font-size:14px;line-height:1.45}
.pill.wait{background:var(--acc-t);color:var(--muted,#888)}
.meta.warn{color:#b4540a;margin-top:6px;word-break:break-word}
.events{display:grid;gap:6px;font-size:13px}
.events div{display:flex;gap:10px} .events time{color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.hist{display:grid;gap:6px}
.hist button{all:unset;cursor:pointer;display:grid;grid-template-columns:150px 1fr auto;gap:12px;padding:9px 12px;
  border-radius:9px;border:1px solid var(--line2);font-size:13px}
.hist button:hover,.hist button:focus-visible{border-color:var(--accent)}
.hist button.on{background:var(--acc-t);border-color:var(--acc-t2)}
.hist time{color:var(--muted);font-variant-numeric:tabular-nums}
.hist .txt{color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.empty{color:var(--muted);padding:20px;text-align:center}
.lv{display:inline-grid;place-items:center;width:26px;height:26px;border-radius:7px;font-weight:800;font-size:13px}
.lv.l5{background:var(--neg);color:var(--bg)} .lv.l4{background:var(--neg-t);color:var(--neg)}
.lv.l3{background:var(--acc-t);color:var(--accent)} .lv.l2,.lv.l1{background:var(--mut-t);color:var(--muted)}
.evt{display:grid;grid-template-columns:34px 1fr auto;gap:10px;align-items:center;padding:9px 0;border-bottom:1px solid var(--line2)}
.evt:last-child{border-bottom:none}
.evt .t{color:var(--text-strong)} .evt .s{font-size:12px;color:var(--muted)}
.books{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}
.book{background:var(--panel2);border-radius:10px;padding:10px 12px}
.book .n{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.book .v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.book .d{font-size:12px;color:var(--muted)}
table.sb{width:100%;border-collapse:collapse;font-size:13px}
table.sb td,table.sb th{padding:7px 8px;border-bottom:1px solid var(--line2);text-align:left}
table.sb th{font-size:10.5px;color:var(--muted2);text-transform:uppercase;letter-spacing:.06em}
table.sb td.n{text-align:right;font-variant-numeric:tabular-nums}
.sbwrap{overflow-x:auto}
@media(max-width:760px){
  .books{grid-template-columns:repeat(2,1fr)}
  main{padding:12px}
  .hero,.verdict{grid-template-columns:1fr}
  .tone{border-left:none;padding-left:0;border-top:1px solid var(--line);padding-top:12px}
  .hist button{grid-template-columns:1fr;gap:2px}
  .grid{grid-template-columns:1fr}
}
</style>
</head>
<body>
<header>
  <h1>Market Moves</h1>
  <span class="tick" id="tick">loading&hellip;</span>
  <span class="spacer"></span>
  <button class="nav-btn" id="run" type="button" onclick="runNow()">Run analysis now</button>
  <a class="nav-btn" href="/">&larr; Dashboard</a>
  <a class="nav-btn" href="/predict">Outlook</a>
  <a class="nav-btn" href="/audit">Audit</a>
</header>
<main id="main"><div class="card empty">Loading&hellip;</div></main>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function pct(v){ if(v==null||!isFinite(v)) return '<b class="muted">—</b>';
  return '<b class="'+(v>=0?'pos':'neg')+'">'+(v>=0?'+':'')+v.toFixed(2)+'%</b>'; }
function stamp(iso){ return window.fmtStamp? window.fmtStamp(iso) : esc(iso); }
function ago(iso){ return window.fmtAgo? window.fmtAgo(iso) : ''; }
const CAUSE={news:'News event',market_wide:'Whole market',coin_specific:'Coin-specific',no_clear_cause:'No clear cause'};
let DATA=null, SEL=0;
// The measured numbers the model was handed, so its reasoning can be checked.
function factsBlock(f){ if(!f) return ''; const out=[]; const n=v=>(v>=0?'+':'')+v;
  if(f.volume_last3h_vs_prior!=null) out.push('vol 3h '+f.volume_last3h_vs_prior+'× prior');
  if(f.volume_vs_avg!=null) out.push('vol 24h '+f.volume_vs_avg+'× avg');
  if(f.largest_1h_candle) out.push('big candle '+n(f.largest_1h_candle.change_pct)+'% @ '+f.largest_1h_candle.at);
  if(f.structure_15m) out.push('15m '+f.structure_15m);
  if(f.support!=null) out.push('support '+f.support);
  if(f.resistance!=null) out.push('resistance '+f.resistance);
  if(f.funding_8h_pct!=null) out.push('funding '+n(f.funding_8h_pct)+'%');
  if(f.open_interest_1h_pct!=null) out.push('OI 1h '+n(f.open_interest_1h_pct)+'%');
  if(f.order_flow) out.push(f.order_flow.replace(/_/g,' '));
  if(f.vs_btc_4h_pct!=null) out.push('vs BTC 4h '+n(f.vs_btc_4h_pct)+'%');
  return out.length?'<div class="facts">'+out.map(x=>'<span>'+esc(x)+'</span>').join('')+'</div>':''; }

function render(){
  const main=document.getElementById('main');
  if(!DATA || !DATA.runs.length){
    main.innerHTML='<div class="card empty">No analysis yet. It runs every hour once the engine has an hour of prices and a Groq key; press <b>Run analysis now</b> to start one.</div>'
      + eventsCard() + shadowCard() + briefingCard();
    return;
  }
  const run=DATA.runs[SEL], r=run.result||{};
  const moves={}; (run.moves||[]).forEach(m=>moves[m.symbol]=m);
  const btc=moves['btcusdt'];
  let h='';
  // hero
  h+='<section class="card hero"><div><div class="eyebrow">What moved the market · last '+run.window_hours+'h</div>'
    +'<p>'+esc(r.overall||'No summary.')+'</p>'
    +'<div class="meta">'+stamp(run.at)+' · '+ago(run.at)+' · '+esc(run.model)+' · '+Math.round((run.latency_ms||0)/1000)+'s</div>'
    +((r.skipped||[]).length?'<div class="meta warn">Web search skipped: '+r.skipped.map(esc).join(' · ')+'</div>':'')+'</div>';
  const b=DATA.briefing;
  if(b){ const t=Math.max(-1,Math.min(1,b.risk_tone||0));
    h+='<div class="tone"><div class="eyebrow">World risk tone</div><div class="val '+(t>=0?'pos':'neg')+'">'+(t>=0?'+':'')+t.toFixed(2)+'</div>'
      +'<div class="gauge"><i style="left:calc('+((t+1)/2*100)+'% - 1px)"></i></div><div class="gauge-l"><span>risk-off</span><span>risk-on</span></div>'
      +'<div class="meta">briefing '+ago(b.at)+'</div></div>'; }
  else { const mf=((DATA.monitor||{}).failures)||[];
    h+='<div class="tone"><div class="eyebrow">World risk tone</div><div class="meta">No briefing yet.</div>'
      +(mf.length?'<div class="meta warn">Web search failing: '+mf.map(esc).join(' · ')+'</div>':'')+'</div>'; }
  h+='</section>';
  // drivers
  const drivers=r.drivers||[];
  h+='<section class="card"><h2>What drove it</h2><div class="drivers">';
  if(!drivers.length) h+='<div class="empty">No single event stood out in this window.</div>';
  drivers.forEach(d=>{ const a=d.direction==='up'?'↑':d.direction==='down'?'↓':'↕';
    h+='<div class="driver"><div class="arrow '+esc(d.direction)+'">'+a+'</div><div><div class="ev">'+esc(d.event)+'</div>'
      +'<div class="sub">'+(d.when?esc(d.when)+' · ':'')+(d.coins||[]).map(c=>esc(c.toUpperCase())).join(', ')+'</div></div>'
      +'<div class="conf">'+Math.round((d.confidence||0)*100)+'% sure<div class="bar"><b style="width:'+Math.round((d.confidence||0)*100)+'%"></b></div></div></div>'; });
  h+='</div></section>';
  // coins
  const sigBy={}; (run.signals||[]).forEach(s=>(sigBy[s.symbol]=sigBy[s.symbol]||[]).push(s));
  h+='<section><div class="eyebrow" style="margin:0 2px 10px">Coin by coin, compared with BTC'+(btc&&btc.change_4h!=null?' ('+(btc.change_4h>=0?'+':'')+btc.change_4h.toFixed(2)+'% in 4h)':'')+'</div><div class="grid">';
  const coins=(r.coins||[]).slice().sort((a,b)=>Math.abs((moves[b.symbol]||{}).change_4h||0)-Math.abs((moves[a.symbol]||{}).change_4h||0));
  coins.forEach(c=>{ const m=moves[c.symbol]||{}; const sigs=sigBy[c.symbol]||[];
    h+='<article class="coin '+esc(c.cause_type)+'"><div class="coin-head"><span class="sym">'+esc(c.symbol.toUpperCase())+'</span>'
      +'<span class="px">'+(m.price!=null&&window.fmtPrice?'$'+fmtPrice(m.price):(m.price||''))+'</span><span class="spacer"></span>'
      +'<span class="badge '+esc(c.cause_type)+'">'+esc(CAUSE[c.cause_type]||c.cause_type)+'</span></div>'
      +'<div class="chg"><div><span>1h</span>'+pct(m.change_1h)+'</div><div><span>4h</span>'+pct(m.change_4h)+'</div>'
      +'<div><span>12h</span>'+pct(m.change_12h)+'</div><div><span>range</span>'+(m.range_12h!=null?'<b>'+m.range_12h.toFixed(2)+'%</b>':'<b class="muted">—</b>')+'</div></div>'
      +(c.cause?'<div class="cause">'+esc(c.cause)+' <span class="muted" style="font-weight:400;font-size:12px">· '+Math.round((c.confidence||0)*100)+'% sure</span></div>':'')
      +'<div class="reason">'+esc(c.reasoning)+'</div>'
      +'<div class="sigs"><div class="lbl">Our signals ('+sigs.length+')</div>'
      +(sigs.length? sigs.slice(0,6).map(s=>'<div class="sig"><span class="pill '+esc(s.direction)+'">'+esc(s.direction)+'</span>'+esc(s.type)
          +' · '+esc(window.istClock?istClock(s.at):s.at)+' · <span class="pill '+esc(s.outcome)+'">'+esc(s.outcome)+'</span>'
          +(s.outcome!=='pending'?' '+(s.pnl_pct>=0?'+':'')+s.pnl_pct.toFixed(2)+'%':'')
          +(s.blocked_by?' · <span class="muted">blocked by '+esc(s.blocked_by)+'</span>':'')+'</div>').join('')
        : '<div class="sig">None fired on this coin.</div>')
      +(c.signals_review?'<div class="review">'+esc(c.signals_review)+'</div>':'')+'</div>'
      +factsBlock(m.facts)
      +(c.watch?'<div class="watch">'+(c.bias?'<span class="pill '+esc(c.bias)+'">'+esc(c.bias)+'</span> ':'')+'<b>Watch:</b> '+esc(c.watch)+'</div>':'')
      +'</article>'; });
  h+='</div></section>';
  // verdict
  h+='<section class="card verdict"><div><h2>How our signals did</h2><p>'+esc(r.signals_verdict||'No verdict.')+'</p></div>'
    +'<div>'+(r.lesson?'<div class="lesson"><b>One change to make:</b> '+esc(r.lesson)+'</div>':'')
    +upcomingBlock()+'</div></section>';
  h+=eventsCard()+shadowCard();
  h+=briefingCard();
  // history
  h+='<section class="card"><h2>Earlier analyses</h2><div class="hist">'
    +DATA.runs.map((x,i)=>'<button type="button" class="'+(i===SEL?'on':'')+'" onclick="pick('+i+')"><time>'+stamp(x.at)+'</time>'
      +'<span class="txt">'+esc(((x.result||{}).overall||'').split('. ')[0])+'</span>'
      +'<span class="muted">'+((x.result||{}).coins||[]).filter(c=>c.cause_type==='news').length+' news-driven</span></button>').join('')
    +'</div></section>';
  main.innerHTML=h;
}
function upcomingBlock(){
  const u=(DATA&&DATA.upcoming)||[]; if(!u.length) return '';
  return '<div style="margin-top:14px"><div class="eyebrow">Next scheduled releases (trading pauses around these)</div><div class="events">'
    +u.map(e=>'<div><time>'+stamp(e.at)+'</time><span>'+esc(e.name)+'</span></div>').join('')+'</div></div>';
}
function briefingCard(){
  const b=DATA&&DATA.briefing; if(!b) return '';
  return '<section class="card"><h2>Latest world briefing</h2><p>'+esc(b.summary)+'</p>'
    +((b.events||[]).length?'<div class="events" style="margin-top:12px">'+b.events.map(e=>'<div><span class="badge">'+esc(e.event_type)+'</span><span>'+esc(e.headline)
      +(e.source?' <span class="muted">· '+esc(e.source)+'</span>':'')+'</span></div>').join('')+'</div>':'')
    +'<div class="meta">'+stamp(b.at)+' · '+esc(b.model)+'</div></section>';
}
const BOOKS={A_pause:'A · Pause (no trade)',B_double_at_release:'B · Your idea: 2x at release, 0.3% trail',
  C_confirmed_breakout:'C · Wait, trade the breakout, 2x size',D_basket:'D · Basket BTC+ETH+SOL'};
function eventsCard(){
  const ev=(DATA&&DATA.events)||[];
  let h='<section class="card"><h2>World events, last 14 days <span class="muted" style="font-weight:400;font-size:12px">· level 1-5 · shadow mode</span></h2>';
  if(!ev.length) return h+'<div class="empty">No events tracked yet. The monitor checks every 5-45 minutes depending on what is happening.</div></section>';
  h+=ev.slice(0,40).map(e=>{ const c=e.level_confirmed;
    return '<div class="evt"><span class="lv l'+e.level+'">'+e.level+'</span><div><div class="t">'+esc(e.title)+'</div>'
      +'<div class="s">'+stamp(e.at)+' · '+esc(e.category.replace(/_/g,' '))+(e.source?' · '+esc(e.source):'')+(e.status!=='active'?' · '+esc(e.status):'')+'</div></div>'
      +'<div class="s" style="text-align:right">'+(c!=null?'market said <b>L'+c+'</b><br>BTC '+(e.btc_move_2h_pct!=null?e.btc_move_2h_pct.toFixed(2)+'% in 2h':''):'waiting for<br>2h of prices')+'</div></div>'; }).join('');
  return h+'</section>';
}
function shadowCard(){
  const rows=(DATA&&DATA.shadow)||[]; const ev={}; ((DATA&&DATA.events)||[]).forEach(e=>ev[e.id]=e);
  let h='<section class="card"><h2>Event trading, shadow books</h2><p class="meta" style="margin:0 0 12px">Every level 4-5 event is replayed four ways on the prices that followed it. P&amp;L is % of wallet after fees. Nothing here was traded; it decides which rule goes live after two weeks.</p>';
  if(!rows.length) return h+'<div class="empty">No level 4-5 event has 6 hours of prices after it yet. Next scheduled: '+esc(((DATA.upcoming||[])[0]||{}).name||'see the list above')+'.</div></section>';
  const tot={}, n={}, wins={};
  rows.forEach(r=>{ tot[r.book]=(tot[r.book]||0)+r.wallet_pct; n[r.book]=(n[r.book]||0)+1; if(r.wallet_pct>0) wins[r.book]=(wins[r.book]||0)+1; });
  h+='<div class="books">'+Object.keys(BOOKS).map(b=>{ const v=tot[b]||0;
    return '<div class="book"><div class="n">'+esc(BOOKS[b])+'</div><div class="v '+(v>0?'pos':v<0?'neg':'muted')+'">'+(v>=0?'+':'')+v.toFixed(2)+'%</div>'
      +'<div class="d">'+(n[b]||0)+' events · '+(wins[b]||0)+' won</div></div>'; }).join('')+'</div>';
  h+='<div class="sbwrap"><table class="sb"><tr><th>Event</th><th>Book</th><th>Side</th><th class="n">P&amp;L</th><th>What happened</th></tr>'
    +rows.slice(0,60).map(r=>'<tr><td>'+esc(((ev[r.event_id]||{}).title||'#'+r.event_id).slice(0,60))+'</td><td>'+esc(BOOKS[r.book]||r.book)+'</td><td>'+esc(r.side||'—')+'</td>'
      +'<td class="n '+(r.wallet_pct>0?'pos':r.wallet_pct<0?'neg':'muted')+'">'+(r.wallet_pct>=0?'+':'')+r.wallet_pct.toFixed(2)+'%</td><td class="muted">'+esc(r.note)+'</td></tr>').join('')
    +'</table></div>';
  return h+'</section>';
}
function pick(i){ SEL=i; render(); window.scrollTo({top:0,behavior:'smooth'}); }
async function load(){
  try{ const r=await fetch('/api/moves?limit=48'); DATA=await r.json(); }catch(e){ DATA={runs:[],upcoming:[]}; }
  document.getElementById('tick').textContent=DATA.runs.length?('updated '+(window.fmtAgo?fmtAgo(DATA.runs[0].at):'')):'no analysis yet';
  render();
}
async function runNow(){
  const btn=document.getElementById('run'); btn.disabled=true; btn.textContent='Running… (about a minute)';
  const f=window.apiFetch||fetch;
  const res=await f('/api/moves/run',{method:'POST'});
  if(!res.ok){ btn.disabled=false; btn.textContent='Run analysis now'; return; }
  setTimeout(async()=>{ await load(); btn.disabled=false; btn.textContent='Run analysis now'; },75000);
}
function fmtPrice(p){ if(p==null) return '—'; const a=Math.abs(p);
  return a>=1000?p.toLocaleString('en-US',{maximumFractionDigits:2}):a>=1?p.toFixed(4):p.toPrecision(4); }
load(); setInterval(load,300000);
</script>
</body>
</html>
"""


_HUB_SNIPPET = """
<style>
/* Page groups: one strip of related pages at the top of every page (see HUB_GROUPS). */
.hub-strip{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding:8px 12px;margin:0 0 12px;
  border-bottom:1px solid rgba(127,127,127,.25);font:13px/1.3 system-ui,-apple-system,"Segoe UI",sans-serif}
.hub-strip b{margin-right:6px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;opacity:.65}
.hub-strip a{padding:4px 11px;border-radius:999px;text-decoration:none;color:inherit;opacity:.78;
  border:1px solid rgba(127,127,127,.32);white-space:nowrap}
.hub-strip a:hover{opacity:1}
.hub-strip a.on{opacity:1;font-weight:600;background:rgba(127,127,127,.2)}
@media(max-width:640px){.hub-strip{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch}}
</style>
<script>
(function(){
  /* Six groups instead of twenty menu entries. Every page still exists at its own address. */
  var G = [
    {id:"trading", label:"Trading", items:[["Overview","/#dashboard"],["Paper book","/#paper"],["Session guard","/#guard"]]},
    {id:"signals", label:"Signals", items:[["Live signals","/#crypto"],["Mirror","/#mirror"],["Accuracy","/#accuracy"],["Audit","/audit"],["History","/#historic"]]},
    {id:"market", label:"Market", items:[["Price outlook","/predict"],["Market moves","/moves"],["Chart","/chart"]]},
    {id:"research", label:"Research", items:[["Pipeline","/pipeline"],["v2 shadow","/v2"],["Simulator","/#simulator"]]},
    {id:"settings", label:"Settings", items:[["Settings","/settings"],["Keys","/keys"],["Watchlist","/#watchlist"]]},
    {id:"admin", label:"Admin", items:[["Data","/data"],["Diagnostics","/api/debug/binance"],["API list","/api-docs"],["Journal","/journal"]]}
  ];
  window.HUB_GROUPS = G;
  function here(){ return location.pathname === "/" ? "/" + (location.hash || "#dashboard") : location.pathname.replace(/\\/$/, ""); }
  window.hubGroupOf = function(url){
    for (var i = 0; i < G.length; i++) for (var j = 0; j < G[i].items.length; j++) if (G[i].items[j][1] === url) return G[i];
    return null;
  };
  window.renderHub = function(){
    var cur = here(), g = window.hubGroupOf(cur), el = document.getElementById("hub-strip");
    if (!g) { if (el) el.remove(); return; }
    if (!el) {
      el = document.createElement("nav"); el.id = "hub-strip"; el.className = "hub-strip"; el.setAttribute("aria-label", g.label + " pages");
      var head = document.querySelector(".main-head");
      if (head && head.parentNode) head.parentNode.insertBefore(el, head); else document.body.insertBefore(el, document.body.firstChild);
    }
    var onMain = location.pathname === "/";
    el.innerHTML = "<b>" + g.label + "</b>" + g.items.map(function(it){
      var tab = onMain && it[1].indexOf("/#") === 0 ? it[1].slice(2) : "";
      return '<a href="' + it[1] + '"' + (it[1] === cur ? ' class="on" aria-current="page"' : "") + (tab ? ' data-hub-tab="' + tab + '"' : "") + ">" + it[0] + "</a>";
    }).join("");
  };
  document.addEventListener("click", function(e){
    var a = e.target.closest && e.target.closest("[data-hub-tab]");
    if (a && typeof window.switchTab === "function") { e.preventDefault(); window.switchTab(a.getAttribute("data-hub-tab")); }
  });
  document.addEventListener("DOMContentLoaded", window.renderHub);
  window.addEventListener("hashchange", window.renderHub);
})();
</script>
"""

_THEME_SNIPPET = """
<style>
/* Theme palettes */
/* Wide tables become cards on a phone. A fifteen-column audit row in a
   horizontal scroller is readable in the sense that the pixels are present:
   you cannot see a symbol and its result at the same time, which is the only
   reason to look. Rows stack, each value carries its column name.
   Opt in with class="tbl"; labelTables fills the labels. */
@media(max-width:640px){
  .tbl,.tbl tbody,.tbl tr,.tbl td{display:block;width:100%}
  .tbl thead{display:none}
  .tbl tr{background:var(--panel,#171f2e);border:1px solid var(--line,#28324a);
    border-radius:9px;padding:10px 12px;margin-bottom:9px}
  .tbl td{border:none;padding:3px 0;white-space:normal;font-size:12px;
    display:flex;justify-content:space-between;align-items:baseline;gap:12px}
  .tbl td::before{content:attr(data-label);color:var(--muted2,#6d7b93);
    font-size:10px;text-transform:uppercase;letter-spacing:.05em;flex:none}
  .tbl td:first-child{padding-bottom:7px;margin-bottom:5px;
    border-bottom:1px solid var(--line2,#1a2333);font-weight:600}
  /* A cell holding a panel is a message, not a value — no label, no columns. */
  .tbl td:has(.pnl){display:block;padding:0}
  .tbl td:has(.pnl)::before{content:none}
}

/* Panel states — one look for loading, empty and failed, on every page. */
.pnl{padding:20px 16px;text-align:center;border-radius:10px;
  background:var(--panel2,#111827);border:1px solid var(--line,#28324a)}
.pnl-t{font-size:13px;font-weight:600;color:var(--text,#e8edf6)}
.pnl-d{font-size:11.5px;color:var(--muted,#9fadc4);margin-top:5px;line-height:1.6}
.pnl-err .pnl-t{color:var(--neg,#ff6b5e)}
.pnl-load .pnl-t{color:var(--muted,#9fadc4);font-weight:500}
.pnl-r{margin-top:11px;font:inherit;font-size:12px;padding:7px 15px;min-height:38px;
  border-radius:7px;cursor:pointer;background:var(--accent,#e3b341);
  color:var(--sunk,#0c1220);border:0;font-weight:600}

/* ── Palette ───────────────────────────────────────────────────────────────
   One variable set drives the sidebar, tables and signal cards. The older
   components are still reskinned by the !important block in the theme
   snippet; these are done properly so a theme actually changes them rather
   than leaving the shell blue while the cards move. */
:root{
  --bg:#15120e; --panel:#201b15; --panel2:#1a1611; --sunk:#0f0c09;
  --line:#3a3128; --line2:#241e18;
  --text:#e8e0d4; --text-strong:#fdf8f0; --muted:#9a8b78; --muted2:#6d6154;
  --accent:#f59e0b; --accent2:#fbbf24; --accent-soft:#fcd34d;
  --pos:#4ade80; --pos-strong:#22c55e; --pos-btn:#16a34a;
  --neg:#f87171; --neg-strong:#ef4444; --neg-btn:#dc2626;

  --pos-t:  color-mix(in srgb, var(--pos-strong) 14%, transparent);
  --pos-t2: color-mix(in srgb, var(--pos-strong) 34%, transparent);
  --neg-t:  color-mix(in srgb, var(--neg-strong) 14%, transparent);
  --neg-t2: color-mix(in srgb, var(--neg-strong) 34%, transparent);
  --acc-t:  color-mix(in srgb, var(--accent) 16%, transparent);
  --acc-t2: color-mix(in srgb, var(--accent) 36%, transparent);
  --mut-t:  color-mix(in srgb, var(--muted) 14%, transparent);
}
html[data-theme="amber"]{
  --bg:#15120e; --panel:#201b15; --panel2:#1a1611; --sunk:#0f0c09;
  --line:#3a3128; --line2:#241e18;
  --text:#e8e0d4; --text-strong:#fdf8f0; --muted:#9a8b78; --muted2:#6d6154;
  --accent:#f59e0b; --accent2:#fbbf24; --accent-soft:#fcd34d;
}
html[data-theme="navy"]{
  --bg:#0f172a; --panel:#1e293b; --panel2:#1b2534; --sunk:#0b1220;
  --line:#334155; --line2:#16202f;
  --text:#e2e8f0; --text-strong:#f1f5f9; --muted:#94a3b8; --muted2:#64748b;
  --accent:#0ea5e9; --accent2:#38bdf8; --accent-soft:#7dd3fc;
}
html[data-theme="carbon"]{
  --bg:#0b0b0c; --panel:#151517; --panel2:#121213; --sunk:#070708;
  --line:#2b2b2f; --line2:#1b1b1e;
  --text:#e4e4e7; --text-strong:#fafafa; --muted:#8b8b93; --muted2:#5f5f66;
  --accent:#a3e635; --accent2:#bef264; --accent-soft:#d9f99d;
}
html[data-theme="crimson"]{
  --bg:#160f11; --panel:#211619; --panel2:#1b1214; --sunk:#0f0a0b;
  --line:#3d2830; --line2:#261a1e;
  --text:#f0dfe3; --text-strong:#fff5f6; --muted:#a8858f; --muted2:#755c64;
  --accent:#fb7185; --accent2:#fda4af; --accent-soft:#fecdd3;
  --pos:#5eead4; --pos-strong:#2dd4bf; --pos-btn:#0d9488;
}
html[data-theme="violet"]{
  --bg:#13111c; --panel:#1c1729; --panel2:#181226; --sunk:#0d0b14;
  --line:#322b4d; --line2:#241f38;
  --text:#e9e4f5; --text-strong:#f5f3fa; --muted:#8b81a8; --muted2:#665d80;
  --accent:#a78bfa; --accent2:#c4b5fd; --accent-soft:#ddd6fe;
}
html[data-theme="emerald"]{
  --bg:#0a1410; --panel:#102219; --panel2:#0c1b13; --sunk:#06100b;
  --line:#1d3b2d; --line2:#12281d;
  --text:#dcefe6; --text-strong:#f0fdf4; --muted:#6b9080; --muted2:#4d6b5d;
  --accent:#34d399; --accent2:#6ee7b7; --accent-soft:#a7f3d0;
}
/* Neumorphism ("soft UI"): surfaces share the page colour and are shaped
   by a light and a dark shadow instead of borders. Two variants, on the
   same amber accent: dark and light. */
html[data-theme="neu"]{
  --bg:#23201c; --panel:#23201c; --panel2:#23201c; --sunk:#1c1a17;
  --line:#2e2a25; --line2:#2a2622;
  --text:#e6ddcf; --text-strong:#fbf5ea; --muted:#a39580; --muted2:#7d705f;
  --accent:#e0a84a; --accent2:#f0bf6a; --accent-soft:#f6d59a;
  --neu-d:#171512; --neu-l:#2f2b26;
}
html[data-theme="neu-light"]{
  --bg:#e6e9ef; --panel:#e6e9ef; --panel2:#e6e9ef; --sunk:#dde1e8;
  --line:#d3d8e0; --line2:#dde1e8;
  --text:#3b4150; --text-strong:#1f2430; --muted:#6b7385; --muted2:#8a93a5;
  --accent:#b8741a; --accent2:#cf8a2a; --accent-soft:#8c5510;
  --pos:#15803d; --pos-strong:#16a34a; --pos-btn:#15803d;
  --neg:#b91c1c; --neg-strong:#dc2626; --neg-btn:#b91c1c;
  --neu-d:#c3c8d2; --neu-l:#ffffff;
}
html[data-theme^="neu"] body{background:var(--bg)!important}
html[data-theme^="neu"] .card,html[data-theme^="neu"] .pnl,html[data-theme^="neu"] .cr-coin,
html[data-theme^="neu"] .stat,html[data-theme^="neu"] article,html[data-theme^="neu"] .tbl tr,
html[data-theme^="neu"] .pt-cell,html[data-theme^="neu"] .driver,html[data-theme^="neu"] .sidebar{
  background:var(--bg)!important;border-color:transparent!important;border-radius:16px!important;
  box-shadow:6px 6px 14px var(--neu-d),-6px -6px 14px var(--neu-l)!important}
html[data-theme^="neu"] .sidebar{border-radius:0 18px 18px 0!important}
html[data-theme^="neu"] button,html[data-theme^="neu"] .nav-btn,html[data-theme^="neu"] .side-item,
html[data-theme^="neu"] .cr-page-btn,html[data-theme^="neu"] .pill,html[data-theme^="neu"] .pt-chip{
  background:var(--bg)!important;border-color:transparent!important;border-radius:12px!important;
  box-shadow:4px 4px 9px var(--neu-d),-4px -4px 9px var(--neu-l)!important}
html[data-theme^="neu"] button:active,html[data-theme^="neu"] .nav-btn:active,
html[data-theme^="neu"] .side-item.active,html[data-theme^="neu"] .side-item.on,
html[data-theme^="neu"] .side-item[aria-current]{
  box-shadow:inset 3px 3px 7px var(--neu-d),inset -3px -3px 7px var(--neu-l)!important}
html[data-theme^="neu"] input,html[data-theme^="neu"] select,html[data-theme^="neu"] textarea,
html[data-theme^="neu"] .cr-input{background:var(--bg)!important;border-color:transparent!important;
  border-radius:12px!important;
  box-shadow:inset 3px 3px 7px var(--neu-d),inset -3px -3px 7px var(--neu-l)!important}
html[data-theme^="neu"] table td,html[data-theme^="neu"] table th{border-color:var(--line2)!important}
html[data-theme="light"]{
  --bg:#f4f1ea; --panel:#ffffff; --panel2:#faf8f4; --sunk:#ece7dd;
  --line:#ded7c9; --line2:#eee9df;
  --text:#2c2620; --text-strong:#1a1611; --muted:#7a6f61; --muted2:#9c9285;
  --accent:#b45309; --accent2:#d97706; --accent-soft:#92400e;
  --pos:#15803d; --pos-strong:#16a34a; --pos-btn:#15803d;
  --neg:#b91c1c; --neg-strong:#dc2626; --neg-btn:#b91c1c;
}


/* Overrides applied only when a non-default theme is active */
html[data-theme] body{background:var(--bg)!important;color:var(--text)!important}
html[data-theme] header,html[data-theme] .topbar,html[data-theme] .tab-bar{background:var(--panel)!important;border-color:var(--line)!important}
html[data-theme] header h1,html[data-theme] .topbar h1,html[data-theme] h2,html[data-theme] .tab-btn.active{color:var(--text-strong)!important}
html[data-theme] .card,html[data-theme] .status-card,html[data-theme] .match-card,html[data-theme] .signal-card,html[data-theme] .fb-card,html[data-theme] .fb-sig-card,html[data-theme] .scalp-card,html[data-theme] .mc2,html[data-theme] .wc-group,html[data-theme] .cr-coin,html[data-theme] .cr-sig-card,html[data-theme] .cr-comm-card,html[data-theme] .cr-sig-tab{background:var(--panel)!important;border-color:var(--line)!important}
html[data-theme] .mc2-top,html[data-theme] .mc2-dt,html[data-theme] .mc2-details,html[data-theme] .mc2-ob,html[data-theme] .mc-scoreboard,html[data-theme] .mc-header,html[data-theme] .sc-header,html[data-theme] .sc-footer,html[data-theme] .sc-probs,html[data-theme] .scalp-head,html[data-theme] .scalp-foot,html[data-theme] .fb-header,html[data-theme] .mc-prob,html[data-theme] .mc-odds-box,html[data-theme] .scroll,html[data-theme] .toc a,html[data-theme] .wc-group-hd,html[data-theme] .cr-note,html[data-theme] .cr-input,html[data-theme] .cr-sig-warn,html[data-theme] .cr-glossary,html[data-theme] .cr-page-btn{background:var(--panel2)!important;border-color:var(--line2)!important}
html[data-theme] .card-value,html[data-theme] .mc2-plname,html[data-theme] .mc2-setnow b,html[data-theme] .mvm .val,html[data-theme] .sb-cur,html[data-theme] .sb-sets-total,html[data-theme] .mc-name,html[data-theme] .mc-sets-won,html[data-theme] .mc-game-score,html[data-theme] .sc-bet-player,html[data-theme] .scalp-player,html[data-theme] .fb-team-name,html[data-theme] .fb-score,html[data-theme] .status-val,html[data-theme] .prob-pct,html[data-theme] .mc2-problbl b,html[data-theme] .card-name,html[data-theme] .meta-value,html[data-theme] .sc-conf,html[data-theme] .toc a,html[data-theme] .tbl-head h2,html[data-theme] .cr-coin-sym,html[data-theme] .cr-coin-price,html[data-theme] .cr-comm-price,html[data-theme] .cr-sig-sym{color:var(--text-strong)!important}
html[data-theme] .card-title,html[data-theme] .card-sub,html[data-theme] .refresh,html[data-theme] section h2,html[data-theme] .mc2-lbl span,html[data-theme] .mvm .lab,html[data-theme] .status-name,html[data-theme] .empty,html[data-theme] footer,html[data-theme] .subtitle,html[data-theme] .card-meta,html[data-theme] .meta-label,html[data-theme] .mc2-obimp,html[data-theme] .mc2-obname,html[data-theme] .mc2-setnow,html[data-theme] .mc2-problbl,html[data-theme] .note,html[data-theme] #status,html[data-theme] .mvm .h{color:var(--muted)!important}
html[data-theme] .mc2-sets{color:var(--score)!important}
html[data-theme] .mc2-lbl h3{color:var(--accent)!important}
html[data-theme] .mc2-obval{color:var(--p1)!important}
html[data-theme] .mc2-obval.none{color:var(--muted)!important}
html[data-theme] .mc2-pb1,html[data-theme] .prob-bar-p1{background:var(--p1)!important}
html[data-theme] .mc2-pb2,html[data-theme] .prob-bar-p2{background:var(--p2)!important}
html[data-theme] .mc2-probbar,html[data-theme] .prob-bar-wrap,html[data-theme] .quota-bar{background:var(--barbg)!important}
html[data-theme] .mvm .val.best{background:var(--bestbg)!important;color:var(--best)!important}
html[data-theme] .mc2-ob.fav{border-color:var(--favline)!important;background:var(--favbg)!important}
html[data-theme] .mc2-ob.fav .mc2-obval{color:var(--fav)!important}
html[data-theme] table th{background:var(--panel)!important;color:var(--muted)!important;border-color:var(--line2)!important}
html[data-theme] table td{border-color:var(--line2)!important}
html[data-theme] tr:nth-child(even) td{background:var(--panel2)!important}
html[data-theme] .topbar a{color:var(--accent)!important;border-color:var(--line)!important}
html[data-theme="light"] .src,html[data-theme="light"] .source-tag{background:#dbeafe!important;color:#1d4ed8!important}
/* Theme picker (settings page) */
.theme-row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:26px}
.theme-sw{display:flex;align-items:center;gap:8px;background:transparent;border:2px solid #30363d;color:inherit;font-size:13px;font-weight:700;padding:9px 16px;border-radius:10px;cursor:pointer;font-family:inherit}
.theme-sw.on{border-color:#3fb950;box-shadow:0 0 0 1px #3fb950}
.theme-sw .sw{width:16px;height:16px;border-radius:50%;display:inline-block;border:1px solid rgba(128,128,128,.4)}
</style>
<script>
(function(){
  var t=localStorage.getItem('site_theme')||'amber';
  document.documentElement.setAttribute('data-theme',t);
  // The dots render after this runs, so mark the active one once they exist.
  document.addEventListener('DOMContentLoaded',function(){
    document.querySelectorAll('.side-themes .dot').forEach(function(d){
      d.classList.toggle('on',d.dataset.t===t);});
    setTimeout(function(){
      if(typeof updateSimulatorEstimates === 'function') updateSimulatorEstimates();
      if(typeof fetchAIEvents === 'function') fetchAIEvents();
    }, 400);
  });
})();
function setSiteTheme(t){
  localStorage.setItem('site_theme',t);
  document.documentElement.setAttribute('data-theme',t);
  document.querySelectorAll('.theme-sw').forEach(function(b){b.classList.toggle('on',b.dataset.t===t);});
  document.querySelectorAll('.side-themes .dot').forEach(function(d){d.classList.toggle('on',d.dataset.t===t);});
}

// Wrapper around fetch() for the handful of endpoints that now require the
// API bearer token (settings toggle, watchlist edits — see
// scheduler/security.py for why those three and not the read endpoints).
// The token lives in localStorage, entered once via a prompt the first time
// a call comes back 401, and is retried exactly once with the fresh token —
// a second failure means the token itself is wrong and is left to surface
// as an error rather than looping the prompt.
async function apiFetch(url, opts){
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers);
  var token = localStorage.getItem('api_token');
  if(token) opts.headers['Authorization'] = 'Bearer ' + token;
  var res = await fetch(url, opts);
  if(res.status === 401){
    var entered = window.prompt('API token needed for this action (API_AUTH_TOKEN on the server):');
    if(!entered) return res;
    localStorage.setItem('api_token', entered);
    opts.headers['Authorization'] = 'Bearer ' + entered;
    res = await fetch(url, opts);
    if(res.status === 401) localStorage.removeItem('api_token');
  }
  return res;
}

// One timestamp format for every page.
//
// There were two implementations of fmtTime and they had drifted: the audit
// page printed "24 Sep, 15:39" and the dashboard printed "03:39 pm IST" with
// no date at all. That was survivable while trades lasted twenty minutes and
// everything on screen was obviously from today. It stopped being survivable
// when the stop widened — holds run for hours now and the closed-trade history
// spans days, so "11:14 am" no longer says which day, and two rows an hour
// apart on screen can be two days apart in fact.
//
// Times are rendered in IST because that is where the operator is; the wire
// format is UTC throughout, so a stamp arriving without a zone is read as UTC
// rather than as the viewer's local time, which is the bug that makes
// everything look 5h30m early.
if(!window.fmtStamp){
  // Every time on every page is shown in IST (owner's request, 26 Sep). The
  // wire and the database stay UTC; a stamp without a zone is read as UTC.
  var _toDate = function(iso){
    if(!iso) return null;
    if(iso instanceof Date) return iso;
    var d = new Date(/[Zz+]|-\\d\\d:\\d\\d$/.test(iso) ? iso : String(iso).replace(' ','T') + 'Z');
    return isNaN(d) ? null : d;
  };
  window.fmtStamp = function(iso, opts){
    var d = _toDate(iso); if(!d) return iso ? String(iso) : '—';
    var o = opts || {};
    // Parts in IST, month named from a fixed list: browsers disagree on
    // "Sep" vs "Sept", and a date column should not change with the phone.
    var p = {};
    new Intl.DateTimeFormat('en-GB', {timeZone:'Asia/Kolkata', year:'numeric', month:'numeric',
      day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'})
      .formatToParts(d).forEach(function(x){ p[x.type] = x.value; });
    var M = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var s = p.day + ' ' + M[Number(p.month) - 1]
      // The year only earns its place once it is not this one.
      + (Number(p.year) !== new Date().getFullYear() ? ' ' + p.year : '')
      + ' ' + p.hour + ':' + p.minute + (o.seconds ? ':' + p.second : '');
    return o.noZone ? s : s + ' IST';
  };
  // Time of day only, for rows that are obviously today: "14:05 IST".
  window.istClock = function(iso){
    var d = _toDate(iso); if(!d) return '—';
    // The clock part of the one formatter, so there is still one format.
    var s = window.fmtStamp(d, {noZone:true});
    return s.slice(s.lastIndexOf(' ') + 1) + ' IST';
  };
  // "just now", "12m ago", "1h 20m ago", "3d ago" — and "in 45m" for the future.
  window.fmtAgo = function(iso){
    var d = _toDate(iso); if(!d) return '';
    var s = Math.round((Date.now() - d.getTime()) / 1000), fut = s < 0;
    s = Math.abs(s);
    var txt;
    if(s < 45) return fut ? 'in a moment' : 'just now';
    if(s < 3600) txt = Math.max(1, Math.round(s/60)) + 'm';
    else if(s < 86400){ var h = Math.floor(s/3600), m = Math.round((s % 3600)/60);
      txt = h + 'h' + (m && h < 6 ? ' ' + m + 'm' : ''); }
    else { var dd = Math.floor(s/86400), hh = Math.round((s % 86400)/3600);
      txt = dd + 'd' + (hh && dd < 3 ? ' ' + hh + 'h' : ''); }
    return fut ? 'in ' + txt : txt + ' ago';
  };
  // Both at once: "26 Sep 14:05 IST · 3m ago".
  window.fmtWhen = function(iso){
    var d = _toDate(iso); if(!d) return '—';
    return window.fmtStamp(d) + ' · ' + window.fmtAgo(d);
  };
  // A UTC hour-of-day as IST wall clock: 7 -> "12:30".
  window.istHour = function(h){
    var m = ((Math.round(Number(h) * 60) + 330) % 1440 + 1440) % 1440;
    return String(Math.floor(m/60)).padStart(2,'0') + ':' + String(m % 60).padStart(2,'0');
  };
  // Relative labels stay true without a reload: <span data-ago="iso"></span>.
  setInterval(function(){
    document.querySelectorAll('[data-ago]').forEach(function(el){
      el.textContent = window.fmtAgo(el.getAttribute('data-ago')); });
  }, 30000);
}

// Carry each table's column names onto its cells so the phone layout can
// print them beside the values. One observer rather than a call at the end
// of every render: tables are built in a dozen places across five pages and
// one missed call is an unlabelled table with no other symptom.
if(!window.labelTables){
  window.labelTables = function(root){
    (root||document).querySelectorAll('table.tbl').forEach(function(t){
      var heads = [].slice.call(t.querySelectorAll('thead th'))
                    .map(function(th){
        // Own text only. A sortable header carries its arrow in a child
        // span, and textContent would fold that in — every card on the
        // paper-trading history would be labelled "Closed↓".
        var own = '';
        [].slice.call(th.childNodes).forEach(function(n){
          if(n.nodeType === 3) own += n.textContent;
        });
        return (own || th.textContent).trim();
      });
      if(!heads.length) return;
      t.querySelectorAll('tbody tr').forEach(function(tr){
        [].slice.call(tr.children).forEach(function(td, i){
          if(heads[i] && !td.hasAttribute('data-label'))
            td.setAttribute('data-label', heads[i]);
        });
      });
    });
  };
  new MutationObserver(function(){ window.labelTables(); })
    .observe(document.documentElement, {childList:true, subtree:true});
}

// ── Panel states ────────────────────────────────────────────────────────
// A blank panel means one of three different things — still loading, loaded
// and genuinely empty, or failed — and until now they all looked the same.
// /audit had five fetches and one catch that wrote anything to the screen,
// so four of its failure paths left the page sitting on whatever was there
// before, with no way to tell a quiet market from a dead endpoint.
//
// Panel.load() makes that distinction unavoidable: it writes the loading
// state, runs the fetch, and turns any failure into a message naming what
// broke and offering a retry. A caller cannot forget the catch because the
// catch is the wrapper.
window.Panel = (function(){
  function box(el, cls, title, detail, retry){
    if(!el) return;
    el.innerHTML =
      '<div class="pnl pnl-' + cls + '">'
      + '<div class="pnl-t">' + title + '</div>'
      + (detail ? '<div class="pnl-d">' + detail + '</div>' : '')
      + (retry ? '<button class="pnl-r" type="button">Try again</button>' : '')
      + '</div>';
    if(retry){
      var b = el.querySelector('.pnl-r');
      if(b) b.addEventListener('click', retry);
    }
  }
  function loading(el, what){ box(el, 'load', 'Loading ' + (what || 'data') + '…'); }
  // `why` is the point: "nothing yet" and "nothing matching your filters"
  // are different answers and send the reader somewhere different.
  function empty(el, what, why){ box(el, 'empty', 'No ' + what, why || ''); }
  function error(el, what, err, retry){
    box(el, 'err', 'Could not load ' + what,
        (err && err.message ? err.message : String(err || 'the request failed')),
        retry);
  }
  async function load(el, what, fn){
    loading(el, what);
    try { return await fn(); }
    catch(e){
      console.error('panel ' + what + ':', e);
      error(el, what, e, function(){ load(el, what, fn); });
      return null;
    }
  }
  return {loading: loading, empty: empty, error: error, load: load, box: box};
})();

// ── Display currency ────────────────────────────────────────────────────
// Every money figure the server sends is INR — margin, P&L, fees, wallet —
// because position size is computed as qty x price x usdt_inr. Prices are
// already USDT and must never be converted; putting a rupee sign on an
// entry price would simply be wrong.
//
// The rate travels with the data rather than being assumed here: a closed
// trade keeps the rate it was booked at, so reading history back through
// today's rate cannot silently restate it.
//
// The choice is per-browser on purpose. It is a display preference, not
// account state, so it belongs in localStorage rather than a round trip.
window.Money = (function(){
  var KEY = 'display_ccy', ccy = 'USDT';
  try { ccy = localStorage.getItem(KEY) || 'USDT'; } catch(e){}

  function get(){ return ccy; }
  function set(next){
    ccy = (next === 'INR') ? 'INR' : 'USDT';
    try { localStorage.setItem(KEY, ccy); } catch(e){}
    document.dispatchEvent(new CustomEvent('ccychange', {detail: ccy}));
  }
  // rate: what one USDT was worth in INR for THIS figure.
  function fmt(inrValue, rate, signed){
    if(inrValue === null || inrValue === undefined || isNaN(inrValue)) return '—';
    rate = rate || 102;
    var v = (ccy === 'INR') ? inrValue : inrValue / rate;
    var sign = signed ? (v > 0 ? '+' : v < 0 ? '−' : '') : (v < 0 ? '−' : '');
    var body = Math.abs(v).toLocaleString('en-IN',
      {minimumFractionDigits: 2, maximumFractionDigits: 2});
    return sign + (ccy === 'INR' ? '₹' : '') + body + (ccy === 'INR' ? '' : ' USDT');
  }
  function label(){ return ccy === 'INR' ? '₹' : 'USDT'; }

  function mount(){
    var sel = document.getElementById('ccy-select');
    if(!sel) return;
    sel.value = ccy;
    sel.addEventListener('change', function(){ set(sel.value); });
  }
  if(document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', mount);
  else mount();

  return {get: get, set: set, fmt: fmt, label: label};
})();
</script>
"""

# The browser must never re-derive the cost model. These come straight from
# the same ScalpConfig the analyzers use, so recalibrating fees updates the
# page and the engine together.
_HTML = (
    _HTML
    # The FULL round trip — brokerage, the spread crossed twice, and what
    # fills actually cost beyond it. This used to be brokerage alone, so every
    # "x cost" chip on the site read 1.42x higher than the trade really earns:
    # a 0.845% ETH target showed as 7.2x when the engine, and /audit, scored
    # the same target at 5.0x. Two pages disagreeing about the same number is
    # how a losing edge gets believed.
    .replace("__BREAK_EVEN_PCT__", f"{_SCALP.cost_floor_pct * 100:.4f}")
    .replace("__MIN_TARGET_PCT__", f"{_SCALP.min_target_pct * 100:.4f}")
    .replace("__PAPER_LEVERAGE__", f"{_SETTINGS.paper_leverage:g}")
)
_THEME_SNIPPET = _THEME_SNIPPET + _HUB_SNIPPET
_HTML = _HTML.replace("</head>", _THEME_SNIPPET + "</head>")
_DATA_HTML = _DATA_HTML.replace("</head>", _THEME_SNIPPET + "</head>")
_SETTINGS_HTML = _SETTINGS_HTML.replace("</head>", _THEME_SNIPPET + "</head>")
_AUDIT_HTML = _AUDIT_HTML.replace("</head>", _THEME_SNIPPET + "</head>")
_PREDICT_HTML = _PREDICT_HTML.replace("</head>", _THEME_SNIPPET + "</head>")
_MOVES_HTML = _MOVES_HTML.replace("</head>", _THEME_SNIPPET + "</head>")


@web.middleware
async def _compress_middleware(request: web.Request, handler):
    """gzip text responses over 1 KB: the dashboard is 115 KB raw, 33 KB gzipped."""
    resp = await handler(request)
    if (isinstance(resp, web.Response) and not isinstance(resp, web.FileResponse)
            and "gzip" in request.headers.get("Accept-Encoding", "")
            and resp.body is not None and len(resp.body) > 1024
            and resp.compression is None):
        resp.enable_compression()
    return resp


async def _api_debug_perf(runner, request: web.Request) -> web.Response:
    """GET /api/debug/perf — slowest routes, slowest jobs, event-loop stalls, DB ping."""
    from scheduler import cache
    from scheduler.perf import report
    if not await _verify_admin_session(request):
        from scheduler.security import check_bearer_auth
        denied = check_bearer_auth(request, _SETTINGS.api_auth_token)
        if denied is not None:
            return denied
    from collectors.llm_client import calls_today
    body = report()
    body["cache_age_s"] = cache.stats()
    # Per-role AI call count since the last UTC midnight — every ask_json()
    # call counts here regardless of which feature made it, so a runaway
    # role (mirror review re-reviewing far more than expected, say) is
    # visible immediately instead of only showing up in a provider bill.
    body["ai_calls_today"] = calls_today()
    return _json_response(body)


async def _api_debug_null_test(runner, request: web.Request) -> web.Response:
    """
    GET /api/debug/null-test — do the live signals beat random entries over
    the same bars? The same question scripts/null_test.py answers from a
    terminal, runnable from the browser so it does not need SSH to the box.

    Reuses analysis.null_test.compare_against_random (the actual comparison)
    and scripts.null_test.load (the same crypto_signal_log/crypto_snapshots
    query the script uses) rather than keeping a second copy of either.
    Query params mirror the script's flags: days, trials, stop, target, hold.
    Read-only — writes nothing.
    """
    from analysis.instruments import spec_for
    from analysis.null_test import compare_against_random, render
    from scripts.null_test import load as _load_null_test_inputs

    def _num(name: str, default: float, kind):
        raw = request.query.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            return kind(raw)
        except (TypeError, ValueError):
            return default

    # Bounded rather than left open: a trials count in the thousands or a
    # days window past what crypto_snapshots retains would just make this
    # request take minutes for no better an answer than the defaults give.
    days = max(1, min(365, _num("days", 120, int)))
    trials = max(10, min(2000, _num("trials", 200, int)))
    stop_pct = _num("stop", 0.92, float)
    target_pct = _num("target", 1.84, float)
    hold_hours = _num("hold", 24.0, float)

    entries, paths = await _load_null_test_inputs(days)
    if not entries:
        return _json_response(
            {"ok": False, "reason": f"no signals in the last {days} days"})
    if not paths:
        return _json_response(
            {"ok": False, "reason": "no usable price history for that window"})

    cost_pct = spec_for(next(iter(paths))).round_trip_pct * 100
    result = compare_against_random(entries, paths, stop_pct, target_pct,
                                    hold_hours, cost_pct, trials=trials)
    return _json_response({
        "ok": True,
        "params": {"days": days, "trials": trials, "stop_pct": stop_pct,
                  "target_pct": target_pct, "hold_hours": hold_hours,
                  "cost_pct": round(cost_pct, 4)},
        "entries": result.entries,
        "real_total_pct": round(result.real_total, 2),
        "random_mean_pct": round(result.random_mean, 2),
        "random_sd_pct": round(result.random_sd, 2),
        "edge_pct": round(result.edge, 2),
        "p_value": round(result.p_value, 4),
        "verdict": result.verdict,
        "summary": render(result, stop_pct, target_pct, hold_hours),
    })


async def make_app(runner) -> web.Application:
    from scheduler.security import rate_limit_middleware, security_headers_middleware

    # Order matters: rate limiting runs first so a limited request never
    # reaches a handler at all, and both wrap every handler uniformly rather
    # than being opted into per-route — a cross-cutting concern bolted onto
    # individual handlers is the one that gets forgotten on the next new
    # endpoint.
    from scheduler.perf import timing_middleware
    app = web.Application(middlewares=[
        timing_middleware,          # outermost: times the whole request
        rate_limit_middleware(lambda: _SETTINGS),
        security_headers_middleware,
        _compress_middleware,
    ])

    def _bind(handler_fn):
        async def _bound(req: web.Request) -> web.Response:
            return await handler_fn(runner, req)
        return _bound

    app.router.add_get("/", _dashboard)
    app.router.add_get("/data", _data_page)
    app.router.add_get("/api/tables", _bind(_api_tables))
    app.router.add_get("/health", _bind(_health))
    app.router.add_get("/api/status", _bind(_api_status))
    app.router.add_get("/api/debug", _bind(_api_debug))
    from scheduler.settings_page import register as _register_settings
    from scheduler.settings_page import settings_page
    app.router.add_get("/settings", settings_page)
    app.router.add_get("/settings/classic", _settings_page)
    _register_settings(app, runner)
    from scheduler.keys_page import register as _register_keys
    _register_keys(app, runner)
    from scheduler.api_docs import register as _register_api_docs
    _register_api_docs(app)
    app.router.add_get("/api/settings", _bind(_api_collector_states))
    app.router.add_post("/api/auth/verify", _bind(_api_auth_verify))
    app.router.add_post("/api/settings/auth/login", _bind(_api_settings_auth_login))
    app.router.add_get("/api/settings/auth/status", _bind(_api_settings_auth_status))
    app.router.add_post("/api/settings/auth/logout", _bind(_api_settings_auth_logout))
    app.router.add_get("/api/paper/config", _bind(_api_paper_config_get))
    app.router.add_post("/api/paper/config", _bind(_api_paper_config_post))
    app.router.add_get("/api/strategy/config", _bind(_api_strategy_config_get))
    app.router.add_post("/api/strategy/config", _bind(_api_strategy_config_post))
    app.router.add_post("/api/settings/toggle", _bind(_api_collector_toggle))
    # Crypto & Commodities Routes
    app.router.add_get("/api/crypto/coins", _bind(_api_crypto_coins))
    app.router.add_get("/api/crypto/signals", _bind(_api_crypto_signals))
    app.router.add_post("/api/crypto/signals/live-check", _bind(_api_crypto_signals_live_check))
    app.router.add_get("/api/simulator/status", _bind(_api_simulator_status))
    app.router.add_get("/api/simulator/config", _bind(_api_simulator_get_config))
    app.router.add_post("/api/simulator/config", _bind(_api_simulator_save_config))
    app.router.add_get("/api/simulator/cycles", _bind(_api_simulator_cycles))
    app.router.add_get("/api/simulator/cycle-ledger", _bind(_api_simulator_cycle_ledger))
    app.router.add_get("/api/simulator/ai-events", _bind(_api_simulator_ai_events))
    app.router.add_post("/api/simulator/start", _bind(_api_simulator_start))
    app.router.add_post("/api/simulator/pause", _bind(_api_simulator_pause))
    app.router.add_get("/api/crypto/forecasts", _bind(_api_crypto_forecasts))
    app.router.add_get("/api/paper", _bind(_api_paper))
    app.router.add_get("/api/swing", _bind(_api_swing))
    app.router.add_get("/api/llm/budget", _bind(_api_llm_budget))
    app.router.add_get("/api/paper/events", _bind(_api_paper_events))
    app.router.add_get("/api/debug/coindcx", _bind(_api_debug_coindcx))
    app.router.add_get("/api/research", _bind(_api_research))
    app.router.add_post("/api/sentiment/ingest", _bind(_api_sentiment_ingest))
    app.router.add_get("/api/sentiment/recent", _bind(_api_sentiment_recent))
    app.router.add_get("/api/signals/history", _bind(_api_signal_history))
    app.router.add_get("/api/debug/signals", _bind(_api_debug_signals))
    app.router.add_get("/predict", _predict_page)
    app.router.add_get("/api/predict", _bind(_api_predict))
    app.router.add_get("/api/signals/accuracy", _bind(_api_signal_accuracy))
    app.router.add_get("/audit", _audit_page)
    app.router.add_get("/api/audit", _bind(_api_audit))
    app.router.add_get("/api/audit/methods", _bind(_api_audit_methods))
    app.router.add_get("/api/audit/reviewer", _bind(_api_reviewer_scorecard))
    app.router.add_get("/api/reviews", _bind(_api_reviews))
    app.router.add_get("/api/debug/volume", _bind(_api_debug_volume))
    app.router.add_get("/api/crypto/watchlist", _bind(_api_crypto_watchlist))
    app.router.add_get("/api/binance/symbols", _bind(_api_binance_symbols))
    app.router.add_get("/api/events", _bind(_api_events))
    app.router.add_get("/api/moves", _bind(_api_moves))
    app.router.add_post("/api/moves/run", _bind(_api_moves_run))
    app.router.add_get("/moves", _moves_page)
    app.router.add_post("/api/crypto/watchlist/add", _bind(_api_crypto_watchlist_add))
    app.router.add_post("/api/crypto/watchlist/remove", _bind(_api_crypto_watchlist_remove))
    app.router.add_get("/api/commodities", _bind(_api_commodities))
    app.router.add_get("/api/debug/binance", _bind(_api_binance_probe))
    app.router.add_get("/api/debug/perf", _bind(_api_debug_perf))
    app.router.add_get("/api/debug/null-test", _bind(_api_debug_null_test))
    app.router.add_get("/api/pipeline/progress", _bind(_api_pipeline_progress))
    app.router.add_get("/pipeline", _pipeline_page)
    from scheduler.chart_page import api_chart_klines, api_chart_overlays, chart_page
    app.router.add_get("/api/chart/klines", _bind(api_chart_klines))
    app.router.add_get("/api/chart/overlays", _bind(api_chart_overlays))
    app.router.add_get("/chart", chart_page)
    from scheduler.v2_pages import register as _register_v2_pages

    _register_v2_pages(app, runner)
    return app


async def _api_pipeline_progress(runner, request: web.Request) -> web.Response:
    from pathlib import Path
    p_file = Path("data/reports/pipeline_progress.json")
    if p_file.exists():
        try:
            return web.Response(text=p_file.read_text(), content_type="application/json")
        except Exception:
            pass
    return web.Response(text=json.dumps({"status": "idle"}), content_type="application/json")


async def _pipeline_page(request: web.Request) -> web.Response:
    html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pipeline & Multi-Year Backtest Monitor</title>
<style>
body { background: #0b1120; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; padding: 24px; line-height: 1.5; margin: 0; }
.container { max-width: 1400px; margin: 0 auto; }
.card { background: #1e293b; border-radius: 12px; padding: 20px; margin-bottom: 20px; border: 1px solid #334155; }
.title { font-size: 18px; font-weight: 700; margin-bottom: 12px; color: #38bdf8; display: flex; align-items: center; justify-content: space-between; }
.phase { margin: 12px 0; }
.bar-bg { background: #334155; border-radius: 6px; height: 10px; overflow: hidden; margin-top: 4px; }
.bar-fill { background: linear-gradient(90deg, #10b981, #38bdf8); height: 100%; width: 0%; transition: width 0.4s ease; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 14px; }
.stat-box { background: #0f172a; padding: 14px; border-radius: 8px; border: 1px solid #334155; }
.stat-val { font-size: 22px; font-weight: bold; color: #38bdf8; }
.stat-label { font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; }
.year-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; margin-top: 10px; }
.year-card { background: #0f172a; padding: 16px; border-radius: 10px; border: 1px solid #334155; }
.year-title { font-size: 16px; font-weight: bold; color: #f1f5f9; display: flex; justify-content: space-between; margin-bottom: 8px; }
.year-count { font-size: 14px; color: #38bdf8; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
.badge-macro_economic { background: rgba(139, 92, 246, 0.2); color: #c084fc; border: 1px solid rgba(139, 92, 246, 0.4); }
.badge-technical_breakout { background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.4); }
.badge-whale_manipulation { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
.badge-regulatory { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
.badge-exchange_event { background: rgba(6, 182, 212, 0.2); color: #22d3ee; border: 1px solid rgba(6, 182, 212, 0.4); }
.badge-news_panic { background: rgba(244, 63, 94, 0.2); color: #fb7185; border: 1px solid rgba(244, 63, 94, 0.4); }
.badge-other { background: rgba(100, 116, 139, 0.2); color: #cbd5e1; border: 1px solid rgba(100, 116, 139, 0.4); }
.badge-year { background: #334155; color: #f8fafc; font-weight: bold; }
.feed-table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
.feed-table th { text-align: left; padding: 10px 12px; background: #0f172a; color: #94a3b8; border-bottom: 2px solid #334155; font-size: 11px; text-transform: uppercase; }
.feed-table td { padding: 10px 12px; border-bottom: 1px solid #1e293b; vertical-align: top; }
.feed-table tr:hover { background: #1a2436; }
.controls { display: flex; gap: 12px; margin-top: 10px; margin-bottom: 10px; }
.controls input, .controls select { background: #0f172a; border: 1px solid #334155; color: #f8fafc; padding: 8px 12px; border-radius: 6px; font-size: 13px; outline: none; }
.controls input:focus, .controls select:focus { border-color: #38bdf8; }
.controls input { flex: 1; }
.move-pos { color: #34d399; font-weight: 600; }
.move-neg { color: #fb7185; font-weight: 600; }
.refresh-badge { font-size: 12px; color: #64748b; font-weight: normal; }
</style>
</head>
<body>
<div class="container">
  <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
    <div style="display:flex; gap:16px; font-size:13px;">
      <a href="/" style="color:#94a3b8; text-decoration:none; font-weight:500;">← Main Dashboard</a>
      <a href="/pipeline" style="color:#38bdf8; text-decoration:none; font-weight:bold;">⚡ Pipeline Monitor</a>
      <a href="/chart" style="color:#38bdf8; text-decoration:none; font-weight:500;">📊 Interactive TradingView Chart</a>
    </div>
    <span class="refresh-badge">Auto-refreshing every 3s</span>
  </div>
  <div class="card">
    <div class="title">
      <span>🚀 Multi-Year Backtest & HF Pipeline Monitor</span>
    </div>
    <div id="meta" style="color: #94a3b8; font-size: 13px;">Connecting to pipeline stream...</div>
  </div>


  <div class="card">
    <div class="title">Execution Phases</div>
    <div id="phases"></div>
  </div>

  <div class="card">
    <div class="title">
      <span>📈 Multi-Window Strategy & Paper Trading Scorecards (1m, 6m, 1y, 2y, 3y)</span>
      <span style="font-size: 12px; color: #94a3b8;" id="scorecard-summary">Live Simulated Performance</span>
    </div>
    <div style="overflow-x: auto; max-height: 480px;">
      <table class="feed-table" id="scorecard-table">
        <thead>
          <tr>
            <th style="width: 100px;">Coin</th>
            <th style="width: 80px;">Window</th>
            <th style="width: 130px;">Paper Trades</th>
            <th style="width: 110px;">Win Rate %</th>
            <th style="width: 110px;">Profit Factor</th>
            <th style="width: 120px;">Max DD (R)</th>
            <th style="width: 120px;">Anti-Flip Vetos</th>
            <th>60m Timeouts Saved</th>
          </tr>
        </thead>
        <tbody id="scorecard-body">
          <tr><td colspan="8" style="text-align:center; color:#94a3b8;">Loading multi-window strategy scorecards...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <div class="title">Multi-Year Research Breakdown (2023 - 2026)</div>
    <div id="year_cards" class="year-grid"></div>
  </div>


  <div class="card">
    <div class="title">AI Categorization & Aggregate Metrics</div>
    <div id="stats" class="grid"></div>
  </div>

  <div class="card">
    <div class="title">
      <span>Live 500+ Researched Anomaly Feed</span>
      <span id="feed-count" style="font-size: 12px; color: #94a3b8;">0 events</span>
    </div>
    <div class="controls">
      <input type="text" id="search-input" placeholder="Search by Coin, Year, or Insight keywords..." oninput="filterFeed()">
      <select id="year-filter" onchange="filterFeed()">
        <option value="">All Years</option>
        <option value="2026">2026</option>
        <option value="2025">2025</option>
        <option value="2024">2024</option>
        <option value="2023">2023</option>
      </select>
    </div>
    <div style="overflow-x: auto; max-height: 600px;">
      <table class="feed-table">
        <thead>
          <tr>
            <th style="width: 140px;">Time</th>
            <th style="width: 60px;">Year</th>
            <th style="width: 80px;">Coin</th>
            <th style="width: 80px;">Move %</th>
            <th style="width: 70px;">Z-Score</th>
            <th style="width: 140px;">Category</th>
            <th>AI Research Discovery & Reasoning</th>
          </tr>
        </thead>
        <tbody id="feed-body">
          <tr><td colspan="7" style="text-align: center; color: #64748b; padding: 24px;">Awaiting researched events stream...</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>

<script>
let allEvents = [];

function getCategoryBadge(cat) {
  const c = (cat || 'no_correlation').toLowerCase().replace(' ', '_');
  const cls = ['macro_economic', 'technical_breakout', 'whale_manipulation', 'regulatory', 'exchange_event', 'news_panic'].includes(c)
    ? 'badge-' + c : 'badge-other';
  return `<span class="badge ${cls}">${c.replace('_', ' ')}</span>`;
}

function filterFeed() {
  const query = (document.getElementById('search-input').value || '').toLowerCase().trim();
  const yearSel = document.getElementById('year-filter').value;
  
  const filtered = allEvents.filter(ev => {
    if (yearSel && String(ev.year || '') !== yearSel) return false;
    if (!query) return true;
    const hay = `${ev.symbol || ''} ${ev.year || ''} ${ev.category || ''} ${ev.reasoning || ''}`.toLowerCase();
    return hay.includes(query);
  });

  document.getElementById('feed-count').innerText = `${filtered.length} of ${allEvents.length} events`;
  
  if (filtered.length === 0) {
    document.getElementById('feed-body').innerHTML = `<tr><td colspan="7" style="text-align: center; color: #64748b; padding: 20px;">No events match filter</td></tr>`;
    return;
  }

  let html = '';
  for (const ev of filtered.slice(0, 300)) {
    const ts = (ev.timestamp || '').replace('T', ' ').substring(0, 19);
    const moveVal = Number(ev.pct_change != null ? ev.pct_change : (ev.move_pct || 0));
    const moveClass = moveVal >= 0 ? 'move-pos' : 'move-neg';
    const moveStr = (moveVal >= 0 ? '+' : '') + moveVal.toFixed(2) + '%';
    const zScore = Number(ev.z_score || 0).toFixed(1) + 'σ';
    const sym = (ev.symbol || 'N/A').replace('USDT', '');
    const yr = ev.year || ts.substring(0, 4) || '—';
    const reasoning = ev.reasoning || 'Statistical volume anomaly verified.';

    html += `<tr>
      <td style="color:#94a3b8; font-size:12px;">${ts}</td>
      <td><span class="badge badge-year">${yr}</span></td>
      <td><strong style="color:#f8fafc;">${sym}</strong></td>
      <td class="${moveClass}">${moveStr}</td>
      <td style="color:#cbd5e1;">${zScore}</td>
      <td>${getCategoryBadge(ev.category)}</td>
      <td style="color:#e2e8f0;">${reasoning}</td>
    </tr>`;
  }
  document.getElementById('feed-body').innerHTML = html;
}

async function refresh() {
  try {
    const res = await fetch('/api/pipeline/progress');
    const data = await res.json();
    if (!data || !data.phases) return;
    document.getElementById('meta').innerText = 'Started: ' + (data.started_at || 'n/a').substring(0, 19) + ' UTC | Updated: ' + (data.updated_at || 'n/a').substring(0, 19) + ' UTC';
    
    // Phases
    let phaseHtml = '';
    for (const [k, p] of Object.entries(data.phases || {})) {
      const pct = p.total_tasks > 0 ? Math.round((p.completed_tasks / p.total_tasks) * 100) : (p.status === 'completed' ? 100 : 0);
      let unit = '';
      if (k.includes('cycle')) unit = ' cycles';
      else if (k.includes('volume')) unit = ' events';
      else if (k.includes('backtest')) unit = ' paper trades';
      else if (k.includes('data_download')) unit = ' coins';
      else if (k.includes('reasoning')) unit = ' AI batches';

      const compStr = Number(p.completed_tasks).toLocaleString();
      const totStr = Number(p.total_tasks || 0).toLocaleString();
      phaseHtml += `<div class="phase">
        <div style="display:flex; justify-content:space-between; font-size:13px;">
          <span><strong>${p.name}</strong> ${p.current_item ? '<span style="color:#94a3b8">(' + p.current_item + ')</span>' : ''}</span>
          <span>${pct}% (${compStr} / ${totStr}${unit})</span>
        </div>
        <div class="bar-bg"><div class="bar-fill" style="width: ${pct}%;"></div></div>
      </div>`;
    }
    document.getElementById('phases').innerHTML = phaseHtml;

    // Multi-Window Scorecards
    if (data.backtest_scorecards && Object.keys(data.backtest_scorecards).length > 0) {
      let scHtml = '';
      let totalTradesSum = 0;
      for (const [k, sc] of Object.entries(data.backtest_scorecards)) {
        totalTradesSum += (sc.trades || 0);
        const wr = (sc.win_rate * 100).toFixed(1);
        const pf = sc.profit_factor;
        const pfClass = pf >= 1.2 ? 'move-pos' : (pf >= 1.0 ? 'style="color:#38bdf8;"' : 'move-neg');
        const wrClass = wr >= 45.0 ? 'move-pos' : 'style="color:#f1f5f9;"';
        scHtml += `<tr>
          <td><strong style="color:#f8fafc;">${sc.symbol}</strong></td>
          <td><span class="badge badge-year">${sc.window_label || sc.window_years + 'y'}</span></td>
          <td style="font-weight:bold; color:#38bdf8;">${Number(sc.trades).toLocaleString()}</td>
          <td class="${wrClass}">${wr}%</td>
          <td class="${pfClass}">${pf}</td>
          <td style="color:#fb7185;">${sc.max_drawdown_r} R</td>
          <td style="color:#34d399;">${sc.anti_flip_vetos || 0}</td>
          <td style="color:#fbbf24;">${sc.stagnation_exits || 0}</td>
        </tr>`;
      }
      document.getElementById('scorecard-body').innerHTML = scHtml;
      document.getElementById('scorecard-summary').innerText = `${Object.keys(data.backtest_scorecards).length} runs • ${totalTradesSum.toLocaleString()} total paper trades evaluated`;
    }


    // Year Breakdown
    let yearHtml = '';
    const years = ['2026', '2025', '2024', '2023'];
    for (const yr of years) {
      const yData = (data.year_breakdown && data.year_breakdown[yr]) || { total: 0 };
      yearHtml += `<div class="year-card">
        <div class="year-title"><span>Year ${yr}</span><span class="year-count">${yData.total || 0} events</span></div>
        <div style="font-size:12px; color:#94a3b8; line-height:1.7;">
          <div>Breakouts: <strong style="color:#38bdf8;">${yData.technical_breakout || 0}</strong></div>
          <div>Macro / Fed: <strong style="color:#c084fc;">${yData.macro_economic || 0}</strong></div>
          <div>Whale / Flow: <strong style="color:#fbbf24;">${yData.whale_manipulation || 0}</strong></div>
          <div>Regulatory: <strong style="color:#34d399;">${yData.regulatory || 0}</strong></div>
        </div>
      </div>`;
    }
    document.getElementById('year_cards').innerHTML = yearHtml;

    // Stats
    let statsHtml = `
      <div class="stat-box"><div class="stat-label">Events Researched</div><div class="stat-val">${data.ai_batches_completed || 0}</div></div>
      <div class="stat-box"><div class="stat-label">Target Data Points</div><div class="stat-val">${data.ai_batches_total || 550}</div></div>
      <div class="stat-box"><div class="stat-label">Retries / Fallback</div><div class="stat-val" style="color:#f43f5e;">${data.ai_batches_failed || 0}</div></div>
    `;
    for (const [cat, count] of Object.entries(data.ai_categories || {})) {
      statsHtml += `<div class="stat-box"><div class="stat-label">${cat.replace('_', ' ')}</div><div class="stat-val">${count}</div></div>`;
    }
    document.getElementById('stats').innerHTML = statsHtml;

    // Events feed
    if (data.events_feed && data.events_feed.length !== allEvents.length) {
      allEvents = data.events_feed;
      filterFeed();
    }
  } catch (e) {
    console.error(e);
  }
}
setInterval(refresh, 3000);
refresh();
</script>
</body>
</html>"""
    return web.Response(text=html.replace("</head>", _THEME_SNIPPET + "</head>"), content_type="text/html")


async def _dashboard(request: web.Request) -> web.Response:
    return web.Response(text=_HTML, content_type="text/html")


async def start_health_server(runner, port: int = 8080) -> web.AppRunner:
    app = await make_app(runner)
    web_runner = web.AppRunner(app)
    await web_runner.setup()
    site = web.TCPSite(web_runner, "0.0.0.0", port)
    await site.start()
    return web_runner
