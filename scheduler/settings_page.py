"""
The settings page, rebuilt: one list of settings that actually do something.

  GET  /api/app-settings   every editable setting, its value and whether it
                           applies live; the AI models in use; paper config
  POST /api/app-settings   save (admin): validated, stored in the database,
                           applied to the running engine immediately

Every setting here is read by the engine (config/overrides.py lists them and
says which apply only after a restart). The old page, kept at
/settings/classic for now, showed switches that were never saved and saved
fields that were never read.

Styled as a neumorphic "soft UI" on the site's theme colours, so it follows
whichever of the themes is picked.
"""
from __future__ import annotations

from aiohttp import web


async def _admin(request: web.Request):
    from scheduler.health import _SETTINGS, _verify_admin_session
    from scheduler.security import check_bearer_auth
    if await _verify_admin_session(request):
        return None
    return check_bearer_auth(request, _SETTINGS.api_auth_token)


def settings_api(runner):
    async def get(request: web.Request) -> web.Response:
        from collectors.llm_client import chain_for
        from config.overrides import describe
        from config.settings import settings
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        async with AsyncSessionFactory() as session:
            stored = await Repository(session).get_app_settings()
        roles = ("pre_trade", "position_review", "post_trade", "briefing", "attribution",
                 "news_scoring", "history")
        models = {r: [f"{p}:{m}" for p, m in chain_for(r)] for r in roles}
        live_sources = sorted(k for k, v in (getattr(runner, "collector_enabled", {}) or {}).items()
                              if v)
        if settings.binance_klines_enabled:
            live_sources.insert(0, "binance_klines")
        return web.json_response({"fields": describe(settings, stored), "models": models,
                                  "sources_on": live_sources})

    async def post(request: web.Request) -> web.Response:
        from config.overrides import BY_KEY, apply, coerce
        from config.settings import settings
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        denied = await _admin(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "send JSON"}, status=400)
        values, errors = {}, []
        for key, value in (body.get("values") or {}).items():
            field = BY_KEY.get(key)
            if field is None:
                errors.append(f"unknown setting {key}")
                continue
            try:
                values[key] = coerce(field, value)
            except (TypeError, ValueError) as exc:
                errors.append(str(exc))
        if errors:
            return web.json_response({"error": "; ".join(errors)}, status=400)
        async with AsyncSessionFactory() as session:
            await Repository(session).save_app_settings(values)
        applied = apply(settings, values)
        if hasattr(runner, "on_settings_changed"):
            runner.on_settings_changed(applied)
        restart = [k for k in applied if not BY_KEY[k].live]
        return web.json_response({"ok": True, "applied": applied, "needs_restart": restart})

    return get, post


async def settings_page(request: web.Request) -> web.Response:
    from scheduler.health import _THEME_SNIPPET
    return web.Response(text=PAGE.replace("</head>", _THEME_SNIPPET + "</head>"),
                        content_type="text/html")


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Settings</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);
  color:var(--text);font-size:14px;line-height:1.5;padding-bottom:110px}
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
nav a:active{box-shadow:inset 3px 3px 7px var(--sh-d),inset -3px -3px 7px var(--sh-l)}
main{max-width:860px;margin:0 auto;padding:6px 16px;display:grid;gap:18px}
.card{padding:16px 18px}
h2{font-size:15px;color:var(--text-strong);display:flex;align-items:center;gap:8px}
.muted{color:var(--muted)} .small{font-size:12px}
.pill{font-size:11px;font-weight:700;padding:3px 9px;border-radius:10px}
.pill.on{color:var(--pos);box-shadow:inset 2px 2px 5px var(--sh-d),inset -2px -2px 5px var(--sh-l)}
.pill.warn{color:var(--accent)}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.chips span{font-size:12px;padding:5px 10px;border-radius:10px;
  box-shadow:inset 2px 2px 5px var(--sh-d),inset -2px -2px 5px var(--sh-l)}
details{padding:0}
summary{list-style:none;cursor:pointer;padding:16px 18px;display:flex;align-items:center;
  justify-content:space-between;font-weight:700;color:var(--text-strong)}
summary::-webkit-details-marker{display:none}
summary::after{content:'＋';color:var(--muted)} details[open] summary::after{content:'－'}
.rows{padding:0 14px 14px;display:grid;gap:10px}
.row{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;padding:12px 14px}
.row .l{font-weight:600} .row .h{font-size:12px;color:var(--muted);margin-top:2px}
.row .b{display:flex;gap:6px;margin-top:4px;flex-wrap:wrap}
.tag{font-size:10.5px;font-weight:700;letter-spacing:.03em;padding:2px 7px;border-radius:8px;
  color:var(--muted);box-shadow:inset 1px 1px 3px var(--sh-d),inset -1px -1px 3px var(--sh-l)}
