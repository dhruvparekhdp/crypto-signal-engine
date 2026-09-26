"""
Two pages for the v2 work: live shadow results, and the owner's trade journal.

  /v2            v2 setups found live, how each resolved, stats per setup
                 against the promotion gates (GET /api/v2/shadow)
  /journal       log a manual trade, or a setup looked at and skipped; the
                 chart at that moment is measured automatically from Binance
                 (GET/POST /api/journal, admin session required)

Registered from scheduler.health; health's helpers are imported lazily to
avoid a circular import.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from aiohttp import web

JOURNAL_TAGS = ("hh_hl_pullback", "sweep_pdl_pdh", "range_fade", "breakout_retest",
                "rejection_wick", "momentum", "news", "other")


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


async def api_v2_shadow(request: web.Request) -> web.Response:
    from analysis.v2_backtest import grade
    from scheduler.health import _iso
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    try:
        days = max(1.0, min(float(request.query.get("days", "30")), 365.0))
    except ValueError:
        days = 30.0
    async with AsyncSessionFactory() as session:
        rows = await Repository(session).recent_v2_shadows(days)

    class _T:  # grade() reads .r, .setup and .filled_at
        def __init__(self, r):
            self.r, self.setup = r.r, r.setup
            self.filled_at = str(r.filled_at or r.decided_at)

    closed = [r for r in rows if r.status == "closed"]
    by_setup = {}
    for code in sorted({r.setup for r in closed}):
        g = grade([_T(r) for r in closed if r.setup == code], mc=False)
        by_setup[code] = {"stats": g["stats"], "gates": g["gates"]}
    # No Monte Carlo here: this endpoint is public and runs on the engine's
    # event loop, where 10,000 simulated paths per request stalled the tick.
    overall = grade([_T(r) for r in closed], mc=False) if closed else None
    body = {
        "days": days,
        "counts": {s: sum(1 for r in rows if r.status == s)
                   for s in ("pending", "open", "closed", "cancelled", "expired")},
        "overall": ({"stats": overall["stats"], "gates": overall["gates"]}
                    if overall else None),
        "by_setup": by_setup,
        "rows": [{"symbol": r.symbol, "setup": r.setup, "side": r.side,
                  "decided_at": _iso(r.decided_at), "entry": r.entry, "stop": r.stop,
                  "target": r.target, "status": r.status, "reason": r.reason, "r": r.r,
                  "exit_at": _iso(r.exit_at)} for r in rows[:300]],
    }
    return web.Response(text=json.dumps(body), content_type="application/json")


def backtest_api(runner):
    """GET /api/v2/backtest (latest report) and POST .../run (admin: run it now)."""
    async def get(request: web.Request) -> web.Response:
        from pathlib import Path

        from config.settings import settings
        path = Path(settings.v2_reports_dir, "backtest_v2_latest.json")
        state = dict(getattr(runner, "_v2_bt_state", {}) or {})
        if not path.exists():
            return web.json_response({"report": None, "state": state})
        report = json.loads(path.read_text())
        trades = report.pop("trades", [])
        report["recent_trades"] = trades[-60:]
        for g in [report.get("overall", {}), *report.get("setups", {}).values()]:
            if isinstance(g, dict):
                g.pop("windows", None)
        return web.json_response({"report": report, "state": state})

    async def run(request: web.Request) -> web.Response:
        import asyncio
        denied = await _admin(request)
        if denied is not None:
            return denied
        if (getattr(runner, "_v2_bt_state", {}) or {}).get("running"):
            return web.json_response({"ok": False, "error": "already running"}, status=409)
        asyncio.create_task(runner._v2_backtest_job())
        return web.json_response({"ok": True, "started": True})

    return get, run


async def _admin(request: web.Request):
    """None when allowed; otherwise the 401 to return."""
    from scheduler.health import _SETTINGS, _verify_admin_session
    from scheduler.security import check_bearer_auth
    if await _verify_admin_session(request):
        return None
    return check_bearer_auth(request, _SETTINGS.api_auth_token)


async def journal_features(symbol: str, at: datetime) -> dict:
    """The chart as it stood at `at`: the same facts the history review measures."""
    import pandas as pd

    from analysis.history_review import facts_before
    from collectors.v2_feed import fetch_frames
    try:
        frames = await fetch_frames(symbol, end=at, intervals=("15m", "1h"))
        return facts_before(pd.Timestamp(_naive(at)), frames["15m"], frames["1h"])
    except Exception as exc:
        return {"error": str(exc)[:120]}


def _float(v, default=0.0) -> float:
    import math
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _when(v) -> datetime | None:
    if not v:
        return None
    try:
        return _naive(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
    except ValueError:
        return None


async def api_journal(request: web.Request) -> web.Response:
    from scheduler.health import _iso
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    denied = await _admin(request)
    if denied is not None:
        return denied
    if request.method == "POST":
        try:
            d = await request.json()
        except Exception:
            return web.json_response({"error": "send JSON"}, status=400)
        symbol = str(d.get("symbol", "")).strip().lower()
        side = str(d.get("side", "")).strip().lower()
        opened = _when(d.get("opened_at"))
        if not symbol.endswith("usdt") or side not in ("long", "short") or opened is None:
            return web.json_response(
                {"error": "symbol (e.g. solusdt), side (long/short) and opened_at are needed"},
                status=400)
        tags = [t for t in str(d.get("tags", "")).split(",") if t.strip() in JOURNAL_TAGS]
        feats = await journal_features(symbol, opened)
        async with AsyncSessionFactory() as session:
            new_id = await Repository(session).add_journal_entry(
                symbol=symbol, side=side, took=bool(d.get("took", True)), opened_at=opened,
                closed_at=_when(d.get("closed_at")), entry=_float(d.get("entry")),
                stop=_float(d.get("stop")), target=_float(d.get("target")),
                exit_price=_float(d.get("exit_price")), leverage=_float(d.get("leverage")),
                tags=",".join(t.strip() for t in tags),
                conviction=max(1, min(5, int(_float(d.get("conviction"), 3)))),
                note=str(d.get("note", ""))[:2000], features=json.dumps(feats))
        return web.json_response({"ok": True, "id": new_id, "features": feats})
    async with AsyncSessionFactory() as session:
        rows = await Repository(session).journal_entries()
    return web.json_response({"tags": JOURNAL_TAGS, "rows": [{
        "id": r.id, "symbol": r.symbol, "side": r.side, "took": r.took,
        "opened_at": _iso(r.opened_at), "closed_at": _iso(r.closed_at), "entry": r.entry,
        "stop": r.stop, "target": r.target, "exit_price": r.exit_price, "leverage": r.leverage,
        "tags": r.tags, "conviction": r.conviction, "note": r.note,
        "features": json.loads(r.features or "{}")} for r in rows]})


_STYLE = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);
  color:var(--text);font-size:14px;line-height:1.5;padding-bottom:60px}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:11px 16px;display:flex;
  gap:10px;align-items:center;flex-wrap:wrap;position:sticky;top:0}
header h1{font-size:16px;color:var(--text-strong)}
a.nav-btn,button.nav-btn{background:var(--acc-t);border:1px solid var(--acc-t2);color:var(--accent);
  font-size:12px;font-weight:600;padding:6px 12px;border-radius:8px;text-decoration:none;
  cursor:pointer;font-family:inherit}
main{padding:16px;max-width:1100px;margin:0 auto;display:grid;gap:16px;grid-template-columns:minmax(0,1fr)}
.card{min-width:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px}
h2{font-size:15px;color:var(--text-strong);margin-bottom:10px}
.muted{color:var(--muted)} .pos{color:var(--pos)} .neg{color:var(--neg)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.stat{background:var(--panel2);border:1px solid var(--line2);border-radius:10px;padding:10px}
.stat b{display:block;font-size:18px;color:var(--text-strong)}
.tw{overflow-x:auto;max-width:100%;-webkit-overflow-scrolling:touch} table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line2);text-align:left;white-space:nowrap}
th{color:var(--muted2);font-weight:600}
.ok{color:var(--pos);font-weight:700} .no{color:var(--neg);font-weight:700}
form{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
label{font-size:12px;color:var(--muted);display:grid;gap:4px}
input,select,textarea{background:var(--panel2);border:1px solid var(--line);color:var(--text);
  border-radius:8px;padding:7px 9px;font:inherit}
.wide{grid-column:1/-1}
.tags{display:flex;flex-wrap:wrap;gap:6px}
.tags label{display:flex;gap:4px;align-items:center;background:var(--panel2);
  border:1px solid var(--line2);border-radius:8px;padding:4px 8px}
@media (max-width:600px){main{padding:12px}}
"""

