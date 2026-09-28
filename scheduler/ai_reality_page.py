"""
"AI vs Reality" — one page for /api/reviews's other half.

/api/reviews already shows what the Groq Sentinel said. This adds what
happened next, from two sources that already exist and are reused here
rather than recomputed:

  * the market's own answer — analysis/signal_audit.py's resolution of the
    signal's own target/stop against real candles, independent of whether a
    paper trade ever opened;
  * the paper trade's answer, if one opened — win/loss and net P&L, or the
    skip/rejection reason if it never did.

Read-only and observational (the owner's own framing, 28 Sep): nothing here
feeds a trading decision. That is Part 1's job
(scheduler/runner.py, settings.ai_review_can_block_trade) and this page does
not touch it.

Registered from scheduler.health, same pattern as scheduler/v2_pages.py.
"""
from __future__ import annotations

import json

from aiohttp import web

REASON_WORDS: dict = {}


def _reason_words() -> dict:
    global REASON_WORDS
    if not REASON_WORDS:
        from scheduler.pipeline import REASON_WORDS as _RW
        REASON_WORDS = _RW
    return REASON_WORDS


async def api_ai_reality(request: web.Request) -> web.Response:
    """GET /api/ai-reality?days=7 — see module docstring."""
    from scheduler.health import _iso
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository

    try:
        days = max(1, min(int(request.query.get("days", "7")), 90))
    except ValueError:
        days = 7
    async with AsyncSessionFactory() as session:
        rows = await Repository(session).get_ai_vs_reality(days=days, limit=300)

    words = _reason_words()
    body = []
    for r in rows:
        paper = dict(r["paper"])
        if paper.get("reason"):
            paper["reason_text"] = words.get(paper["reason"],
                                              paper["reason"].replace("_", " "))
        body.append({
            "review_id": r["review_id"],
            "at": _iso(r["at"]),
            "symbol": r["symbol"],
            "signal_type": r["signal_type"],
            "direction": r["direction"],
            "ai": r["ai"],
            "market": r["market"],
            "paper": paper,
        })
    return web.Response(text=json.dumps({"days": days, "count": len(body), "rows": body}),
                        content_type="application/json")


async def ai_reality_page(request: web.Request) -> web.Response:
    return web.Response(text=_themed(_HTML), content_type="text/html")


def _themed(html: str) -> str:
    from scheduler.health import _THEME_SNIPPET
    return html.replace("</head>", _THEME_SNIPPET + "</head>")


def register(app: web.Application, runner=None) -> None:
    app.router.add_get("/ai-vs-reality", ai_reality_page)
    app.router.add_get("/api/ai-reality", api_ai_reality)


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI vs Reality</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);
  min-height:100vh;padding-bottom:60px;font-size:14px;line-height:1.5}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:11px 18px;display:flex;align-items:center;
  gap:10px;flex-wrap:wrap;position:sticky;top:0;z-index:30}
header h1{font-size:16px;font-weight:700;color:var(--text-strong)}
.spacer{margin-left:auto}
.sub{font-size:12px;color:var(--muted)}
a.nav-btn,button.nav-btn{background:var(--acc-t);border:1px solid var(--acc-t2);color:var(--accent);font-size:12px;
  font-weight:600;padding:6px 12px;border-radius:8px;text-decoration:none;white-space:nowrap;cursor:pointer;font-family:inherit}
select.nav-btn{padding:6px 10px}
main{padding:18px;max-width:1180px;margin:0 auto;display:grid;gap:14px}
.empty{color:var(--muted);padding:28px;text-align:center;background:var(--panel);border:1px solid var(--line);
  border-radius:14px}
.row{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;
  display:grid;gap:12px}
