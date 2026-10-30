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


def _lake_bars(sym: str, tf: str, limit: int) -> list[dict]:
    """Older history from lake parquet only (W19 #7)."""
    sym = sym.upper()
    tf = tf.lower()
    lake_5m = Path(f"data/lake/um/klines/{sym}/5m")
    if not lake_5m.exists() or not list(lake_5m.glob("*.parquet")):
        return []
    try:
        parquets = sorted(list(lake_5m.glob("*.parquet")))[-12:]
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
                "open": round(float(r["open"]), 6),
                "high": round(float(r["high"]), 6),
                "low": round(float(r["low"]), 6),
                "close": round(float(r["close"]), 6),
                "volume": round(float(r["volume"]), 2),
            }
            for ts, r in tail_df.iterrows()
        ]
    except Exception:
        return []


def _merge_bars(lake: list[dict], recent: list[dict], limit: int) -> list[dict]:
    by_time: dict[int, dict] = {int(b["time"]): b for b in lake}
    for b in recent:
        by_time[int(b["time"])] = b
    return sorted(by_time.values(), key=lambda x: int(x["time"]))[-limit:]


async def _load_chart_bars_async(runner, sym: str, tf: str, limit: int = 600) -> list[dict]:
    """Recent candles from Delta (collectors/delta_market.fetch_candles); lake for older bars."""
    sym_u = sym.upper()
    tf_l = tf.lower()
    limit = max(50, min(int(limit), 2000))
    recent: list[dict] = []

    from collectors.delta_market import TF_TO_RES

    res = TF_TO_RES.get(tf_l)
    dm = getattr(runner, "delta_market", None) if runner is not None else None
    if dm is not None and res is not None and callable(getattr(dm, "fetch_candles", None)):
        try:
            raw = await dm.fetch_candles(sym_u, res, limit=min(limit, 2000))
        except TypeError:
            raw = []
        except Exception:
            raw = []
        recent = [
            {
                "time": int(b["timestamp"].timestamp()),
                "open": round(float(b["open"]), 6),
                "high": round(float(b["high"]), 6),
                "low": round(float(b["low"]), 6),
                "close": round(float(b["close"]), 6),
                "volume": round(float(b.get("volume") or 0.0), 2),
            }
            for b in raw
        ]

    if len(recent) >= limit:
        return recent[-limit:]

    lake = await asyncio.to_thread(_lake_bars, sym_u, tf_l, limit)
    return _merge_bars(lake, recent, limit)


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
    bars = await _load_chart_bars_async(runner, sym, tf, limit)
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
    bars = await _load_chart_bars_async(runner, sym, tf, limit)
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
    from scheduler.health import _THEME_SNIPPET
    from scheduler.portal import asset_url, command_tool_head_extra

    ci = asset_url("chart-indicators.js")
    cd = asset_url("chart-drawings.js")
    cp = asset_url("chart-pro.js")
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chart — Command</title>
<style>
.ind-chip{display:inline-flex;align-items:center;gap:4px;border:2px solid var(--line2);padding:2px 8px;margin:2px;font:11px var(--mono)}
.ind-chip button{background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px}
#ind-modal{position:fixed;inset:0;background:rgba(0,0,0,.75);display:none;align-items:center;justify-content:center;z-index:200;padding:16px}
#ind-modal.open{display:flex}
.ind-panel{background:var(--panel);border:3px solid var(--line);box-shadow:8px 8px 0 var(--acc);width:min(640px,96vw);max-height:85vh;display:flex;flex-direction:column}
.ind-panel-head{padding:12px 14px;border-bottom:2px solid var(--line2);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ind-panel-body{overflow:auto;padding:8px 12px 14px;max-height:60vh}
.ind-row{display:flex;align-items:center;gap:8px;padding:8px 4px;border-bottom:1px solid var(--line2);font:12px var(--mono);cursor:pointer;min-height:44px}
.ind-cat{margin-left:auto;font-size:10px;color:var(--dim)}
.tv-partial{font-size:9px;color:var(--dn);border:2px solid var(--line2);padding:1px 4px;margin-left:4px}
#chart-empty{display:none;position:absolute;inset:0;align-items:center;justify-content:center;padding:24px;text-align:center;z-index:2;pointer-events:none}
#chart-empty.on{display:flex}
.status{font:10px var(--mono);color:var(--dim);margin-left:6px}
#parity-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:210;padding:16px;overflow:auto}
#parity-modal.open{display:block}
</style>
</head>
<body class="cmd-tool cmd-tool-chart">
<div class="wrap">
  <div class="tool-head">
    <h2>Chart</h2>
    <span class="dim" id="load-status">Loading…</span>
  </div>
  <div class="flag-research" role="note">Research: strategy-scan markers (S:) replay intraday detectors on visible bars — no proven edge. Live swing signals use the Book, not this scan.</div>
  <div class="panel chart-panel">
<div class="chart-toolbar-wrap" id="chart-toolbar-wrap">
  <button type="button" class="pill-btn chart-tools-toggle" id="btn-tools-toggle" aria-expanded="false" aria-controls="chart-toolbar">Tools</button>
<div class="chart-toolbar" id="chart-toolbar">
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
    <label class="toggle"><input type="checkbox" id="ov-scan" checked> S: research scan (intraday detectors, no proven edge)</label>
    <label class="toggle"><input type="checkbox" id="ov-tpsl" checked> Last TP/SL</label>
  </div>
  <div class="tool-block">
    <button class="pill-btn" id="btn-indicators" type="button">📈 Indicators (50+)</button>
    <button class="pill-btn" id="btn-fit" type="button">Fit</button>
    <button class="pill-btn" id="btn-log" type="button">Log scale</button>
  </div>
  <div class="tool-block">
    <span class="tool-label">Draw</span>
    <div id="draw-tools" class="draw-bar"></div>
    <button class="pill-btn" id="btn-clear-draw" type="button">Clear drawings</button>
  </div>
</div>
<div id="active-indicators" class="chart-tools-extra"></div>
</div>
<div class="chart-legend" id="chart-legend">—</div>
<div class="chart-layout">
  <div id="chart-stack" class="chart-stack">
    <div id="chart-empty" class="empty-state">No candle data for this symbol and timeframe. Delta India did not return recent bars and the local lake has no history to show.</div>
    <div id="tv-chart-root"></div>
  </div>
  <aside class="side-panel panel-body">
    <div class="panel-title">Indicators &amp; drawings</div>
    <p>Open <strong>Indicators (50+)</strong> in the toolbar. Drawings save per symbol+TF in this browser.</p>
    <p><button type="button" class="pill-btn" id="btn-parity">TradingView parity matrix</button></p>
    <p id="parity-blurb" class="dim tiny"></p>
    <p class="tiny"><strong>H:</strong> signal log · <strong>S:</strong> research scan only</p>
  </aside>
</div>
  </div>
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
<div id="parity-modal">
  <div class="panel" style="max-width:900px;margin:16px auto;padding:16px">
    <div class="tool-head" style="margin-bottom:12px">
      <h2 style="font-size:16px">TradingView parity</h2>
      <button type="button" class="pill-btn" id="parity-modal-close">Close</button>
    </div>
    <pre id="parity-md" class="mono tiny" style="white-space:pre-wrap;line-height:1.5;color:var(--ink)"></pre>
  </div>
</div>
<script src="__CHART_INDICATORS__"></script>
<script src="__CHART_DRAWINGS__"></script>
<script src="__CHART_PRO__"></script>
<script>
document.getElementById('btn-parity')?.addEventListener('click', async () => {
  const res = await fetch('/api/chart/parity');
  const d = await res.json();
  document.getElementById('parity-md').textContent = d.markdown || '';
  document.getElementById('parity-modal').classList.add('open');
});
document.getElementById('parity-modal-close')?.addEventListener('click', () => {
  document.getElementById('parity-modal').classList.remove('open');
});
fetch('/api/chart/indicator-catalog').then(r=>r.json()).then(d=>{
  const el = document.getElementById('parity-blurb');
  if(el) el.textContent = d.count + ' indicators · ' + (d.categories||[]).length + ' categories.';
}).catch(()=>{});
(function(){
  const wrap = document.getElementById('chart-toolbar-wrap');
  const btn = document.getElementById('btn-tools-toggle');
  if(!wrap || !btn) return;
  btn.addEventListener('click', () => {
    const open = wrap.classList.toggle('tools-open');
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    btn.textContent = open ? 'Hide tools' : 'Tools';
  });
})();
</script>
</body>
</html>"""
    html = html.replace("__CHART_INDICATORS__", ci).replace("__CHART_DRAWINGS__", cd).replace("__CHART_PRO__", cp)
    lw = "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"
    sri = "sha384-OK7vELvjHdhUFi31JYioPIcRHTROLdcDa6ZsNWgvgLaKj+9JqhU0Ad8g4wz3CXjA"
    head = (
        _THEME_SNIPPET
        + command_tool_head_extra()
        + f'<script src="{lw}" integrity="{sri}" crossorigin="anonymous"></script>'
    )
    return web.Response(text=html.replace("</head>", head + "</head>"), content_type="text/html")