_ESC = ("function esc(s){return String(s==null?'':s).replace(/[&<>\"']/g,c=>({'&':'&amp;',"
        "'<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));}")

V2_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>v2 Shadow</title>
<style>""" + _STYLE + """</style></head><body>
<header><h1>v2 setups · shadow</h1><span class="muted" id="sub"></span>
<span style="margin-left:auto"></span><a class="nav-btn" href="/">← Dashboard</a>
<a class="nav-btn" href="/journal">Journal</a></header>
<main id="main"><div class="card muted">Loading…</div></main>
<script>""" + _ESC + """
const NAMES={A:'Trend pullback',B:'Sweep & reclaim',C:'Range fade',D:'Breakout-retest'};
function gates(g){return Object.entries(g||{}).map(([k,v])=>'<span class="'+(v?'ok':'no')+'">'
  +esc(k)+(v?' ✓':' ✗')+'</span>').join(' · ');}
// No losing trade yet: the profit factor is infinite, not missing.
function pf(s){ return s.profit_factor ?? (s.trades && s.avg_loss_r===0 ? '∞' : '—'); }
// Below 30 trades the gates say nothing (one window "positive", a PF of
// infinity): show progress towards the sample instead of green ticks.
function statCard(title,s,g){ if(!s||!s.trades) return '<div class="stat"><span class="muted">'
  +esc(title)+'</span><b>—</b><span class="muted">no closed trades</span></div>';
  const early = s.trades < 30;
  return '<div class="stat"><span class="muted">'+esc(title)+'</span><b class="'
  +(s.expectancy_r>=0?'pos':'neg')+'">'+(s.expectancy_r>=0?'+':'')+s.expectancy_r+'R</b>'
  +'<span class="muted">'+s.trades+' trades · win '+Math.round(s.win_rate*100)+'% · PF '
  +pf(s)+'</span><div style="font-size:11px;margin-top:4px">'
  +(early?'<span class="muted">Too few trades to judge: '+s.trades+' of 200 needed</span>':gates(g))
  +'</div></div>'; }