.row-head{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.sym{font-size:16px;font-weight:700;color:var(--text-strong)}
.meta{font-size:11.5px;color:var(--muted2)}
.dir{padding:2px 8px;border-radius:999px;font-size:10.5px;font-weight:700;text-transform:uppercase}
.dir.long{background:var(--pos-t);color:var(--pos)} .dir.short{background:var(--neg-t);color:var(--neg)}
.verdict{padding:3px 9px;border-radius:999px;font-size:10.5px;font-weight:800;letter-spacing:.04em;text-transform:uppercase}
.verdict.approve{background:var(--pos-t);color:var(--pos)}
.verdict.caution{background:var(--acc-t);color:var(--accent)}
.verdict.reject{background:var(--neg-t);color:var(--neg)}
.verdict.none{background:var(--mut-t);color:var(--muted)}
.cols{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
.col{background:var(--panel2);border:1px solid var(--line2);border-radius:10px;padding:10px 12px;display:grid;gap:6px;
  align-content:start}
.col .lbl{font-size:10px;letter-spacing:.08em;text-transform:uppercase;font-weight:700;color:var(--muted2)}
.col .txt{font-size:13px;color:var(--text)}
.delta{font-variant-numeric:tabular-nums;font-weight:700}
.pos{color:var(--pos)} .neg{color:var(--neg)} .muted{color:var(--muted)}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;background:var(--mut-t);
  color:var(--muted)}
.pill.won{background:var(--pos-t);color:var(--pos)} .pill.lost{background:var(--neg-t);color:var(--neg)}
.pill.pending{background:var(--mut-t);color:var(--muted)}
.stat{display:flex;justify-content:space-between;font-size:12.5px}
.stat b{font-variant-numeric:tabular-nums}
@media(max-width:820px){ .cols{grid-template-columns:1fr} main{padding:12px} }
</style>
</head>
<body>
<header>
  <h1>AI vs Reality</h1>
  <span class="sub" id="count">loading&hellip;</span>
  <span class="spacer"></span>
  <select class="nav-btn" id="days" onchange="load()">
    <option value="1">Last 1 day</option>
    <option value="3">Last 3 days</option>
    <option value="7" selected>Last 7 days</option>
    <option value="14">Last 14 days</option>
    <option value="30">Last 30 days</option>
  </select>
  <a class="nav-btn" href="/">&larr; Dashboard</a>
  <a class="nav-btn" href="/audit">Audit</a>
</header>
<main id="main"><div class="empty">Loading&hellip;</div></main>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function stamp(iso){ return window.fmtStamp? window.fmtStamp(iso) : esc(iso); }
function ago(iso){ return window.fmtAgo? window.fmtAgo(iso) : ''; }
function pct(v,digits){ if(v==null||!isFinite(v)) return '<span class="muted">—</span>';
  digits=digits==null?2:digits;
  return '<span class="'+(v>=0?'pos':'neg')+'">'+(v>=0?'+':'')+v.toFixed(digits)+'%</span>'; }

function verdictBadge(v){
  const k=(v||'').toUpperCase();
  const cls=k==='APPROVE'?'approve':k==='CAUTION'?'caution':k==='REJECT'?'reject':'none';
  return '<span class="verdict '+cls+'">'+esc(k||'no answer')+'</span>';
}

function aiCol(r){
  const a=r.ai||{};
  return '<div class="col"><div class="lbl">What AI said</div>'
    + verdictBadge(a.verdict)
    + '<div class="txt">'+esc(a.summary||'No summary recorded.')+'</div>'
    + '<div class="stat">Delta wanted <b class="delta '+((a.confidence_delta||0)>=0?'pos':'neg')+'">'
      +((a.confidence_delta||0)>=0?'+':'')+(a.confidence_delta||0).toFixed(3)+'</b></div>'
    + (a.model?'<div class="meta">'+esc(a.model)+'</div>':'')
    + '</div>';
}

function marketCol(r){
  const m=r.market;
  if(!m) return '<div class="col"><div class="lbl">What actually happened</div>'
    +'<div class="txt muted">No matching signal row found.</div></div>';
  const oc=m.outcome||'pending';
  const pill='<span class="pill '+(oc==='won'?'won':oc==='lost'?'lost':'pending')+'">'+esc(oc)+'</span>';
  return '<div class="col"><div class="lbl">What actually happened (real price)</div>'
    + pill
    + (m.decided? '<div class="stat">Move '+pct(m.pnl_pct)+'</div>'
                  +'<div class="stat">Net of costs '+pct(m.net_pnl_pct)+'</div>'
       : '<div class="txt muted">Target/stop not resolved yet.</div>')
    + '<div class="stat"><span class="muted">Target</span><b>'+esc(m.target)+'</b></div>'
    + '<div class="stat"><span class="muted">Stop</span><b>'+esc(m.stop)+'</b></div>'
    + '</div>';
}

function paperCol(r){
  const p=r.paper||{};
  let body;
  if(p.opened && p.status==='closed'){
    body = '<span class="pill '+(p.won?'won':'lost')+'">'+(p.won?'won':'lost')+'</span>'
      + '<div class="stat">Net P&amp;L <b class="'+(p.net_pnl>=0?'pos':'neg')+'">'
        +(p.net_pnl>=0?'+':'')+(p.net_pnl||0).toFixed(2)+'</b></div>'
      + '<div class="stat">Return on margin '+pct(p.return_on_margin_pct)+'</div>'
      + (p.exit_reason?'<div class="meta">Exit: '+esc(p.exit_reason)+'</div>':'');
  } else if(p.opened && p.status==='open'){
    body = '<span class="pill pending">still open</span>';
  } else if(p.status==='suppressed' || p.status==='rejected' || p.status==='not_opened'){
    body = '<span class="pill pending">not opened</span>'
      + '<div class="txt muted">'+esc(p.reason_text||p.reason||'unspecified')+'</div>';
  } else {
    body = '<div class="txt muted">No linked paper trade found.</div>';
  }
  return '<div class="col"><div class="lbl">What the paper trade did</div>'+body+'</div>';
}

async function load(){
  const days=document.getElementById('days').value;
  let data;
  try{ const r=await fetch('/api/ai-reality?days='+days); data=await r.json(); }
  catch(e){ data={rows:[],count:0}; }
  document.getElementById('count').textContent = data.count+' reviewed signal'+(data.count===1?'':'s')+' · last '+days+'d';
  const main=document.getElementById('main');
  if(!data.rows || !data.rows.length){
    main.innerHTML='<div class="empty">No Groq Sentinel reviews in this window yet.</div>';
    return;
  }
  main.innerHTML = data.rows.map(r=>
    '<article class="row"><div class="row-head">'
    + '<span class="sym">'+esc(r.symbol)+'</span>'
    + (r.direction? '<span class="dir '+esc(r.direction)+'">'+esc(r.direction)+'</span>':'')
    + '<span class="meta">'+esc(r.signal_type||'')+'</span>'
    + '<span class="spacer"></span>'
    + '<span class="meta">'+stamp(r.at)+' · '+ago(r.at)+'</span>'
    + '</div>'
    + '<div class="cols">'+aiCol(r)+marketCol(r)+paperCol(r)+'</div>'
    + '</article>'
  ).join('');
}
load();
</script>
</body>
</html>"""
