"""
/world: the World news page (owner 31 Oct 2026: "a,b and c all design with switch, and deploy"). A 3D Earth you can
spin and zoom, coloured by how much news each country has, with event pins, a headline feed (source under each
headline) and a ticker, in three looks the viewer switches between (remembered in the browser). Reads
/api/portal/world every 60 s. Labels and pages only, never trades.

Class names start with "w-": this page also loads portal.css, whose bare classes (.pos, .coin, .hist ...) broke Market
moves the same day.
"""
from __future__ import annotations

from aiohttp import web

GLOBE_JS = "https://cdn.jsdelivr.net/npm/globe.gl@2.27.2/dist/globe.gl.min.js"
# echarts 4 ships world country shapes as a script that calls echarts.registerMap; a stub catches them (no echarts)
WORLD_JS = "https://cdn.jsdelivr.net/npm/echarts@4.9.0/map/js/world.js"

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>World — Command</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600&family=IBM+Plex+Sans:wght@400;500&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
body.w-body{
  --w-void:#04070d;--w-glass:rgba(14,24,40,.42);--w-edge:rgba(160,210,255,.16);--w-ink:#e8f1ff;--w-dim:#8ea3bf;
  --w-a1:#5ee0ff;--w-a2:#9d7bff;--w-hot:#ff5d73;--w-warm:#ffb547;--w-calm:#45e3a3;
  --w-display:"Chakra Petch",ui-sans-serif,system-ui,sans-serif;--w-body-f:"IBM Plex Sans",ui-sans-serif,system-ui,sans-serif;
  --w-data:"JetBrains Mono",ui-monospace,Menlo,monospace;
  margin:0;height:100vh;display:flex;flex-direction:column;overflow:hidden;background:var(--w-void)!important;color:var(--w-ink)!important;
  font-family:var(--w-body-f)!important;color-scheme:dark}