.tag.acc{color:var(--accent)} .row.dirty{outline:2px solid var(--acc-t2)}
.sw{position:relative;display:inline-block;width:54px;height:30px;flex:none;vertical-align:middle}
.sw input{opacity:0;width:0;height:0}
.sw span{position:absolute;inset:0;border-radius:18px;cursor:pointer;
  box-shadow:inset 3px 3px 6px var(--sh-d),inset -3px -3px 6px var(--sh-l);transition:.2s}
.sw span::before{content:'';position:absolute;width:22px;height:22px;left:4px;top:4px;
  border-radius:50%;background:var(--muted2);transition:.2s;
  box-shadow:2px 2px 5px var(--sh-d),-1px -1px 3px var(--sh-l)}
.sw input:checked+span::before{transform:translateX(24px);background:var(--pos)}
input.num{width:96px;border:0;color:var(--text);font:inherit;font-weight:600;
  padding:9px 11px;text-align:right;outline:none}
.btn{border:0;cursor:pointer;font:inherit;font-weight:700;color:var(--accent);
  padding:12px 20px;border-radius:14px;background:var(--bg);
  box-shadow:5px 5px 11px var(--sh-d),-5px -5px 11px var(--sh-l)}
.btn:active{box-shadow:inset 3px 3px 7px var(--sh-d),inset -3px -3px 7px var(--sh-l)}
.btn[disabled]{opacity:.5;cursor:default}
.savebar{position:fixed;left:0;right:0;bottom:0;padding:12px 16px 18px;background:var(--bg);
  display:flex;justify-content:center;gap:12px;align-items:center;z-index:6;
  box-shadow:0 -6px 14px var(--sh-d)}
.themes{display:flex;flex-wrap:wrap;gap:12px;margin-top:12px}
.th{width:40px;height:40px;border-radius:50%;cursor:pointer;border:3px solid transparent;
  box-shadow:4px 4px 9px var(--sh-d),-4px -4px 9px var(--sh-l)}
.th.on{border-color:var(--text-strong)}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
td,th{padding:6px 4px;text-align:left;border-bottom:1px solid var(--line2)}
th{color:var(--muted2);font-weight:600}
.login{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}
.login input{flex:1;min-width:160px;border:0;color:var(--text);font:inherit;padding:12px 14px;
  outline:none}
#toast{position:fixed;top:14px;left:50%;transform:translateX(-50%);padding:10px 16px;
  font-weight:600;display:none;z-index:9}
@media(max-width:520px){.row{gap:10px;padding:12px}.row .h{font-size:11.5px}}
</style></head><body>
<header><h1>⚙️ Settings</h1>
<nav><a href="/">Dashboard</a><a href="/v2">v2 strategy</a><a href="/journal">Journal</a>
<a href="/moves">Market moves</a><a href="#speed">Speed</a><a href="/settings/classic">Classic</a></nav>
</header>
<main>
<section class="card raise" id="auth"></section>
<section class="card raise"><h2>Data in use right now</h2>
<div class="chips" id="sources"></div>
<p class="small muted" style="margin-top:10px">Every switch below is saved in the database and read
by the engine. A <span class="tag acc">restart</span> tag means it takes effect after the next
restart; everything else applies the moment you save.</p></section>
<div id="groups"></div>
<details class="raise"><summary>🧪 Paper trading</summary><div class="rows" id="paper"></div></details>
<details class="raise"><summary>🤖 AI models in use</summary><div class="rows" id="models"></div></details>
<details class="raise" id="speed"><summary>⚡ Speed (why pages are slow)</summary>
<div class="rows" id="perf"><p class="muted small">Log in to see timings.</p></div></details>
<section class="card raise"><h2>🎨 Theme</h2><div class="themes" id="themes"></div></section>
</main>
<div class="savebar"><span class="muted small" id="dirty">No changes</span>
<button class="btn" id="save" disabled onclick="save()">Save changes</button></div>
<div id="toast" class="raise"></div>
<script>
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
const tok=()=>sessionStorage.getItem('settings_token')||'';
const H=()=>({'Content-Type':'application/json','X-Settings-Token':tok()});
let FIELDS=[], PAPER={}, changes={}, paperChanges={};
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.style.display='block';
  setTimeout(()=>t.style.display='none',3000);}