function row(label,g){ const s=(g||{}).stats||{}; if(!s.trades) return '<tr><td>'+esc(label)
  +'</td><td colspan="7" class="muted">no trades</td></tr>';
  return '<tr><td>'+esc(label)+'</td><td>'+s.trades+'</td><td>'+Math.round(s.win_rate*100)+'%</td>'
  +'<td class="'+(s.expectancy_r>=0?'pos':'neg')+'">'
  +(s.expectancy_r>=0?'+':'')+s.expectancy_r+'R</td>'
  +'<td>+'+s.avg_win_r+'R / '+s.avg_loss_r+'R</td><td>'+pf(s)+'</td>'
  +'<td>'+Math.round((g.positive_windows||0)*100)+'%</td><td>'
  +(g.promote_to_paper?'<span class="ok">PROMOTE</span>':'<span class="muted">hold</span>')
  +'</td></tr>'; }
async function loadBacktest(){
  const d=await (await fetch('/api/v2/backtest')).json(); const r=d.report, st=d.state||{};
  const busy=st.running?' · <b>running now ('+esc(st.stage||'')+')</b>':'';
  if(!r) return '<section class="card"><h2>Backtest on real Binance data</h2><p class="muted">'
    +(st.error?'Last run failed: '+esc(st.error):'The server downloads 2 years of data and '
    +'runs the test a few minutes after deploy, then daily at 07:47 IST.')+busy
    +'</p><button class="nav-btn" onclick="runBt()">Run now (admin)</button></section>';
  let h='<section class="card"><h2>Backtest on real Binance data</h2><p class="muted" '
    +'style="font-size:12px;margin-bottom:10px">'+esc(r.window[0])+' → '+esc(r.window[1])+' · '
    +esc(r.symbols.join(', '))+' · all fees, GST, slippage and funding included · run '
    +esc(fmtWhen(r.generated))+busy+'</p>'
    +'<div class="tw"><table><tr><th></th><th>Trades</th><th>Win</th><th>Per trade</th>'
    +'<th>Avg win / loss</th><th>PF</th><th>Windows up</th><th>Gate</th></tr>'
    +row('All setups',r.overall);
  Object.entries(r.setups).forEach(([k,g])=>{h+=row(k+' · '+NAMES[k],g);});
  Object.entries(r.by_side||{}).forEach(([k,g])=>{h+=row(k+'s',g);});
  Object.entries(r.by_setup_side||{}).forEach(([k,g])=>{ if((g.stats||{}).trades)
    h+=row(k.replace('_',' '),g);});
  Object.entries(r.by_symbol||{}).forEach(([k,g])=>{h+=row(k,g);});
  h+='</table></div>';
  const vs=r.variants||{};
  if(Object.keys(vs).length){
    h+='<h2 style="margin-top:16px">Same signals, different execution</h2>'
      +'<p class="muted" style="font-size:12px;margin-bottom:8px">What the limit entry costs '
      +'versus a market entry, and what breakeven or half-profit exits do to win rate AND '
      +'profit per trade. Deflated Sharpe accounts for trying all four.</p>'
      +'<div class="tw"><table><tr><th></th><th>Trades</th><th>Win</th><th>Per trade</th>'
      +'<th>Avg win / loss</th><th>PF</th><th>Windows up</th><th>Gate</th></tr>';
    Object.values(vs).forEach(v=>{h+=row(v.label,v.overall);});
    h+='</table></div>'; }
  const coins=r.coins||{};
  if(Object.keys(coins).length){
    const ex=['base','owner_tight','owner_medium'];
    const cell=st=>st&&st.trades?'<td class="'+(st.expectancy_r>=0?'pos':'neg')+'">'
      +(st.expectancy_r>=0?'+':'')+st.expectancy_r+'R<br><span class="muted">'
      +Math.round(st.win_rate*100)+'% · '+st.trades+'</span></td>':'<td class="muted">—</td>';
    h+='<h2 style="margin-top:16px">Which coins suit your profit lock</h2>'
      +'<p class="muted" style="font-size:12px;margin-bottom:8px">Per coin: profit per trade, '
      +'win rate and trades for the normal exit and your two lock styles. Steady / volatile is '
      +'relative to the rest of the watchlist (median 15m candle range).</p>'
      +'<div class="tw"><table><tr><th>Coin</th><th>Moves</th><th>Normal exit</th>'
      +'<th>Your style (tight)</th><th>Your style (looser)</th></tr>';
    Object.entries(coins).sort((a,b)=>a[1].median_15m_range_pct-b[1].median_15m_range_pct)
      .forEach(([sym,c])=>{h+='<tr><td>'+esc(sym)+'</td><td>'+esc(c.class)+'<br><span class="muted">'
        +c.median_15m_range_pct+'% / 15m</span></td>'+ex.map(n=>cell((c.exits||{})[n])).join('')+'</tr>';});
    h+='</table></div>'; }
  const sv=r.setup_variants||{};
  if(Object.keys(sv).length){
    h+='<h2 style="margin-top:16px">Research filters, one at a time</h2>'
      +'<p class="muted" style="font-size:12px;margin-bottom:8px">Each row changes one rule from '
      +'the research and re-runs everything. A filter is worth keeping only if profit per '
      +'trade rises, not just win rate.</p>'
      +'<div class="tw"><table><tr><th></th><th>Trades</th><th>Win</th><th>Per trade</th>'
      +'<th>Avg win / loss</th><th>PF</th><th>Windows up</th><th>Gate</th></tr>'
      +row('Base (for comparison)',r.overall);
    Object.values(sv).forEach(v=>{h+=row(v.label,v.overall);});
    h+='</table></div>'; }
  const bm=r.benchmarks||{};
  if(Object.keys(bm).length){
    h+='<h2 style="margin-top:16px">Freqtrade community strategies, same data and costs</h2>'
      +'<p class="muted" style="font-size:12px;margin-bottom:8px">Published rules from '
      +'freqtrade-strategies, long-only at 1x, per trade after fees. The bar v2 has to clear.</p>'
      +'<div class="tw"><table><tr><th>Strategy</th><th>TF</th><th>Trades</th><th>Win</th>'
      +'<th>Per trade</th><th>Avg win / loss</th><th>PF</th><th>Compounded</th></tr>';
    Object.entries(bm).forEach(([k,b])=>{ h+='<tr><td>'+esc(k)+'</td><td>'+esc(b.timeframe)+'</td>'
      +(b.trades?'<td>'+b.trades+'</td><td>'+Math.round(b.win_rate*100)+'%</td><td class="'
      +(b.avg_pct>=0?'pos':'neg')+'">'+(b.avg_pct>=0?'+':'')+b.avg_pct+'%</td><td>+'
      +b.avg_win_pct+'% / '+b.avg_loss_pct+'%</td><td>'+(b.profit_factor??'—')+'</td><td>'
      +(b.compounded_pct>=0?'+':'')+b.compounded_pct+'%</td>'
      :'<td colspan="6" class="muted">no trades</td>')+'</tr>'; });
    h+='</table></div>'; }
  h+='<p style="margin-top:10px"><button class="nav-btn" onclick="runBt()">'
    +'Run again now (admin)</button></p></section>';
  return h; }
