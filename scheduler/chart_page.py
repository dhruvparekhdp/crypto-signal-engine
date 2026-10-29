"""
Interactive TradingView-Style Multi-Timeframe Chart & Analytics Page.

Chart Pro (Lightweight Charts):
- Multi-TF candles from lake parquet + volume pane
- Indicators: EMA 20/50/200, Bollinger, VWAP, RSI, MACD
- Strategy filter: same dispatcher as market simulator (scan + DB history overlays)
- TP/SL from live swing book (3R / 1R) on latest marker
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from aiohttp import web
import pandas as pd

from analysis.chart_scan import scan_strategy_markers
from analysis.simulator.strategy_dispatcher import AVAILABLE_STRATEGIES
from storage.repository import Repository


OVERLAYS_FILE = Path("data/reports/chart_overlays.json")
PROGRESS_FILE = Path("data/reports/pipeline_progress.json")


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

    # 30 Oct 2026 review: never invent prices. The old fallback drew random candles (BTC near 65,000) and ran the
    # strategy scan on them as if they were real; no data now means an empty chart that says so.
    return []


def _q_int(request: web.Request, key: str, default: int) -> int:
    try:
        return int(request.query.get(key, default))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=json.dumps({"error": f"{key} must be a whole number"}),
                                 content_type="application/json") from None


def _q_float(request: web.Request, key: str, default: float) -> float:
    try:
        return float(request.query.get(key, default))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=json.dumps({"error": f"{key} must be a number"}),
                                 content_type="application/json") from None


async def api_chart_klines(runner, request: web.Request) -> web.Response:
    """Return historical candlestick bars for requested symbol and timeframe."""
    sym = request.query.get("symbol", "BTCUSDT").upper()
    tf = request.query.get("tf", "1h").lower()
    limit = _q_int(request, "limit", 600)
    bars = await asyncio.to_thread(_load_chart_bars, sym, tf, limit)      # parquet reads stay off the trading loop
    return web.Response(text=json.dumps(bars), content_type="application/json")


async def api_chart_strategies(runner, request: web.Request) -> web.Response:
    return web.Response(text=json.dumps(AVAILABLE_STRATEGIES), content_type="application/json")


async def api_chart_strategy_scan(runner, request: web.Request) -> web.Response:
    sym = request.query.get("symbol", "BTCUSDT").upper()
    tf = request.query.get("tf", "1h").lower()
    strategy = request.query.get("strategy", "all").lower()
    min_conf = _q_float(request, "min_confidence", 0.60)
    limit = _q_int(request, "limit", 800)
    t0 = time.perf_counter()
    bars = await asyncio.to_thread(_load_chart_bars, sym, tf, limit)
    # the replay is CPU work: run it in a thread so the trading jobs in this process are never held up
    markers = await asyncio.to_thread(scan_strategy_markers, bars, strategy=strategy, min_confidence=min_conf,
                                      step=2 if len(bars) > 400 else 1)
    return web.Response(
        text=json.dumps({"symbol": sym, "tf": tf, "strategy": strategy, "markers": markers, "elapsed_ms": int((time.perf_counter() - t0) * 1000)}),
        content_type="application/json",
    )


async def api_chart_indicator_catalog(runner, request: web.Request) -> web.Response:
    """Metadata for all client-side indicators (for mobile apps / external tools)."""
    # Catalog lives in JS; mirror counts/categories here for API consumers.
    catalog_path = Path(__file__).resolve().parent / "portal" / "chart-indicators.js"
    text = catalog_path.read_text(encoding="utf-8") if catalog_path.exists() else ""
    import re

    names = re.findall(r'name:\s*"([^"]+)"', text)
    # moving averages are generated: [periods].forEach(p => { REGISTRY.push(maLine("sma", ...)); ... }) and single
    # REGISTRY.push(maLine("wma", 20, ...)) lines (30 Oct 2026 review: the regex alone counted 22 of 59)
    loop = re.search(r"\[([\d,\s]+)\]\.forEach\(p => \{(.*?)\}\);", text, re.S)
    if loop:
        periods = [x for x in loop.group(1).split(",") if x.strip()]
        kinds = re.findall(r'maLine\("(\w+)", p', loop.group(2))
        names += [f"{k.upper()} {p.strip()}" for p in periods for k in kinds]
    names += [f"{k.upper()} {n}" for k, n in re.findall(r'REGISTRY\.push\(maLine\("(\w+)", (\d+)', text)]
    categories = sorted(set(re.findall(r'category:\s*"([^"]+)"', text)))
    return web.Response(
        text=json.dumps({"count": len(names), "categories": categories, "note": "Full catalog in chart-indicators.js + Indicators modal"}),
        content_type="application/json",
    )


async def api_chart_parity_doc(runner, request: web.Request) -> web.Response:
    p = Path("docs/CHART_TRADINGVIEW_PARITY.md")
    body = p.read_text(encoding="utf-8") if p.exists() else "# Parity doc missing"
    return web.Response(text=json.dumps({"markdown": body}), content_type="application/json")


async def api_chart_history_signals(runner, request: web.Request) -> web.Response:
    sym = request.query.get("symbol", "BTCUSDT").upper()
    days = _q_int(request, "days", 60)
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
.ind-chip{display:inline-flex;align-items:center;gap:4px;background:#1e293b;border:1px solid #334155;border-radius:999px;padding:2px 8px;margin:2px;font-size:10px;color:#e2e8f0}
.ind-chip button{background:none;border:none;color:#94a3b8;cursor:pointer;font-size:14px;line-height:1}
#ind-modal{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:200;padding:16px}
#ind-modal.open{display:flex}
.ind-panel{background:#0f172a;border:1px solid #334155;border-radius:12px;width:min(640px,96vw);max-height:85vh;display:flex;flex-direction:column}
.ind-panel-head{padding:12px 14px;border-bottom:1px solid #334155;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ind-panel-body{overflow:auto;padding:8px 12px 14px}
.ind-row{display:flex;align-items:center;gap:8px;padding:6px 4px;border-bottom:1px solid #1e293b;font-size:12px;cursor:pointer}
.ind-row:hover{background:#131d31}
.ind-cat{margin-left:auto;font-size:10px;color:#64748b}
.tv-partial{font-size:9px;color:#fbbf24;border:1px solid #92400e;border-radius:4px;padding:1px 4px;margin-left:4px}
#active-indicators{display:flex;flex-wrap:wrap;gap:2px;padding:4px 12px;border-bottom:1px solid var(--border);min-height:28px}
.draw-bar{display:flex;gap:4px;flex-wrap:wrap}
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
    <button class="pill-btn" id="btn-indicators" type="button">📈 Indicators (50+)</button>
    <button class="pill-btn" id="btn-fit" type="button">Fit</button>
    <button class="pill-btn" id="btn-log" type="button">Log scale</button>
    <span class="status" id="load-status"></span>
  </div>
  <div class="tool-block">
    <span class="tool-label">Draw</span>
    <div id="draw-tools" class="draw-bar"></div>
    <button class="pill-btn" id="btn-clear-draw" type="button">Clear drawings</button>
  </div>
</div>
<div id="active-indicators"></div>
<div id="chart-legend" style="padding:4px 12px;font-size:11px;color:var(--muted);border-bottom:1px solid var(--border)">—</div>
<div class="layout">
  <div id="chart-stack" class="chart-stack">
    <div id="tv-chart-root"></div>
  </div>
  <aside class="side">
    <h3>Indicators</h3>
    <p>Open <strong>Indicators (50+)</strong> — search SuperTrend, moving averages, MACD, etc. Active set is saved in your browser.</p>
    <h3>Drawings</h3>
    <p>Trend line, H/V line, ray, rectangle, Fib, text. Stored per symbol+timeframe. Double-click canvas to undo last shape (select tool).</p>
    <h3>TradingView parity</h3>
    <p><button type="button" class="pill-btn" id="btn-parity">What we have vs TV →</button></p>
    <p id="parity-blurb" style="font-size:10px"></p>
    <h3>Signals</h3>
    <p><strong>H:</strong> DB history · <strong>S:</strong> strategy rescan · TP/SL = 3R/1R on latest marker.</p>
  </aside>
</div>
<div id="ind-modal">
  <div class="ind-panel">
    <div class="ind-panel-head">
      <strong style="color:#38bdf8">Indicator catalog</strong>
      <input type="search" id="ind-search" placeholder="Search (e.g. SuperTrend, EMA…)" style="flex:1;min-width:140px">
      <select id="ind-cat"><option value="all">All</option></select>
      <button type="button" class="pill-btn" id="ind-modal-close">Done</button>
    </div>
    <div class="ind-panel-body" id="ind-list"></div>
  </div>
</div>
<div id="parity-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:210;padding:20px;overflow:auto">
  <div style="max-width:900px;margin:0 auto;background:#0f172a;border:1px solid #334155;border-radius:12px;padding:16px">
    <div style="display:flex;justify-content:space-between;margin-bottom:12px">
      <strong style="color:#38bdf8">TradingView parity matrix</strong>
      <button type="button" class="pill-btn" id="parity-modal-close">Close</button>
    </div>
    <pre id="parity-md" style="white-space:pre-wrap;font-size:11px;color:#cbd5e1;line-height:1.5"></pre>
  </div>
</div>
<script src="/portal/static/chart-indicators.js"></script>
<script src="/portal/static/chart-drawings.js"></script>
<script src="/portal/static/chart-pro.js"></script>
<script>
document.getElementById('btn-parity')?.addEventListener('click', async () => {
  const res = await fetch('/api/chart/parity');
  const d = await res.json();
  document.getElementById('parity-md').textContent = d.markdown || '';
  document.getElementById('parity-modal').style.display = 'block';
});
document.getElementById('parity-modal-close')?.addEventListener('click', () => {
  document.getElementById('parity-modal').style.display = 'none';
});
fetch('/api/chart/indicator-catalog').then(r=>r.json()).then(d=>{
  const el = document.getElementById('parity-blurb');
  if(el) el.textContent = d.count + ' indicators · ' + (d.categories||[]).length + ' categories. Full TV library needs commercial widget — see matrix.';
});
</script>
</body>
</html>"""
    from scheduler.health import _THEME_SNIPPET
    return web.Response(text=html.replace("</head>", _THEME_SNIPPET + "</head>"), content_type="text/html")
