"""
GET /api-docs: every API the engine serves, each with Fetch and Copy.

The owner debugs from a phone by pasting JSON into a chat. This page lists
the endpoints (a small Swagger), fetches one with the browser's own admin
cookie, and copies the answer ready to paste:

  - secrets are masked before anything is shown or copied (token, password,
    key, hash, cookie fields and credentials inside URLs)
  - long lists and strings can be trimmed, since a phone paste has limits
  - "Copy diagnostics" fetches the handful that answer "why no trades / why
    slow" in one go

The site is plain http, where navigator.clipboard does not exist, so Copy
selects the text box and uses execCommand, inside the tap that asked for it.
POST endpoints are listed for reference only: this page never changes state.
"""
from __future__ import annotations

import json

from aiohttp import web

# (group, method, path, what it answers, query params {name: example})
ENDPOINTS: tuple[tuple[str, str, str, str, dict], ...] = (
    ("Why no trades", "GET", "/api/pipeline",
     "Signal funnel for 24 h: why the analysers found no setup, filters, paper skips, "
     "live gates, per-coin movement.", {}),
    ("Why no trades", "GET", "/api/debug/signals",
     "Why each coin did or did not produce a signal right now.", {}),
    ("Why no trades", "GET", "/api/events",
     "Is trading paused for news, and what is coming up.", {}),
    ("Why no trades", "GET", "/api/debug/volume",
     "Whether each coin has usable per-bar volume.", {"symbol": ""}),
    ("Health & speed", "GET", "/api/status", "Engine status, collectors, heartbeat.", {}),
    ("Health & speed", "GET", "/api/debug", "Per-coin feed state and collector status.", {}),
    ("Health & speed", "GET", "/api/debug/perf",
     "Slowest pages, slowest jobs, event-loop stalls.", {}),
    ("Health & speed", "GET", "/api/debug/binance", "Try a Binance WebSocket from the server.", {}),
    ("Health & speed", "GET", "/api/debug/coindcx", "What CoinDCX returns for a coin.",
     {"symbol": "btcusdt"}),
    ("Health & speed", "GET", "/health", "Liveness check.", {}),
    ("Paper trading", "GET", "/api/paper", "Wallet, open positions, trade log.", {}),
    ("Paper trading", "GET", "/api/paper/events", "One trade's change log.",
     {"symbol": "SOLUSDT", "opened_at": ""}),
    ("Paper trading", "GET", "/api/paper/config", "Paper trading settings.", {}),
    ("Signals", "GET", "/api/crypto/signals", "Recent signals.", {}),
    ("Signals", "GET", "/api/signals/history", "Signals in a window.",
     {"days": "7", "before": ""}),
    ("Signals", "GET", "/api/signals/accuracy", "Calibration and per-setup accuracy.", {}),
    ("Signals", "GET", "/api/audit", "Every fired signal in a window, scored.", {"days": "7"}),
    ("Signals", "GET", "/api/audit/reviewer", "Was the AI reviewer right?", {"days": "7"}),
    ("Signals", "GET", "/api/audit/methods", "The code that produces a signal.", {}),
    ("Signals", "GET", "/api/reviews", "Every AI review.",
     {"days": "7", "phase": "pre", "limit": "100"}),
    ("v2 strategy", "GET", "/api/v2/backtest", "Latest v2 backtest report.", {}),
    ("v2 strategy", "GET", "/api/v2/shadow", "v2 live shadow signals.", {"days": "7"}),
    ("v2 strategy", "GET", "/api/journal", "Your manual trade journal.", {}),
    ("Market", "GET", "/api/crypto/coins", "Live market state and indicators per coin.", {}),
    ("Market", "GET", "/api/crypto/forecasts", "30m / 1h / 4h / 1d forecasts.", {}),
    ("Market", "GET", "/api/predict", "Band and lean per coin, three horizons.", {}),
    ("Market", "GET", "/api/crypto/watchlist", "Coins being followed.", {}),
    ("Market", "GET", "/api/binance/symbols", "Search Binance pairs.", {"q": "sol"}),
    ("Market", "GET", "/api/commodities", "Gold, silver, oil.", {}),
    ("Market", "GET", "/api/moves", "Hourly 'why did it move' analyses.", {"limit": "24"}),
    ("Market", "GET", "/api/sentiment/recent", "Scored headlines lately.",
     {"hours": "6", "symbol": ""}),
    ("Market", "GET", "/api/research", "Measured state of the edge.", {}),
    ("Settings", "GET", "/api/app-settings", "Every setting, its value and source.", {}),
    ("Settings", "GET", "/api/strategy/config", "Strategy config (legacy).", {}),
    ("Settings", "GET", "/api/settings", "Collector switches (legacy).", {}),
    ("Settings", "GET", "/api/settings/auth/status", "Are you logged in as admin?", {}),
    ("Database", "GET", "/api/tables", "Every table, newest rows. Large: keep trim on.",
     {"limit": "5"}),
    ("Changes state (reference only)", "POST", "/api/app-settings", "Save settings.", {}),
    ("Changes state (reference only)", "POST", "/api/paper/config", "Save paper config.", {}),
    ("Changes state (reference only)", "POST", "/api/v2/backtest/run", "Run the v2 backtest.", {}),
    ("Changes state (reference only)", "POST", "/api/moves/run", "Run move analysis now.", {}),
    ("Changes state (reference only)", "POST", "/api/crypto/watchlist/add",
     "Add a coin: {\"symbol\": \"dogeusdt\"}.", {}),
    ("Changes state (reference only)", "POST", "/api/crypto/watchlist/remove",
     "Remove a coin.", {}),
    ("Changes state (reference only)", "POST", "/api/journal", "Add a journal entry.", {}),
    ("Changes state (reference only)", "POST", "/api/sentiment/ingest",
     "Scored headlines from an external analyser.", {}),
)

