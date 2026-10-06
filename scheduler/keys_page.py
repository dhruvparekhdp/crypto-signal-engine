"""
/keys: every API key, token and service address in one place, editable from a phone.

Keys are stored encrypted in the database (config/secret_box.py, master key SECRETS_MASTER_KEY in .env) and the
database wins over .env. The page never receives a key back: only whether it is set, where it comes from and its last
four characters. Every action needs the same admin login as /settings.

    GET  /keys                 the page
    GET  /api/keys             status of every key
    POST /api/keys/save        {"key", "value"}   store (encrypted) and apply now
    POST /api/keys/clear       {"key"}            stop using the stored value (falls back to .env, if any)
    POST /api/keys/test        {"key"}            a harmless read-only call that proves the key works
    POST /api/keys/import-env                     copy every key that only .env has into the database
    POST /api/keys/reveal      {"key"}            the value itself, on request, to view or copy (logged by name)
"""
from __future__ import annotations

import os

from aiohttp import web

TESTERS = {
    "groq_api_key": ("GET", "https://api.groq.com/openai/v1/models", "bearer"),
    "openrouter_api_key": ("GET", "https://openrouter.ai/api/v1/key", "bearer"),
    "anthropic_api_key": ("GET", "https://api.anthropic.com/v1/models", "anthropic"),
    "gemini_api_key": ("GET", "https://generativelanguage.googleapis.com/v1beta/models", "query_key"),
    "hf_api_token": ("GET", "https://huggingface.co/api/whoami-v2", "bearer"),
    "telegram_bot_token": ("GET", "https://api.telegram.org/bot{v}/getMe", "path"),
    "twelvedata_api_key": ("GET", "https://api.twelvedata.com/api_usage", "query_apikey"),
}


def _keys():
    from config.overrides import FIELDS
    return [f for f in FIELDS if f.group == "keys"]


def _value(settings, key: str) -> str:
    v = getattr(settings, key, None)
    if v is None:
        return ""
    return v.get_secret_value() if hasattr(v, "get_secret_value") else str(v)


def _env_value(key: str) -> str:
    """What .env (or the process environment) alone would give, ignoring the database."""
    from config.settings import Settings
    try:
        return _value(Settings(), key)
    except Exception:  # noqa: BLE001
        return os.environ.get(key.upper(), "")


async def _stored() -> dict:
    from storage.database import AsyncSessionFactory
    from storage.repository import Repository
    async with AsyncSessionFactory() as session:
        return await Repository(session).get_app_settings()


def status_rows(settings, stored: dict) -> list[dict]:
    from config import secret_box
    rows = []
    for f in _keys():
        s = stored.get(f.key)
        live = _value(settings, f.key)
        env = _env_value(f.key)
        if secret_box.is_sealed(s):
            where = "database (encrypted)" if secret_box.open_(s) is not None else "database (cannot decrypt: master key changed?)"
        elif isinstance(s, str) and s:
            where = "database (plain text)" if f.kind == "secret" else "database"
        elif env:
            where = ".env only"
        else:
            where = "not set"
        hint = ("…" + live[-4:]) if (f.kind == "secret" and len(live) >= 8) else ("set" if live and f.kind == "secret" else "")
        rows.append({"key": f.key, "label": f.label, "help": f.help, "kind": f.kind, "where": where,
                     "is_set": bool(live), "hint": hint, "value": live if f.kind != "secret" else None,
                     "env_differs": bool(env) and bool(live) and env != live, "testable": f.key in TESTERS or
                     f.key in ("hf_base_url", "ollama_base_url")})
    return rows