async function runBt(){ const r=await fetch('/api/v2/backtest/run',{method:'POST',headers:{'X-Settings-Token':sessionStorage.getItem('settings_token')||''}});
  alert(r.ok?'Started. Download plus test takes a few minutes; this page refreshes.'
    :(r.status===409?'Already running.':'Log in at /settings in this tab first.')); }
async function load(){
  const bt=await loadBacktest().catch(()=>'');
  const d=await (await fetch('/api/v2/shadow?days=30')).json();
  const c=d.counts; document.getElementById('sub').textContent=
    c.closed+' closed · '+c.open+' open · '+c.pending+' resting · '
    +c.cancelled+' unfilled (30 days)';
  let h=bt+'<section class="card"><h2>Live shadow · expectancy after all costs, in R</h2>'
    +'<div class="grid">'
    +statCard('All setups',d.overall&&d.overall.stats,d.overall&&d.overall.gates);
  ['A','B','C','D'].forEach(k=>{const x=d.by_setup[k]||{};
    h+=statCard(k+' · '+NAMES[k],x.stats,x.gates);});
  h+='</div><p class="muted" style="margin-top:10px;font-size:12px">Shadow only — nothing is '
    +'traded. Same setups and fill rules as scripts/backtest_v2.py, so live and backtest '
    +'numbers compare directly. Gates to paper: 200+ trades, +0.15R, PF 1.3, '
    +'60% of windows up.</p></section>';
  h+='<section class="card"><h2>Signals</h2><div class="tw"><table><tr><th>Time (IST)</th>'
    +'<th>Coin</th><th>Setup</th><th>Side</th><th>Entry</th><th>Stop</th><th>Target</th>'
    +'<th>Status</th><th>Result</th></tr>';
  d.rows.forEach(r=>{h+='<tr><td>'+esc(fmtStamp(r.decided_at))+'<br><span class="muted" data-ago="'+esc(r.decided_at)+'">'+esc(fmtAgo(r.decided_at))+'</span>'
    +'</td><td>'+esc(r.symbol.toUpperCase())+'</td><td>'+esc(r.setup+' '+NAMES[r.setup])
    +'</td><td class="'+(r.side==='long'?'pos':'neg')+'">'+esc(r.side)+'</td><td>'
    +r.entry.toPrecision(6)
    +'</td><td>'+r.stop.toPrecision(6)+'</td><td>'+r.target.toPrecision(6)+'</td><td>'
    +esc(r.status)+(r.reason?' · '+esc(r.reason):'')+'</td><td class="'+(r.r>=0?'pos':'neg')+'">'
    +(r.status==='closed'?(r.r>=0?'+':'')+r.r.toFixed(2)+'R':'')+'</td></tr>';});
  if(!d.rows.length) h+='<tr><td colspan="9" class="muted">No v2 signals yet. They appear on '
    +'weekdays '+istHour(7)+'–'+istHour(17)+' IST when a setup passes every filter.</td></tr>';
  document.getElementById('main').innerHTML=h+'</table></div></section>';
}
load(); setInterval(load,60000);
</script></body></html>"""

JOURNAL_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Trade Journal</title>
<style>""" + _STYLE + """</style></head><body>
<header><h1>Trade journal</h1><span class="muted">your trades teach the engine</span>
<span style="margin-left:auto"></span><a class="nav-btn" href="/">← Dashboard</a>
<a class="nav-btn" href="/v2">v2 Shadow</a></header>
<main>
<section class="card"><h2>Log a trade — or a setup you looked at and skipped</h2>
<form id="f">
<label>Coin<input name="symbol" placeholder="solusdt" required></label>
<label>Side<select name="side"><option>long</option><option>short</option></select></label>
<label>Took it?<select name="took"><option value="1">Yes, traded</option>
  <option value="0">No, skipped</option></select></label>
<label>Opened (your local time)<input name="opened_at" type="datetime-local" required></label>
<label>Closed<input name="closed_at" type="datetime-local"></label>
<label>Entry<input name="entry" type="number" step="any"></label>
<label>Stop<input name="stop" type="number" step="any"></label>
<label>Target<input name="target" type="number" step="any"></label>
<label>Exit<input name="exit_price" type="number" step="any"></label>
<label>Leverage<input name="leverage" type="number" step="any" placeholder="30"></label>
<label>Conviction 1–5<input name="conviction" type="number" min="1" max="5" value="3"></label>
<div class="wide"><span class="muted" style="font-size:12px">Why (pick all that fit)</span>
<div class="tags" id="tags"></div></div>
<label class="wide">What you saw on the chart<textarea name="note" rows="3"
  placeholder="rejected 3 times at the 15m high, then a red engulfing"></textarea></label>
<div class="wide"><button class="nav-btn" type="submit">Save</button>
<span id="msg" class="muted" style="margin-left:10px"></span></div>
</form></section>
<section class="card"><h2>Entries</h2><div class="tw" id="list">Loading…</div></section>
</main>
<script>""" + _ESC + """
const TAGS=""" + json.dumps(list(JOURNAL_TAGS)) + """;
document.getElementById('tags').innerHTML=TAGS.map(t=>'<label><input type="checkbox" value="'
  +t+'">'+t.replace(/_/g,' ')+'</label>').join('');
function iso(v){return v?new Date(v).toISOString():null;}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();
  const f=new FormData(e.target), msg=document.getElementById('msg');
  const body=Object.fromEntries(f.entries());
  body.took=body.took==='1'; body.opened_at=iso(body.opened_at); body.closed_at=iso(body.closed_at);
  body.tags=[...document.querySelectorAll('#tags input:checked')].map(x=>x.value).join(',');
  msg.textContent='Saving and reading the chart at that time…';
  const r=await fetch('/api/journal',{method:'POST',headers:{'Content-Type':'application/json','X-Settings-Token':sessionStorage.getItem('settings_token')||''},
    body:JSON.stringify(body)});
  const d=await r.json();
  msg.textContent=r.ok?'Saved #'+d.id+' with '+Object.keys(d.features||{}).length+' chart facts'
    :(d.error||'Not saved — log in at /settings in this tab first');
  if(r.ok){e.target.reset(); list();}};
async function list(){
  const r=await fetch('/api/journal',{headers:{'X-Settings-Token':sessionStorage.getItem('settings_token')||''}}); const el=document.getElementById('list');
  if(!r.ok){el.innerHTML='<span class="muted">Log in as admin to see the journal.</span>';return;}
  const d=await r.json();
  if(!d.rows.length){el.innerHTML='<span class="muted">Nothing yet. Twenty trades with a line '
    +'of why each is enough to check the rules against how you trade.</span>';return;}
  el.innerHTML='<table><tr><th>Opened (IST)</th><th>Coin</th><th>Side</th><th>Took</th>'
    +'<th>Entry → Exit</th><th>Tags</th><th>15m structure</th><th>Note</th></tr>'
    +d.rows.map(r=>'<tr><td>'+esc(fmtWhen(r.opened_at))+'</td><td>'
    +esc(r.symbol.toUpperCase())+'</td><td class="'+(r.side==='long'?'pos':'neg')+'">'+esc(r.side)
    +'</td><td>'+(r.took?'yes':'skipped')+'</td><td>'+(r.entry||'')+' → '+(r.exit_price||'')
    +'</td><td>'+esc(r.tags)+'</td><td>'+esc((r.features||{}).structure_15m_12h||'')
    +'</td><td style="white-space:normal;min-width:200px">'+esc(r.note)+'</td></tr>').join('')
    +'</table>';}
list();
</script></body></html>"""


async def v2_page(request: web.Request) -> web.Response:
    return web.Response(text=_themed(V2_HTML), content_type="text/html")


async def journal_page(request: web.Request) -> web.Response:
    return web.Response(text=_themed(JOURNAL_HTML), content_type="text/html")


def _themed(html: str) -> str:
    from scheduler.health import _THEME_SNIPPET
    return html.replace("</head>", _THEME_SNIPPET + "</head>")


def register(app: web.Application, runner=None) -> None:
    get, run = backtest_api(runner)
    app.router.add_get("/api/v2/backtest", get)
    app.router.add_post("/api/v2/backtest/run", run)
    app.router.add_get("/v2", v2_page)
    app.router.add_get("/journal", journal_page)
    app.router.add_get("/api/v2/shadow", api_v2_shadow)
    app.router.add_get("/api/journal", api_journal)
    app.router.add_post("/api/journal", api_journal)