# Fetched together by "Copy diagnostics".
DIAGNOSTICS = ("/api/pipeline", "/api/debug/signals", "/api/events", "/api/status",
               "/api/debug/perf", "/api/paper", "/api/app-settings")


def spec() -> list[dict]:
    return [{"group": g, "method": m, "path": p, "desc": d, "params": q}
            for g, m, p, d, q in ENDPOINTS]


async def api_docs_page(request: web.Request) -> web.Response:
    from scheduler.health import _THEME_SNIPPET
    html = (PAGE.replace("__SPEC__", json.dumps(spec()))
            .replace("__DIAG__", json.dumps(list(DIAGNOSTICS)))
            .replace("</head>", _THEME_SNIPPET + "</head>"))
    return web.Response(text=html, content_type="text/html")


def register(app: web.Application) -> None:
    app.router.add_get("/api-docs", api_docs_page)


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>API list</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);
  color:var(--text);font-size:14px;line-height:1.5;padding-bottom:40px}
:root{--sh-d:var(--neu-d,color-mix(in srgb,#000 55%,var(--bg)));
  --sh-l:var(--neu-l,color-mix(in srgb,#fff 7%,var(--bg)))}
html[data-theme="light"]{--sh-d:color-mix(in srgb,#000 16%,var(--bg));--sh-l:#fff}
.raise{background:var(--bg);border-radius:18px;
  box-shadow:6px 6px 14px var(--sh-d),-6px -6px 14px var(--sh-l)}
.inset{background:var(--bg);border-radius:12px;
  box-shadow:inset 3px 3px 7px var(--sh-d),inset -3px -3px 7px var(--sh-l)}
header{position:sticky;top:0;z-index:5;background:var(--bg);padding:14px 16px 10px}
header h1{font-size:20px;color:var(--text-strong);margin-bottom:10px}
nav{display:flex;gap:10px;overflow-x:auto;padding:4px 2px 10px}
nav a{flex:none;text-decoration:none;color:var(--text);font-weight:600;font-size:13px;
  padding:9px 15px;border-radius:14px;background:var(--bg);
  box-shadow:4px 4px 9px var(--sh-d),-4px -4px 9px var(--sh-l)}
main{max-width:860px;margin:0 auto;padding:6px 16px;display:grid;gap:18px}
.card{padding:16px 18px;min-width:0}
h2{font-size:15px;color:var(--text-strong);margin-bottom:10px}
.muted{color:var(--muted)} .small{font-size:12px}
.ep{padding:12px 14px;margin-top:10px;min-width:0}
.ep .top{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.m{font-size:10.5px;font-weight:800;padding:2px 7px;border-radius:8px;color:var(--pos);
  box-shadow:inset 1px 1px 3px var(--sh-d),inset -1px -1px 3px var(--sh-l)}
.m.post{color:var(--accent)}
code{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;word-break:break-all}
.d{font-size:12px;color:var(--muted);margin-top:3px}
.params{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.params label{font-size:12px;color:var(--muted);display:flex;gap:6px;align-items:center}
.params input,.opts input[type=number]{width:120px;border:0;color:var(--text);font:inherit;
  font-size:12.5px;padding:7px 9px;outline:none;background:var(--bg);border-radius:10px;
  box-shadow:inset 2px 2px 5px var(--sh-d),inset -2px -2px 5px var(--sh-l)}
.btns{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;align-items:center}
.btn{border:0;cursor:pointer;font:inherit;font-weight:700;font-size:13px;color:var(--accent);
  padding:9px 15px;border-radius:12px;background:var(--bg);
  box-shadow:4px 4px 9px var(--sh-d),-4px -4px 9px var(--sh-l)}
.btn:active{box-shadow:inset 3px 3px 7px var(--sh-d),inset -3px -3px 7px var(--sh-l)}
.btn[disabled]{opacity:.45;cursor:default}
textarea{width:100%;height:220px;margin-top:10px;border:0;outline:none;resize:vertical;
  color:var(--text);background:var(--bg);font:12px/1.4 ui-monospace,Menlo,monospace;
  padding:10px;border-radius:12px;display:none;
  box-shadow:inset 3px 3px 7px var(--sh-d),inset -3px -3px 7px var(--sh-l)}
.opts{display:flex;flex-wrap:wrap;gap:14px;align-items:center;font-size:13px}
.opts label{display:flex;gap:6px;align-items:center}
#toast{position:fixed;top:14px;left:50%;transform:translateX(-50%);padding:10px 16px;
  font-weight:600;display:none;z-index:9}
</style></head><body>
<header><h1>🔌 API list</h1>
<nav><a href="/">Dashboard</a><a href="/settings">Settings</a><a href="/v2">v2 strategy</a>
<a href="/journal">Journal</a></nav></header>
<main>
<section class="card raise">
  <h2>Copy for Claude</h2>
  <p class="small muted">Fetch an API, tap Copy, paste it in the chat. Secrets (tokens,
  passwords, keys, database URLs) are hidden before anything is shown or copied. Admin-only
  APIs use your login from the Settings page.</p>
  <div class="opts" style="margin-top:10px">
    <label><input type="checkbox" id="trim" checked> Trim long lists to</label>
    <input type="number" id="items" value="20" min="1" max="500"> items
  </div>
  <div class="btns"><button class="btn" id="diagBtn">Fetch diagnostics bundle</button>
    <button class="btn" id="diagCopy" disabled>Copy</button>
    <span class="small muted" id="diagInfo"></span></div>
  <p class="small muted" style="margin-top:6px">Bundle: why no trades, events, status, speed,
    paper book, settings.</p>
  <textarea id="diagOut" readonly></textarea>
</section>
<div id="groups" style="display:grid;gap:18px"></div>
</main>
<div id="toast" class="raise"></div>
<script>
const SPEC=__SPEC__, DIAG=__DIAG__;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function toast(t){const el=document.getElementById('toast');el.textContent=t;el.style.display='block';
  clearTimeout(toast.t);toast.t=setTimeout(()=>el.style.display='none',1800);}
const SECRET=/(token|secret|password|passwd|api_?key|apikey|hash|authorization|cookie|private|credential|dsn|database_url)/i;
function clean(v,depth){
  const trim=document.getElementById('trim').checked, n=+document.getElementById('items').value||20;
  if(Array.isArray(v)){const out=(trim?v.slice(0,n):v).map(x=>clean(x,depth+1));
    if(trim&&v.length>n) out.push('… '+(v.length-n)+' more');return out;}
  if(v&&typeof v==='object'){const o={};for(const k of Object.keys(v))
    o[k]=SECRET.test(k)&&v[k]!==null&&typeof v[k]!=='boolean'&&typeof v[k]!=='object'?'[hidden]':clean(v[k],depth+1);return o;}
  if(typeof v==='string'){let s=v.replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s\/@:]+:[^\s\/@]+@/gi,'$1[hidden]@');
    if(trim&&s.length>600) s=s.slice(0,600)+'… ('+(v.length-600)+' more chars)';return s;}
  return v;
}
async function grab(path){
  const t0=performance.now();
  try{const r=await fetch(path,{credentials:'same-origin'});
    const ms=Math.round(performance.now()-t0), txt=await r.text();
    let body; try{body=clean(JSON.parse(txt),0);}catch(e){body=clean(txt.slice(0,2000),0);}
    return {status:r.status, ms, body};
  }catch(e){return {status:0, ms:Math.round(performance.now()-t0), body:'fetch failed: '+e};}
}
function stamp(){return window.fmtStamp?window.fmtStamp(new Date().toISOString(),{seconds:true}):new Date().toISOString();}
function show(ta,btn,info,obj){
  const txt=JSON.stringify(obj,null,1);ta.value=txt;ta.style.display='block';btn.disabled=false;
  info.textContent=(txt.length/1024).toFixed(1)+' KB';}
function copy(ta){
  ta.focus();ta.select();ta.setSelectionRange(0,ta.value.length);
  let ok=false;try{ok=document.execCommand('copy');}catch(e){}
  if(!ok&&navigator.clipboard){navigator.clipboard.writeText(ta.value).then(()=>toast('Copied'),()=>toast('Long-press the box and Copy'));return;}
  toast(ok?'Copied — paste it in the chat':'Long-press the box and Copy');
}
function url(ep,box){const q=new URLSearchParams();
  box.querySelectorAll('.params input').forEach(i=>{if(i.value.trim()) q.set(i.name,i.value.trim());});
  const s=q.toString();return ep.path+(s?'?'+s:'');}
const groups={};SPEC.forEach(e=>(groups[e.group]=groups[e.group]||[]).push(e));
document.getElementById('groups').innerHTML=Object.entries(groups).map(([g,list])=>
  '<section class="card raise"><h2>'+esc(g)+'</h2>'+list.map((e,i)=>{
    const id=esc(g)+'-'+i, get=e.method==='GET';
    return '<div class="ep inset" data-path="'+esc(e.path)+'"><div class="top"><span class="m '+(get?'':'post')+'">'
      +e.method+'</span><code>'+esc(e.path)+'</code></div><div class="d">'+esc(e.desc)+'</div>'
      +(Object.keys(e.params).length?'<div class="params">'+Object.entries(e.params).map(([k,v])=>
        '<label>'+esc(k)+' <input name="'+esc(k)+'" value="'+esc(v)+'" placeholder="optional"></label>').join('')+'</div>':'')
      +(get?'<div class="btns"><button class="btn f">Fetch</button><button class="btn c" disabled>Copy</button>'
        +'<a class="btn" style="text-decoration:none" target="_blank" href="'+esc(e.path)+'">Open</a>'
        +'<span class="small muted i"></span></div><textarea readonly></textarea>':'')
      +'</div>';}).join('')+'</section>').join('');
document.querySelectorAll('.ep').forEach(box=>{
  const f=box.querySelector('.f'); if(!f) return;
  const c=box.querySelector('.c'), ta=box.querySelector('textarea'), info=box.querySelector('.i');
  const ep=SPEC.find(e=>e.path===box.dataset.path&&e.method==='GET');
  f.onclick=async()=>{f.disabled=true;info.textContent='fetching…';
    const u=url(ep,box), r=await grab(u);f.disabled=false;
    show(ta,c,info,{api:'GET '+u,status:r.status,ms:r.ms,at:stamp(),data:r.body});
    info.textContent='HTTP '+r.status+' · '+r.ms+' ms · '+info.textContent;};
  c.onclick=()=>copy(ta);
});
const dBtn=document.getElementById('diagBtn'), dCopy=document.getElementById('diagCopy'),
  dOut=document.getElementById('diagOut'), dInfo=document.getElementById('diagInfo');
dBtn.onclick=async()=>{dBtn.disabled=true;dInfo.textContent='fetching '+DIAG.length+' APIs…';
  const res=await Promise.all(DIAG.map(grab)), out={bundle:'diagnostics',at:stamp()};
  DIAG.forEach((p,i)=>out[p]={status:res[i].status,ms:res[i].ms,data:res[i].body});
  dBtn.disabled=false;show(dOut,dCopy,dInfo,out);};
dCopy.onclick=()=>copy(dOut);
</script></body></html>"""