body.w-body.w-look-b{--w-a1:#ff8de0;--w-a2:#7cf3ff;--w-glass:rgba(30,12,40,.40);--w-edge:rgba(255,170,240,.18)}
body.w-body.w-look-c{--w-a1:#c8ff6a;--w-a2:#4dd8c8;--w-glass:rgba(10,22,20,.55);--w-edge:rgba(200,255,140,.16);--w-void:#030807}
.w-world{flex:1;position:relative;min-height:0}
#w-stage{position:absolute;inset:0}
.w-hud{position:absolute;inset:0;pointer-events:none;display:grid;grid-template-columns:minmax(0,320px) 1fr minmax(0,400px);
  grid-template-rows:auto minmax(0,1fr) auto;gap:12px;padding:12px max(16px,env(safe-area-inset-right,0px)) 12px max(16px,env(safe-area-inset-left,0px))}
.w-glass{pointer-events:auto;background:var(--w-glass);border:1px solid var(--w-edge);border-radius:14px;
  backdrop-filter:blur(14px) saturate(140%);-webkit-backdrop-filter:blur(14px) saturate(140%);box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 20px 50px rgba(0,0,0,.35)}
body.w-look-c .w-glass{border-radius:4px}
.w-top{grid-column:1/-1;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;padding:8px 12px}
.w-brand{font:600 13px var(--w-display);letter-spacing:.14em;text-transform:uppercase;display:flex;align-items:center;gap:10px}
.w-dot{width:8px;height:8px;border-radius:50%;background:var(--w-calm);box-shadow:0 0 12px var(--w-calm);animation:w-pulse 2s infinite}
@keyframes w-pulse{50%{opacity:.35}}
.w-meta{font:12px var(--w-data);color:var(--w-dim)}
.w-looks{display:flex;gap:6px;flex-wrap:wrap}
.w-looks button{font:500 12px var(--w-data);letter-spacing:.06em;color:var(--w-dim);background:transparent;border:1px solid var(--w-edge);
  border-radius:999px;padding:0 12px;min-height:36px;cursor:pointer}
.w-looks button[aria-pressed="true"]{color:var(--w-void);background:var(--w-a1);border-color:var(--w-a1)}
.w-looks button:focus-visible,.w-cty:focus-visible,.w-item:focus-visible{outline:2px solid var(--w-a1);outline-offset:2px}
.w-left,.w-right{display:flex;flex-direction:column;gap:12px;min-height:0}
.w-panel{padding:14px 16px;min-width:0}
.w-eyebrow{font:500 11px var(--w-data);letter-spacing:.16em;text-transform:uppercase;color:var(--w-dim);margin:0 0 8px}
.w-big{font:600 42px/1 var(--w-display);margin:0;color:var(--w-a1);text-shadow:0 0 24px color-mix(in srgb,var(--w-a1) 45%,transparent)}
.w-big small{font:400 13px var(--w-body-f);color:var(--w-dim);margin-left:8px;text-shadow:none}
.w-levels{display:flex;gap:6px;margin-top:12px;flex-wrap:wrap}
.w-levels span{font:12px var(--w-data);padding:4px 8px;border-radius:6px;border:1px solid var(--w-edge)}
.w-ctys{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;max-height:260px;overflow:auto}
.w-cty{all:unset;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 10px;border-radius:9px;
  background:rgba(255,255,255,.03);font:13px var(--w-body-f);border:1px solid transparent}
.w-cty:hover{border-color:var(--w-edge);background:rgba(255,255,255,.07)}
.w-cty i{display:block;height:4px;border-radius:2px;background:var(--w-hot);min-width:4px}
.w-feed{flex:1;min-height:0;overflow:auto;padding:4px 6px 8px}
.w-item{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 12px;padding:11px 10px;border-radius:10px;cursor:pointer}
.w-item:hover{background:rgba(255,255,255,.05)}
body.w-look-b .w-item{border:1px solid var(--w-edge);margin:8px 2px;background:linear-gradient(135deg,rgba(255,141,224,.08),rgba(124,243,255,.05))}
.w-sev{grid-row:span 2;align-self:start;font:600 12px var(--w-display);width:34px;height:34px;display:grid;place-items:center;border-radius:9px;border:1px solid currentColor}
body.w-look-c .w-sev{border-radius:3px}
.w-l5,.w-l4{color:var(--w-hot)} .w-l3{color:var(--w-warm)} .w-l2,.w-l1{color:var(--w-dim)}
.w-hl{font:500 14px/1.35 var(--w-body-f);margin:0;text-wrap:balance;color:var(--w-ink)}
body.w-look-c .w-hl{font:400 13px/1.4 var(--w-data)}
.w-hl a{color:inherit;text-decoration:none} .w-hl a:hover{text-decoration:underline}
.w-src{font:11px var(--w-data);color:var(--w-dim);letter-spacing:.03em}
.w-src em{font-style:normal;color:var(--w-a1)}
.w-closed{opacity:.55}
.w-bottom{grid-column:1/-1;overflow:hidden;padding:8px 0}
.w-ticker{display:flex;gap:40px;white-space:nowrap;font:12px var(--w-data);color:var(--w-dim);animation:w-run 70s linear infinite;width:max-content}
.w-ticker b{color:var(--w-ink);font-weight:500}
@keyframes w-run{to{transform:translateX(-50%)}}
.w-center{display:flex;align-items:flex-end;justify-content:center}
.w-hint{font:11px var(--w-data);color:var(--w-dim);letter-spacing:.12em;text-transform:uppercase;padding:8px 12px}
.w-empty{font:13px var(--w-data);color:var(--w-dim);padding:16px}
.w-note{font:11px var(--w-data);color:var(--w-dim);margin:10px 0 0}
@media (max-width:900px){
  body.w-body{height:auto;overflow:auto}
  .w-world{flex:none}
  #w-stage{position:relative;height:56vh}
  .w-hud{position:relative;grid-template-columns:minmax(0,1fr);grid-template-rows:auto}
  .w-center{display:none}
  .w-feed{max-height:none}
}
@media (prefers-reduced-motion:reduce){.w-ticker,.w-dot{animation:none}}
</style>
</head>
<body class="w-body cmd-tool">
<div class="w-world">
  <div id="w-stage" role="img" aria-label="3D Earth: drag to rotate, scroll or pinch to zoom"></div>
  <div class="w-hud">
    <div class="w-top w-glass">
      <div class="w-brand"><span class="w-dot" aria-hidden="true"></span>World news <span class="w-meta" id="w-updated">loading…</span></div>
      <div class="w-looks" role="group" aria-label="Look">
        <button type="button" id="w-look-a" aria-pressed="true">A · Orbital glass</button>
        <button type="button" id="w-look-b" aria-pressed="false">B · Holo neon</button>
        <button type="button" id="w-look-c" aria-pressed="false">C · Ops terminal</button>
      </div>
    </div>
    <div class="w-left">
      <section class="w-glass w-panel">
        <p class="w-eyebrow">Open world events</p>
        <p class="w-big" id="w-open">–<small>tracked by the news engine</small></p>
        <div class="w-levels" id="w-levels"></div>
      </section>
      <section class="w-glass w-panel">
        <p class="w-eyebrow">News heat by country · tap to fly</p>
        <div class="w-ctys" id="w-ctys"><div class="w-empty">No country activity yet.</div></div>
        <p class="w-note">Heat = severity of a country's open events. It shows how much is happening, not whether it is good or bad.</p>
      </section>
    </div>
    <div class="w-center"><span class="w-hint w-glass">Drag to spin · scroll to zoom</span></div>
    <section class="w-right w-glass" aria-label="Headlines">
      <div style="padding:14px 16px 0"><p class="w-eyebrow">Headlines · open first, by level</p></div>
      <div class="w-feed" id="w-feed"><div class="w-empty">Loading headlines…</div></div>
    </section>
    <div class="w-bottom w-glass"><div class="w-ticker" id="w-ticker"></div></div>
  </div>
</div>
<script>window.echarts={registerMap:function(n,g){window.W_WORLD=g}};</script>
<script src="__WORLD_JS__"></script>
<script src="__GLOBE_JS__"></script>
<script>
(function(){
  "use strict";
  // country centroids (lat, lng) for the engine's 20 countries + names in the map's shapes
  var C={US:["United States",39,-98],CN:["China",35,104],EU:["Euro area",50,10],GB:["United Kingdom",54,-2],JP:["Japan",36,138],
    IN:["India",22,79],RU:["Russia",60,90],UA:["Ukraine",49,32],IL:["Israel",31.5,35],KR:["South Korea",36.5,128],
    AE:["United Arab Emirates",24,54],SG:["Singapore",1.35,103.8],CH:["Switzerland",46.8,8.2],TR:["Turkey",39,35],
    BR:["Brazil",-10,-52],MX:["Mexico",23,-102],CA:["Canada",57,-101],AU:["Australia",-25,134],SA:["Saudi Arabia",24,45],TW:["Taiwan",23.7,121]};
  var SHAPE={US:["United States"],CN:["China"],GB:["United Kingdom"],JP:["Japan"],IN:["India"],RU:["Russia"],UA:["Ukraine"],
    IL:["Israel"],KR:["Korea"],AE:["United Arab Emirates"],SG:["Singapore"],CH:["Switzerland"],TR:["Turkey"],BR:["Brazil"],
    MX:["Mexico"],CA:["Canada"],AU:["Australia"],SA:["Saudi Arabia"],TW:["Taiwan"],
    EU:["Germany","France","Italy","Spain","Netherlands","Belgium","Austria","Portugal","Ireland","Finland","Greece","Slovakia",
        "Slovenia","Lithuania","Latvia","Estonia","Luxembourg","Croatia","Cyprus","Malta"]};
  var BYSHAPE={};Object.keys(SHAPE).forEach(function(k){SHAPE[k].forEach(function(n){BYSHAPE[n]=k})});
  var LEVEL_ALT=[0,0.02,0.04,0.07,0.11,0.16];
  var state={events:[],heat:{}},G=null;
  function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]})}
  function css(n){return getComputedStyle(document.body).getPropertyValue(n).trim()}
  function lvColor(l){return l>=4?css("--w-hot"):l===3?css("--w-warm"):css("--w-a1")}
  function hhmm(iso){return iso?iso.slice(5,10).replace("-","/")+" "+iso.slice(11,16)+" UTC":""}
  function fly(lat,lng,alt){if(G)G.pointOfView({lat:lat,lng:lng,altitude:alt||1.4},1400)}
  function decodeRing(str,off){var out=[],px=off[0],py=off[1];for(var i=0;i<str.length;i+=2){var x=str.charCodeAt(i)-64,y=str.charCodeAt(i+1)-64;
    x=(x>>1)^(-(x&1));y=(y>>1)^(-(y&1));x+=px;y+=py;px=x;py=y;out.push([x/1024,y/1024])}return out}
  function shapes(){var w=window.W_WORLD;if(!w||!w.features)return[];return w.features.map(function(f){var g=f.geometry;
    if(w.UTF8Encoding&&g.encodeOffsets){var c=g.type==="Polygon"?g.coordinates.map(function(r,i){return decodeRing(r,g.encodeOffsets[i])})
      :g.coordinates.map(function(p,j){return p.map(function(r,i){return decodeRing(r,g.encodeOffsets[j][i])})});
      return{type:"Feature",properties:f.properties,geometry:{type:g.type,coordinates:c}}}return f})}
  function heatColor(f){var k=BYSHAPE[f.properties.name],h=k?state.heat[k]:undefined;
    if(h===undefined)return "rgba(140,170,210,.28)";var c=h>=0.66?css("--w-hot"):h>=0.33?css("--w-warm"):css("--w-calm");return c}
  function pins(){var out=[];state.events.forEach(function(e){if(e.status!=="open")return;(e.countries||[]).forEach(function(k,i){
    var c=C[k];if(c)out.push({e:e,lat:c[1]+i*0.6+(e.id%5)*0.5,lng:c[2]+(e.id%7)*0.6})})});return out}
  function tip(e){return "<div style='max-width:280px;font:12px IBM Plex Sans,sans-serif;padding:8px 10px;background:rgba(4,8,16,.9);border:1px solid rgba(160,210,255,.25);border-radius:8px;color:#e8f1ff'><b>L"+e.level+"</b> "+esc(e.title)+"<br><span style='opacity:.7;font-family:JetBrains Mono,monospace'>"+esc(e.source)+" · "+esc(hhmm(e.at))+"</span></div>"}
  function paintGlobe(){if(!G)return;var p=pins();
    G.hexPolygonColor(heatColor).pointsData(p).pointColor(function(d){return lvColor(d.e.level)})
     .ringsData(p.filter(function(d){return d.e.level>=3})).atmosphereColor(css("--w-a1"))}
  function build(){if(typeof Globe!=="function"){document.getElementById("w-stage").innerHTML="<p class='w-empty'>The 3D globe could not load (blocked script?). Headlines still update.</p>";return}
    var el=document.getElementById("w-stage");
    G=Globe()(el).backgroundColor("rgba(0,0,0,0)").showAtmosphere(true).atmosphereAltitude(0.22)
      .hexPolygonsData(shapes()).hexPolygonResolution(3).hexPolygonMargin(0.35).hexPolygonColor(heatColor)
      .hexPolygonLabel(function(f){var k=BYSHAPE[f.properties.name],h=k?state.heat[k]:undefined;return "<div style='font:12px JetBrains Mono,monospace;padding:6px 8px;background:rgba(4,8,16,.88);border:1px solid rgba(160,210,255,.25);border-radius:6px;color:#e8f1ff'>"+esc(f.properties.name)+(h!==undefined?" · news heat "+Math.round(h*100)+"%":"")+"</div>"})
      .pointLat("lat").pointLng("lng").pointAltitude(function(d){return LEVEL_ALT[d.e.level]||0.03}).pointRadius(0.45).pointLabel(function(d){return tip(d.e)})
      .onPointClick(function(d){if(d.e.url)window.open(d.e.url,"_blank","noopener")})
      .ringLat("lat").ringLng("lng").ringMaxRadius(6).ringPropagationSpeed(2).ringRepeatPeriod(1400)
      .ringColor(function(d){var c=lvColor(d.e.level);return function(t){return c+Math.round((1-t)*255).toString(16).padStart(2,"0")}});
    var m=G.globeMaterial();if(m&&m.color){m.color.set("#071226");if(m.emissive)m.emissive.set("#030a18")}
    G.controls().autoRotate=!matchMedia("(prefers-reduced-motion: reduce)").matches;G.controls().autoRotateSpeed=0.35;
    G.pointOfView({lat:30,lng:40,altitude:2.3});
    function size(){G.width(el.clientWidth).height(el.clientHeight)}size();addEventListener("resize",size);paintGlobe()}
  function render(d){
    if(!d.enabled){document.getElementById("w-feed").innerHTML="<div class='w-empty'>"+esc(d.message||"Off")+"</div>";return}
    state.events=d.events||[];state.heat=d.heat||{};
    document.getElementById("w-open").firstChild.nodeValue=String(d.open);
    var lv={};state.events.forEach(function(e){if(e.status==="open")lv[e.level]=(lv[e.level]||0)+1});
    document.getElementById("w-levels").innerHTML=[5,4,3,2,1].filter(function(l){return lv[l]}).map(function(l){return "<span class='w-l"+l+"'>L"+l+" · "+lv[l]+"</span>"}).join("");
    var ks=Object.keys(state.heat).sort(function(a,b){return state.heat[b]-state.heat[a]});
    var cEl=document.getElementById("w-ctys");cEl.innerHTML=ks.length?"":"<div class='w-empty'>No country activity yet.</div>";
    ks.forEach(function(k){var c=C[k];var b=document.createElement("button");b.type="button";b.className="w-cty";
      b.innerHTML="<span>"+esc(c?c[0]:k)+"</span><i style='width:"+Math.round(8+state.heat[k]*52)+"px'></i>";
      b.onclick=function(){if(c)fly(c[1],c[2])};cEl.appendChild(b)});
    var f=document.getElementById("w-feed");f.innerHTML=state.events.length?"":"<div class='w-empty'>No events yet.</div>";
    state.events.slice(0,60).forEach(function(e){var d2=document.createElement("div");d2.className="w-item"+(e.status==="open"?"":" w-closed");d2.tabIndex=0;
      var t=e.url?"<a href='"+esc(e.url)+"' target='_blank' rel='noopener'>"+esc(e.title)+"</a>":esc(e.title);
      d2.innerHTML="<span class='w-sev w-l"+e.level+"'>L"+e.level+"</span><p class='w-hl'>"+t+"</p><span class='w-src'><em>"+esc(e.source)+"</em> · "+esc(hhmm(e.at))+" · "+esc(String(e.type).replace("_"," "))+((e.countries||[]).length?" · "+esc(e.countries.join(", ")):"")+(e.status==="open"?"":" · closed")+"</span>";
      var k=(e.countries||[])[0];if(k&&C[k]){d2.onclick=function(ev){if(ev.target.tagName!=="A")fly(C[k][1],C[k][2])};d2.onkeydown=function(ev){if(ev.key==="Enter")fly(C[k][1],C[k][2])}}
      f.appendChild(d2)});
    var tk=(d.ticker||[]).map(function(t){return "<span>"+esc(t.k)+" <b>"+esc(t.v)+"</b></span>"}).join("");
    document.getElementById("w-ticker").innerHTML=tk?tk+tk:"<span>Waiting for odds and market data…</span>";
    document.getElementById("w-updated").textContent="updated "+new Date().toISOString().slice(11,16)+" UTC";
    paintGlobe()}
  function load(){fetch("/api/portal/world",{cache:"no-store"}).then(function(r){if(!r.ok)throw new Error("server returned "+r.status);return r.json()})
    .then(render).catch(function(e){document.getElementById("w-updated").textContent="update failed: "+e.message})}
  function setLook(k){document.body.classList.remove("w-look-b","w-look-c");if(k!=="a")document.body.classList.add("w-look-"+k);
    ["a","b","c"].forEach(function(x){document.getElementById("w-look-"+x).setAttribute("aria-pressed",String(x===k))});
    try{localStorage.setItem("w-look",k)}catch(e){}paintGlobe()}
  ["a","b","c"].forEach(function(x){document.getElementById("w-look-"+x).onclick=function(){setLook(x)}});
  var saved="a";try{saved=localStorage.getItem("w-look")||"a"}catch(e){}
  setLook(saved);build();load();setInterval(load,60000);
})();
</script>
</body>
</html>"""


async def world_page(request: web.Request) -> web.Response:
    from scheduler.health import _THEME_SNIPPET
    from scheduler.portal import command_tool_head_extra

    html = _HTML.replace("__WORLD_JS__", WORLD_JS).replace("__GLOBE_JS__", GLOBE_JS)
    return web.Response(text=html.replace("</head>", _THEME_SNIPPET + command_tool_head_extra() + "</head>"),
                        content_type="text/html")