async def _test(settings, key: str) -> dict:
    import httpx
    v = _value(settings, key)
    if not v:
        return {"ok": False, "detail": "not set"}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            if key == "hf_base_url":
                tok = _value(settings, "hf_api_token")
                r = await c.get(v.rstrip("/") + "/health", headers={"Authorization": f"Bearer {tok}"} if tok else {})
            elif key == "ollama_base_url":
                r = await c.get(v.rstrip("/") + "/api/tags")
            else:
                method, url, how = TESTERS[key]
                headers, params = {}, {}
                if how == "bearer":
                    headers["Authorization"] = f"Bearer {v}"
                elif how == "anthropic":
                    headers.update({"x-api-key": v, "anthropic-version": "2023-06-01"})
                elif how == "query_key":
                    params["key"] = v
                elif how == "query_apikey":
                    params["apikey"] = v
                elif how == "path":
                    url = url.format(v=v)
                r = await c.request(method, url, headers=headers, params=params)
        ok = r.status_code == 200 and not (isinstance(r.json(), dict) and r.json().get("status") == "error") \
            if "json" in r.headers.get("content-type", "") else r.status_code == 200
        detail = f"HTTP {r.status_code}"
        if key == "hf_api_token" and ok:
            detail += f" · logged in as {r.json().get('name')}"
        if key == "telegram_bot_token" and ok:
            detail += f" · bot @{(r.json().get('result') or {}).get('username')}"
        if key == "hf_base_url" and ok:
            j = r.json()
            detail += f" · model {j.get('model')} · ready {j.get('ready')}"
        return {"ok": ok, "detail": detail if ok else f"{detail}: {r.text[:120]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)[:160]}


async def _admin(request):
    from scheduler import settings_page
    return await settings_page._admin(request)         # same login as /settings, looked up per request


def register(app: web.Application, runner) -> None:

    async def page(request):
        return web.Response(text=PAGE, content_type="text/html")

    async def api(request):
        denied = await _admin(request)
        if denied is not None:
            return denied
        from config import secret_box
        from config.settings import settings
        return web.json_response({"encrypted_storage": secret_box.available(),
                                  "keys": status_rows(settings, await _stored())})

    async def save(request):
        denied = await _admin(request)
        if denied is not None:
            return denied
        from config.overrides import BY_KEY, apply, coerce, seal_secrets
        from config.settings import settings
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        body = await request.json()
        key, value = body.get("key"), body.get("value")
        f = BY_KEY.get(key)
        if f is None or f.group != "keys":
            return web.json_response({"error": "unknown key"}, status=400)
        try:
            value = coerce(f, value)
        except (TypeError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=400)
        if f.kind == "secret" and not value:
            return web.json_response({"error": "paste a value, or use Clear"}, status=400)
        async with AsyncSessionFactory() as session:
            await Repository(session).save_app_settings(seal_secrets({key: value}))
        apply(settings, {key: value})
        from scheduler import cache
        cache.invalidate("app_settings_db")
        if hasattr(runner, "on_settings_changed"):
            runner.on_settings_changed([key])
        return web.json_response({"ok": True})

    async def clear(request):
        denied = await _admin(request)
        if denied is not None:
            return denied
        from config.overrides import BY_KEY
        from config.settings import Settings, settings
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        key = (await request.json()).get("key")
        f = BY_KEY.get(key)
        if f is None or f.group != "keys":
            return web.json_response({"error": "unknown key"}, status=400)
        async with AsyncSessionFactory() as session:              # empty = "use .env"; rows are never deleted
            await Repository(session).save_app_settings({key: ""})
        setattr(settings, key, getattr(Settings(), key))
        from scheduler import cache
        cache.invalidate("app_settings_db")
        return web.json_response({"ok": True})

    async def test(request):
        denied = await _admin(request)
        if denied is not None:
            return denied
        from config.settings import settings
        return web.json_response(await _test(settings, (await request.json()).get("key", "")))

    async def reveal(request):
        """One key's value, only when asked, only for the admin; the log records which key, never the value."""
        denied = await _admin(request)
        if denied is not None:
            return denied
        import structlog

        from config.overrides import BY_KEY
        from config.settings import settings
        key = (await request.json()).get("key")
        f = BY_KEY.get(key)
        if f is None or f.group != "keys":
            return web.json_response({"error": "unknown key"}, status=400)
        structlog.get_logger().info("key_revealed", key=key, ip=request.remote)
        resp = web.json_response({"key": key, "value": _value(settings, key)})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def hf_space(request):
        """Upload huggingface_space/ to the saved Space with the saved token; private, CPU Basic, address saved."""
        denied = await _admin(request)
        if denied is not None:
            return denied
        from scripts.hf_space_deploy import deploy_from_saved_settings
        try:
            out = await deploy_from_saved_settings()
        except Exception as e:  # noqa: BLE001
            return web.json_response({"error": str(e)[:200]}, status=502)
        return web.json_response(out, status=400 if "error" in out else 200)

    async def import_env(request):
        denied = await _admin(request)
        if denied is not None:
            return denied
        from config.overrides import seal_secrets
        from storage.database import AsyncSessionFactory
        from storage.repository import Repository
        stored = await _stored()
        moved = {}
        for f in _keys():
            if stored.get(f.key):
                continue
            env = _env_value(f.key)
            if env:
                moved[f.key] = env
        if moved:
            async with AsyncSessionFactory() as session:
                await Repository(session).save_app_settings(seal_secrets(moved))
            from scheduler import cache
            cache.invalidate("app_settings_db")
        return web.json_response({"ok": True, "moved": sorted(moved)})

    app.router.add_get("/keys", page)
    app.router.add_get("/api/keys", api)
    app.router.add_post("/api/keys/save", save)
    app.router.add_post("/api/keys/clear", clear)
    app.router.add_post("/api/keys/test", test)
    app.router.add_post("/api/keys/import-env", import_env)
    app.router.add_post("/api/keys/reveal", reveal)
    app.router.add_post("/api/keys/hf-space-deploy", hf_space)


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Keys</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--fg:#e2e8f0;--mut:#94a3b8;--line:#334155;--ok:#22c55e;--bad:#ef4444;--warn:#f59e0b;--acc:#0ea5e9}
@media (prefers-color-scheme:light){:root{--bg:#f4f6f9;--card:#fff;--fg:#0f172a;--mut:#475569;--line:#dbe1e8;--ok:#15803d;--bad:#b91c1c;--warn:#b45309;--acc:#0369a1}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,sans-serif;padding:16px;max-width:820px;margin:auto}
h1{font-size:20px;margin:4px 0}.sub{color:var(--mut);font-size:13px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:10px 0}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap}.pill{font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}input{width:100%;min-height:42px;font-size:16px;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
button{min-height:40px;padding:6px 14px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg);font:inherit;cursor:pointer}
button.p{background:var(--acc);border-color:var(--acc);color:#fff}.acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}a{color:var(--acc)}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:13px;word-break:break-all;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px;margin-top:8px;user-select:all}
</style></head><body>
<div class="sub"><a href="/settings">← Settings</a></div>
<h1>Keys &amp; tokens</h1>
<div class="sub" id="top">Loading…</div>
<div class="card" id="login" hidden><b>Admin login</b><div class="sub">Same password as /settings.</div>
 <div class="acts"><input id="pw" type="password" autocomplete="current-password" placeholder="Admin password"><button class="p" onclick="login()">Log in</button></div></div>
<div id="list"></div>
<script>
const tok=()=>sessionStorage.getItem('settings_token')||'';
const H=()=>({'Content-Type':'application/json','X-Settings-Token':tok()});
const esc=t=>String(t??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function login(){const r=await fetch('/api/settings/auth/login',{method:'POST',headers:H(),body:JSON.stringify({password:document.getElementById('pw').value})});
 const d=await r.json().catch(()=>({}));if(d.ok){sessionStorage.setItem('settings_token',d.token);load()}else alert('Wrong password')}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:H(),body:JSON.stringify(body||{})});return r.json().catch(()=>({error:'HTTP '+r.status}))}
function col(w){return /encrypted/.test(w)?'ok':/plain|cannot|\\.env/.test(w)?'warn':/not set/.test(w)?'bad':''}
async function load(){const r=await fetch('/api/keys',{headers:H()});if(r.status===401||r.status===403||r.status===503){document.getElementById('login').hidden=false;document.getElementById('top').textContent='Log in to see your keys.';return}
 document.getElementById('login').hidden=true;const d=await r.json();
 const envOnly=d.keys.filter(k=>/\\.env only/.test(k.where)).length;
 document.getElementById('top').innerHTML=(d.encrypted_storage?'<span class="ok">Keys are stored encrypted.</span>':'<span class="warn">Encryption is not set up on the server (SECRETS_MASTER_KEY missing): keys are stored in plain text.</span>')+
  ` The database wins over .env. ${envOnly?`<div class="acts"><button class="p" onclick="imp()">Move ${envOnly} key${envOnly>1?'s':''} from .env into the database</button></div>`:''}`;
 document.getElementById('list').innerHTML=d.keys.map(k=>`<div class="card" id="c-${k.key}"><div class="row"><b>${esc(k.label)}</b><span class="pill ${col(k.where)}">${esc(k.where)}</span></div>
  <div class="sub">${esc(k.help)}</div><div class="sub">${k.kind==='secret'?(k.is_set?'current: '+esc(k.hint):'no value'):(k.value?'current: '+esc(k.value):'no value')}${k.env_differs?' · <span class="warn">.env has a different value</span>':''}</div>
  <div class="acts"><input id="v-${k.key}" ${k.kind==='secret'?'type="password" autocomplete="off"':''} placeholder="${k.kind==='secret'?'Paste new value':'New value'}">
  <button class="p" onclick="save('${k.key}')">Save</button>${k.key==='hf_space_repo'&&k.value?`<button onclick="hfDeploy()">Update Space</button>`:''}${k.testable?`<button onclick="test('${k.key}')">Test</button>`:''}${k.is_set?`<button onclick="show('${k.key}')" id="s-${k.key}">Show</button><button onclick="copyKey('${k.key}')">Copy</button><button onclick="clr('${k.key}')">Clear</button>`:''}</div>
  <div class="mono" id="r-${k.key}" hidden></div><div class="sub" id="t-${k.key}"></div></div>`).join('')}
const _shown={};
async function reveal(key){if(_shown[key])return _shown[key];const d=await post('/api/keys/reveal',{key});if(d.error){alert(d.error);return null}_shown[key]=d.value;setTimeout(()=>{delete _shown[key];const el=document.getElementById('r-'+key);if(el){el.hidden=true;el.textContent=''}const b=document.getElementById('s-'+key);if(b)b.textContent='Show'},60000);return d.value}
async function show(key){const el=document.getElementById('r-'+key),b=document.getElementById('s-'+key);if(!el.hidden){el.hidden=true;el.textContent='';b.textContent='Show';return}
 const v=await reveal(key);if(v==null)return;el.textContent=v;el.hidden=false;b.textContent='Hide'}
async function copyKey(key){const v=await reveal(key);if(v==null)return;const t=document.getElementById('t-'+key);
 try{await navigator.clipboard.writeText(v);t.innerHTML='<span class="ok">Copied.</span> Hidden again in 60 s.'}
 catch(e){const el=document.getElementById('r-'+key);el.textContent=v;el.hidden=false;const r=document.createRange();r.selectNodeContents(el);const s=getSelection();s.removeAllRanges();s.addRange(r);t.textContent='Selected: tap Copy in the menu.'}}
async function save(key){const v=document.getElementById('v-'+key).value.trim();if(!v){alert('Paste a value first');return}
 const d=await post('/api/keys/save',{key,value:v});if(d.ok){load();setTimeout(()=>test(key),600)}else alert(d.error||'Save failed')}
async function test(key){const el=document.getElementById('t-'+key);if(!el)return;el.textContent='Testing…';const d=await post('/api/keys/test',{key});el.innerHTML=`<span class="${d.ok?'ok':'bad'}">${d.ok?'✔ works':'✖ failed'}</span> ${esc(d.detail||d.error||'')}`}
async function clr(key){if(!confirmBox(key))return;const d=await post('/api/keys/clear',{key});if(d.ok)load();else alert(d.error)}
function confirmBox(key){const el=document.getElementById('t-'+key);if(el.dataset.arm==='1'){el.dataset.arm='';return true}el.dataset.arm='1';el.innerHTML='<span class="warn">Tap Clear again to stop using this key.</span>';return false}
async function hfDeploy(){const el=document.getElementById('t-hf_space_repo');el.textContent='Uploading to your Space (about 30 s)…';
 const d=await post('/api/keys/hf-space-deploy');el.innerHTML=d.error?`<span class="bad">✖ ${esc(d.error)}</span>`:
 `<span class="ok">✔ uploaded ${esc((d.uploaded||[]).join(', '))}</span> · private ${esc(d.private)} · hardware ${esc(d.hardware)} · address saved: ${esc(d.url)} · build stage ${esc(d.stage)}. The model downloads on first start (a few minutes), then tap Test on the Space URL.`;load()}
async function imp(){const d=await post('/api/keys/import-env');if(d.ok){load();document.getElementById('top').insertAdjacentHTML('beforeend',`<div class="ok">Moved: ${esc((d.moved||[]).join(', ')||'nothing')}</div>`)}else alert(d.error)}
load();
</script></body></html>"""
