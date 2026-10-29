"""
Interactive TradingView-Style Multi-Timeframe Chart & Analytics Page.

Chart Pro (Lightweight Charts):
- Multi-TF candles from lake parquet + volume pane
- Indicators: EMA 20/50/200, Bollinger, VWAP, RSI, MACD
- Strategy filter: same dispatcher as market simulator (scan + DB history overlays)
- TP/SL from live swing book (3R / 1R) on latest marker
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from aiohttp import web
import numpy as np
import pandas as pd

from analysis.chart_scan import scan_strategy_markers
from analysis.simulator.strategy_dispatcher import AVAILABLE_STRATEGIES
from storage.repository import Repository


OVERLAYS_FILE = Path("data/reports/chart_overlays.json")
PROGRESS_FILE = Path("data/reports/pipeline_progress.json")


def _generate_synthetic_series(symbol: str, tf: str, count: int = 500) -> list[dict]:
    """Fallback generator for smooth interactive preview when lake data is missing."""
    np.random.seed(abs(hash(symbol + tf)) % (2**31))
    base_price = 65000.0 if "BTC" in symbol else (3400.0 if "ETH" in symbol else 160.0)
    
    tf_minutes = {
        "1m": 1, "5m": 5, "15m": 15, "30m": 30,
        "1h": 60, "4h": 240, "8h": 480, "1d": 1440
    }.get(tf, 60)
    
    import time
    now_ts = int(time.time()) - (count * tf_minutes * 60)
    
    bars = []
    cur_p = base_price
    for i in range(count):
        ret = np.random.normal(0.0002, 0.008)
        cur_p = max(1.0, cur_p * (1.0 + ret))
        high = cur_p * (1.0 + np.random.uniform(0.001, 0.008))
        low = cur_p * (1.0 - np.random.uniform(0.001, 0.008))
        open_p = cur_p * (1.0 + np.random.uniform(-0.003, 0.003))
        vol = float(np.random.exponential(scale=250.0))
        
        bars.append({
            "time": now_ts + (i * tf_minutes * 60),
            "open": round(open_p, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(cur_p, 2),
            "volume": round(vol, 2),
        })
    return bars


def _load_chart_bars(sym: str, tf: str, limit: int = 600) -> list[dict]:
    """Shared kline loader for chart APIs."""
    sym = sym.upper()
    tf = tf.lower()
    limit = max(50, min(int(limit), 2000))

    if OVERLAYS_FILE.exists():
        try:
            data = json.loads(OVERLAYS_FILE.read_text())
            cached = data.get("klines", {}).get(sym, {}).get(tf)
            if cached and len(cached) > 0:
                return cached[-limit:]
        except Exception:
            pass

    lake_5m = Path(f"data/lake/um/klines/{sym}/5m")
    if lake_5m.exists() and list(lake_5m.glob("*.parquet")):
        try:
            parquets = sorted(list(lake_5m.glob("*.parquet")))[-6:]
            dfs = [pd.read_parquet(p) for p in parquets]
            merged = pd.concat(dfs, ignore_index=True)
            if "open_time" in merged.columns:
                merged["datetime"] = pd.to_datetime(merged["open_time"], unit="ms", utc=True)
                merged = merged.set_index("datetime").sort_index()

            from scripts.backtest_multi_year_pipeline import resample_klines

            resampled = resample_klines(merged, tf) if tf != "5m" else merged
            tail_df = resampled.tail(limit)
            return [
                {
                    "time": int(ts.timestamp()),
                    "open": round(float(r["open"]), 2),
                    "high": round(float(r["high"]), 2),
                    "low": round(float(r["low"]), 2),
                    "close": round(float(r["close"]), 2),
                    "volume": round(float(r["volume"]), 2),
                }
                for ts, r in tail_df.iterrows()
            ]
        except Exception:
            pass

    return _generate_synthetic_series(sym, tf, limit)


async def api_chart_klines(runner, request: web.Request) -> web.Response:
    """Return historical candlestick bars for requested symbol and timeframe."""
    sym = request.query.get("symbol", "BTCUSDT").upper()
    tf = request.query.get("tf", "1h").lower()
    limit = int(request.query.get("limit", 600))
    bars = _load_chart_bars(sym, tf, limit)
    return web.Response(text=json.dumps(bars), content_type="application/json")


async def api_chart_strategies(runner, request: web.Request) -> web.Response:
    return web.Response(text=json.dumps(AVAILABLE_STRATEGIES), content_type="application/json")


async def api_chart_strategy_scan(runner, request: web.Request) -> web.Response:
    sym = request.query.get("symbol", "BTCUSDT").upper()
    tf = request.query.get("tf", "1h").lower()
    strategy = request.query.get("strategy", "all").lower()
    min_conf = float(request.query.get("min_confidence", "0.60"))
    limit = int(request.query.get("limit", 800))
    t0 = time.perf_counter()
    bars = _load_chart_bars(sym, tf, limit)
    markers = scan_strategy_markers(
        bars,
        strategy=strategy,
        min_confidence=min_conf,
        step=2 if len(bars) > 400 else 1,
    )
    return web.Response(
        text=json.dumps({"symbol": sym, "tf": tf, "strategy": strategy, "markers": markers, "elapsed_ms": int((time.perf_counter() - t0) * 1000)}),
        content_type="application/json",
    )


async def api_chart_history_signals(runner, request: web.Request) -> web.Response:
    sym = request.query.get("symbol", "BTCUSDT").upper()
    days = int(request.query.get("days", 60))
    strategy = request.query.get("strategy", "").lower().strip()
    out: list[dict[str, Any]] = []
    try:
        async with runner.session_factory() as session:
            repo = Repository(session)
            rows = await repo.crypto_signals_between(days, limit=800)
        sym_l = sym.lower()
        for r in rows:
            if (r.symbol or "").lower().replace("/", "") != sym_l.replace("/", ""):
                continue
            st = (r.signal_type or "").lower()
            if strategy and strategy not in st and st != strategy:
                continue
            ts = r.timestamp
            t_sec = int(ts.timestamp()) if ts else 0
            is_long = (r.direction or "").lower() == "long"
            out.append(
                {
                    "time": t_sec,
                    "entry_time": ts.isoformat() if ts else "",
                    "direction": "LONG" if is_long else "SHORT",
                    "strategy": r.signal_type,
                    "confidence": float(r.confidence or 0),
                    "entry": float(r.current_price or 0),
                    "tp": float(r.target_price or 0) if r.target_price else None,
                    "sl": float(r.stop_loss or 0) if r.stop_loss else None,
                    "outcome": r.outcome,
                    "source": "history",
                }
            )
    except Exception:
        pass
    return web.Response(text=json.dumps({"symbol": sym, "signals": out[:200]}), content_type="application/json")


async def api_chart_overlays(runner, request: web.Request) -> web.Response:
    """Return projected signals, trade targets, and news/event overlays."""
    sym = request.query.get("symbol", "BTCUSDT").upper()
    signals = []
    events = []
    cycle_report = {}

    if OVERLAYS_FILE.exists():
        try:
            data = json.loads(OVERLAYS_FILE.read_text())
            signals = [s for s in data.get("signals", []) if s.get("symbol") == sym]
            events = [e for e in data.get("events", []) if e.get("symbol") == sym or e.get("symbol") == "ALL"]
            cycle_report = data.get("cycle_reports", {}).get(sym, {})
        except Exception:
            pass

    # Also include recent feed events from pipeline_progress if available
    if PROGRESS_FILE.exists() and not events:
        try:
            p_data = json.loads(PROGRESS_FILE.read_text())
            feed = p_data.get("events_feed", [])
            events = [e for e in feed if e.get("symbol") == sym]
        except Exception:
            pass

    return web.Response(text=json.dumps({
        "symbol": sym,
        "signals": signals[:150],
        "events": events[:80],
        "cycle_report": cycle_report,
    }), content_type="application/json")

async def chart_page(request: web.Request) -> web.Response:
    """TradingView Lightweight Charts + engine strategy scanner."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chart Pro — Multi-TF · Strategies · Indicators</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#0b1120;--panel:#131d31;--border:#223049;--text:#f1f5f9;--muted:#94a3b8;--primary:#38bdf8}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}