const THEMES=[['amber','#f59e0b'],['neu','#e0a84a'],['neu-light','#e6e9ef'],['carbon','#a3e635'],
  ['crimson','#fb7185'],['violet','#a78bfa'],['emerald','#34d399'],['navy','#0ea5e9'],['light','#f4f1ea']];
function themes(){const cur=localStorage.getItem('site_theme')||'amber';
  document.getElementById('themes').innerHTML=THEMES.map(([t,c])=>'<div class="th'+(t===cur?' on':'')
    +'" title="'+t+'" style="background:'+c+'" onclick="setSiteTheme(\''+t+'\');themes()"></div>').join('');}
async function auth(){
  const r=await fetch('/api/settings/auth/status',{headers:H()}).then(r=>r.json()).catch(()=>({}));
  const el=document.getElementById('auth');
  if(r.authenticated){el.innerHTML='<h2>🔓 Logged in <span class="pill on">admin</span></h2>'
    +'<p class="small muted" style="margin-top:6px">Changes save to the engine. Other tabs need '
    +'their own login (the session lives in this tab).</p>'
    +'<div class="login"><button class="btn" onclick="logout()">Log out</button></div>'; perf(); return true;}
  el.innerHTML='<h2>🔒 Log in to change settings</h2><div class="login">'
    +'<input class="inset" type="password" id="pw" placeholder="Admin password" '
    +'onkeydown="if(event.key===\'Enter\')login()"><button class="btn" onclick="login()">Log in</button></div>';
  return false;}
async function login(){const pw=document.getElementById('pw').value;
  const r=await fetch('/api/settings/auth/login',{method:'POST',headers:H(),body:JSON.stringify({password:pw})});
  const d=await r.json(); if(d.ok){sessionStorage.setItem('settings_token',d.token);auth();toast('Logged in');}
  else toast(d.error||'Login failed');}
async function logout(){await fetch('/api/settings/auth/logout',{method:'POST',headers:H()});
  sessionStorage.removeItem('settings_token');auth();}
function control(f){const v=f.value, id='f-'+f.key;
  if(f.kind==='bool') return '<label class="sw"><input type="checkbox" id="'+id+'"'+(v?' checked':'')
    +' onchange="mark(\''+f.key+'\',this.checked)"><span></span></label>';
  return '<input class="num inset" id="'+id+'" type="number" step="'+(f.kind==='int'?1:0.01)+'"'
    +(f.lo!=null?' min="'+f.lo+'"':'')+(f.hi!=null?' max="'+f.hi+'"':'')+' value="'+esc(v)
    +'" onchange="mark(\''+f.key+'\',this.value)">';}
function mark(k,v){changes[k]=v;document.getElementById('r-'+k).classList.add('dirty');bar();}
function markP(k,v){paperChanges[k]=v;document.getElementById('p-'+k).classList.add('dirty');bar();}
function bar(){const n=Object.keys(changes).length+Object.keys(paperChanges).length;
  document.getElementById('dirty').textContent=n?n+' unsaved change'+(n>1?'s':''):'No changes';
  document.getElementById('save').disabled=!n;}