.header{background:var(--panel);border-bottom:1px solid var(--border);padding:8px 14px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.brand{font-weight:700;color:var(--primary);font-size:14px}
.toolbar{background:#0f172a;border-bottom:1px solid var(--border);padding:6px 12px;display:flex;flex-wrap:wrap;gap:10px;align-items:center;max-height:42vh;overflow-y:auto}
.tool-block{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:4px 8px;background:rgba(15,23,42,0.6);border:1px solid var(--border);border-radius:8px}
.tool-label{font-size:10px;text-transform:uppercase;color:var(--muted);letter-spacing:.06em;margin-right:4px}
.pill-btn{background:var(--panel);border:1px solid var(--border);color:var(--muted);padding:3px 8px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer}
.pill-btn.active,.pill-btn:hover{color:var(--primary);border-color:var(--primary)}
.toggle{font-size:11px;color:var(--muted);display:flex;align-items:center;gap:4px;cursor:pointer;white-space:nowrap}
.toggle input{accent-color:var(--primary)}
select,input[type=range]{background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:6px;padding:4px 8px;font-size:11px}
.layout{flex:1;display:grid;grid-template-columns:1fr 280px;min-height:0}
.chart-stack{display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--border)}
#tv-chart-root{flex:1;min-height:200px}
.sub-pane{height:100px;border-top:1px solid var(--border)}
.side{background:#0a1020;padding:10px 12px;overflow-y:auto;font-size:11px;line-height:1.5;color:var(--muted)}
.side h3{font-size:12px;color:var(--primary);margin:8px 0 6px}
.side p{margin-bottom:8px}
.status{font-size:10px;color:#64748b;margin-left:6px}
.nav-links a{color:var(--muted);text-decoration:none;font-size:12px;margin-left:10px}
.nav-links a:hover{color:var(--primary)}
@media(max-width:900px){.layout{grid-template-columns:1fr}.side{display:none}}
</style>
</head>
<body>
<div class="header">
  <div class="brand">📊 Chart Pro <span style="font-weight:400;color:var(--muted);font-size:11px">Lightweight Charts · live engine strategies</span></div>
  <div class="nav-links"><a href="/">Dashboard</a><a href="/pipeline">Pipeline</a><a href="/classic#simulator">Simulator</a></div>
</div>
<div class="toolbar">
  <div class="tool-block"><span class="tool-label">Symbol</span><div id="sym-row" style="display:flex;flex-wrap:wrap;gap:4px"></div></div>
  <div class="tool-block"><span class="tool-label">TF</span><div id="tf-row" style="display:flex;flex-wrap:wrap;gap:4px"></div></div>
  <div class="tool-block">
    <span class="tool-label">Strategy</span>
    <select id="sel-strategy" style="max-width:220px"><option value="all">Loading…</option></select>
    <button class="pill-btn" id="btn-rescan" type="button">Rescan</button>
    <span class="status" id="scan-status"></span>
  </div>
  <div class="tool-block">
    <span class="tool-label">Filter</span>
    <select id="sel-dir"><option value="all">All dirs</option><option value="long">Long only</option><option value="short">Short only</option></select>
    <label class="toggle">Min conf <span id="lbl-conf">60%</span><input type="range" id="rng-conf" min="0.5" max="0.95" step="0.05" value="0.6"></label>
  </div>
  <div class="tool-block">
    <span class="tool-label">Overlays</span>
    <label class="toggle"><input type="checkbox" id="ov-history" checked> DB history (H:)</label>
    <label class="toggle"><input type="checkbox" id="ov-scan" checked> Strategy scan (S:)</label>
    <label class="toggle"><input type="checkbox" id="ov-tpsl" checked> Last TP/SL</label>
  </div>
  <div class="tool-block">
    <span class="tool-label">Indicators</span>
    <label class="toggle ind-toggle"><input type="checkbox" id="ind-ema20" checked> EMA20</label>
    <label class="toggle ind-toggle"><input type="checkbox" id="ind-ema50" checked> EMA50</label>
    <label class="toggle ind-toggle"><input type="checkbox" id="ind-ema200"> EMA200</label>
    <label class="toggle ind-toggle"><input type="checkbox" id="ind-bb" checked> BB</label>
    <label class="toggle ind-toggle"><input type="checkbox" id="ind-vwap"> VWAP</label>
    <label class="toggle ind-toggle"><input type="checkbox" id="ind-vol" checked disabled> Vol</label>
    <label class="toggle ind-toggle"><input type="checkbox" id="ind-rsi"> RSI</label>
    <label class="toggle ind-toggle"><input type="checkbox" id="ind-macd"> MACD</label>
    <button class="pill-btn" id="btn-fit" type="button">Fit</button>
    <button class="pill-btn" id="btn-log" type="button">Log scale</button>
    <span class="status" id="load-status"></span>
  </div>
</div>
<div id="chart-legend" style="padding:4px 12px;font-size:11px;color:var(--muted);border-bottom:1px solid var(--border)">—</div>
<div class="layout">
  <div class="chart-stack">
    <div id="tv-chart-root"></div>
    <div id="pane-rsi" class="sub-pane" style="display:none"></div>
    <div id="pane-macd" class="sub-pane" style="display:none"></div>
  </div>
  <aside class="side">
    <h3>How to read this chart</h3>
    <p><strong style="color:#10b981">H:</strong> markers from your live signal log (last 60 days). <strong style="color:#38bdf8">S:</strong> markers from replaying the selected strategy on visible bars (same code as the market simulator).</p>
    <p>TP/SL lines use the live swing book: <strong>3R target · 1R stop (3×ATR)</strong> on the most recent marker.</p>
    <h3>Strategies</h3>
    <p>Confluence, Bollinger squeeze, volume spike, RSI divergence, trend pullback, range breakout, liquidity sweep, breakout retest — or <em>All</em> (ensemble waterfall).</p>
    <h3>Shortcuts</h3>
    <p>Drag to pan · scroll to zoom · crosshair syncs RSI/MACD panes when enabled.</p>
  </aside>
</div>
<script src="/portal/static/chart-pro.js"></script>
</body>
</html>"""
    from scheduler.health import _THEME_SNIPPET
    return web.Response(text=html.replace("</head>", _THEME_SNIPPET + "</head>"), content_type="text/html")