async function load(){
  const d=await fetch('/api/app-settings').then(r=>r.json());
  FIELDS=d.fields;
  document.getElementById('sources').innerHTML=(d.sources_on||[]).map(s=>'<span>'+esc(s)+'</span>').join('');
  const groups={}; FIELDS.forEach(f=>(groups[f.group_label]=groups[f.group_label]||[]).push(f));
  const icons={'Market data':'📡','Signals':'📈','Protections':'🛡️','AI':'🤖','v2 strategy':'🧭','Storage':'🗄️'};
  document.getElementById('groups').innerHTML=Object.entries(groups).map(([g,fs],i)=>
    '<details class="raise" style="margin-bottom:18px"'+(i===0?' open':'')+'><summary>'+(icons[g]||'')
    +' '+esc(g)+'</summary><div class="rows">'+fs.map(f=>'<div class="row inset" id="r-'+f.key+'"><div>'
    +'<div class="l">'+esc(f.label)+'</div>'+(f.help?'<div class="h">'+esc(f.help)+'</div>':'')
    +'<div class="b">'+(f.live?'':'<span class="tag acc">restart</span>')
    +(f.source==='saved'?'<span class="tag">saved</span>':'<span class="tag">default</span>')
    +'</div></div><div>'+control(f)+'</div></div>').join('')+'</div></details>').join('');
  document.getElementById('models').innerHTML=Object.entries(d.models||{}).map(([role,chain])=>
    '<div class="row inset"><div><div class="l">'+esc(role.replace(/_/g,' '))+'</div><div class="h">'
    +(chain.length?esc(chain.join(' → ')):'no model configured')+'</div></div><div></div></div>').join('')
    +'<p class="small muted">Set in .env (LLM_CHAIN_*): the first model that answers is used.</p>';
  PAPER=await fetch('/api/paper/config').then(r=>r.json()).catch(()=>({}));
  const P=[['enabled','Paper trading on','bool'],['starting_wallet','Starting wallet (₹)','num'],
    ['target_wallet','Target wallet (₹)','num'],['leverage','Base leverage','num'],
    ['max_leverage','Max leverage','num'],['min_confidence','Min confidence','num'],
    ['max_concurrent','Max open trades','num'],['max_hold_minutes','Max hold (min)','num'],
    ['trailing_enabled','Trailing stop','bool'],['alert_telegram','Telegram alerts','bool']];
  document.getElementById('paper').innerHTML=P.map(([k,l,t])=>'<div class="row inset" id="p-'+k+'"><div>'
    +'<div class="l">'+l+'</div></div><div>'+(t==='bool'
      ?'<label class="sw"><input type="checkbox"'+(PAPER[k]?' checked':'')+' onchange="markP(\''+k+'\',this.checked)"><span></span></label>'
      :'<input class="num inset" type="number" step="any" value="'+esc(PAPER[k])+'" onchange="markP(\''+k+'\',Number(this.value))">')
    +'</div></div>').join('');
}
async function save(){
  let msg=[];
  if(Object.keys(changes).length){
    const r=await fetch('/api/app-settings',{method:'POST',headers:H(),body:JSON.stringify({values:changes})});
    const d=await r.json();
    if(!r.ok){toast(r.status===401?'Log in first':(d.error||'Not saved'));return;}
    msg.push('Saved '+d.applied.length+(d.needs_restart.length?' ('+d.needs_restart.length+' after restart)':''));}
  if(Object.keys(paperChanges).length){
    const r=await fetch('/api/paper/config',{method:'POST',headers:H(),body:JSON.stringify(paperChanges)});
    if(!r.ok){toast(r.status===401?'Log in first':'Paper settings not saved');return;}
    msg.push('paper settings saved');}
  changes={};paperChanges={};bar();await load();toast(msg.join(', '));}
async function perf(){
  const r=await fetch('/api/debug/perf',{headers:H()}); if(!r.ok) return;
  const d=await r.json(); const el=document.getElementById('perf');
  const blame=Object.entries(d.stall_blame_ms||{}).slice(0,6);
  el.innerHTML='<div class="row inset"><div><div class="l">Database round trip</div><div class="h">'
    +d.db_ping_ms.p50+' ms typical, '+d.db_ping_ms.max+' ms worst, '+d.db_ping_ms.failures+' failures</div></div><div></div></div>'
    +'<div class="row inset"><div><div class="l">What froze the server</div><div class="h">'
    +(blame.length?blame.map(([j,ms])=>esc(j)+' '+(ms/1000).toFixed(1)+'s').join(' · '):'No freezes over 0.3 s yet')
    +'</div></div><div></div></div>'
    +'<div class="inset" style="padding:10px 12px;overflow-x:auto"><table><tr><th>Slowest pages / APIs</th><th>typical</th><th>worst 5%</th><th>n</th></tr>'
    +d.routes.slice(0,10).map(r=>'<tr><td>'+esc(r.route)+'</td><td>'+r.p50_ms+' ms</td><td>'+r.p95_ms+' ms</td><td>'+r.n+'</td></tr>').join('')
    +'</table></div><div class="inset" style="padding:10px 12px;overflow-x:auto"><table><tr><th>Slowest jobs</th><th>typical</th><th>worst</th></tr>'
    +d.jobs.slice(0,10).map(j=>'<tr><td>'+esc(j.job)+'</td><td>'+j.p50_ms+' ms</td><td>'+j.max_ms+' ms</td></tr>').join('')
    +'</table></div>';}
themes(); auth(); load();
</script></body></html>"""


def register(app: web.Application, runner) -> None:
    get, post = settings_api(runner)
    app.router.add_get("/api/app-settings", get)
    app.router.add_post("/api/app-settings", post)
