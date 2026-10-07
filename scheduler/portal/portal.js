/* Portal: Brutal Command. Spec: docs/PORTAL_REVAMP.md
   Plain JS, no build. Every figure comes from the engine's read APIs:
     /api/crypto/coins  /api/swing  /api/llm/budget  /api/status
     /api/portal/candles  /api/portal/signals  /api/portal/meta
   Motion is decoration only; nothing on the page is simulated. */
(function () {
  "use strict";

  var HUBS = ["command", "book", "evidence", "system"];
  // Old single-page dashboard anchors (/#paper etc.) now live at /classic.
  var LEGACY_HASH = ["#dashboard", "#crypto", "#mirror", "#paper", "#guard", "#accuracy", "#historic", "#watchlist", "#simulator"];
  var AGENTS = [
    ["Market feed", "Binance / CoinDCX prices and candles"],
    ["Detectors", "Five 15m families · shadow only"],
    ["Market filter", "Skips swings when BTC vol is in its top third"],
    ["Swing scanner", "Validated 4h / 8h breakout strategies"],
    ["Risk sizer", "Risk per trade · stop 3×ATR · 8% cap"],
    ["Paper exec", "Target 3R · time limit · paper fills"],
    ["Outcome replay", "Every swing signal replayed on 1h bars"],
    ["AI analyst", "Briefings within free-tier budgets"]
  ];
  var ASKS = ["Is the filter open?", "Why the last skip?", "What is my risk?", "Is anything live?"];
  var EVIDENCE_LINKS = [
    ["/audit", "Audit", "every signal and its outcome"],
    ["/classic#accuracy", "Accuracy", "win rate by detector family"],
    ["/chart", "Chart", "candles with signal overlays"],
    ["/classic#historic", "Historic data", "archive older than 7 days"],
    ["/moves", "Market moves", "what moved and why"],
    ["/predict", "Price outlook", "multi-horizon forecasts"],
    ["/pipeline", "Pipeline", "multi-year backtest monitor"],
    ["/v2", "v2 shadow", "next strategy set, shadow only"],
    ["/journal", "Journal", "owner's trade journal"],
    ["/classic#mirror", "Mirror", "mirror review of signals"]
  ];
  var SYSTEM_LINKS = [
    ["/settings", "Settings", "engine and paper settings"],
    ["/keys", "Keys", "API keys and providers"],
    ["/classic#watchlist", "Watchlist", "symbols the collectors track"],
    ["/classic#guard", "Session guard", "loss limits and pauses"],
    ["/classic#simulator", "Simulator", "paper cycle controls"],
    ["/data", "Data", "tables and row counts"],
    ["/api-docs", "API list", "every endpoint"],
    ["/api/debug/binance", "Diagnostics", "exchange connectivity probe"],
    ["/classic", "Classic dashboard", "the previous one-page app"]
  ];

  var S = {
    hub: "command", sel: null, coins: [], swing: null, signals: [], signalsErr: "", meta: null, budget: null, status: null,
    candles: [], candleSym: null, candleErr: "", form: null, stage: 0, ask: -1,
    seen: null, traceId: null, traceFull: "", traceN: 0, loaded: {}
  };

  // ---------- helpers ----------
  function $(id) { return document.getElementById(id); }
  function esc(v) { return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function pad(n) { return String(n).padStart(2, "0"); }
  function hms(d) { return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()); }
  function dur(ms) { var s = Math.max(0, Math.floor(ms / 1000)); return pad(Math.floor(s / 3600)) + ":" + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60); }
  function px(p) {
    if (p == null || !isFinite(p)) return "—";
    var d = p >= 10 ? 2 : p >= 1 ? 3 : p >= 0.01 ? 5 : 7;
    return Number(p).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function pct(x, d) { if (x == null || !isFinite(x)) return "—"; return (x >= 0 ? "+" : "") + Number(x).toFixed(d == null ? 2 : d) + "%"; }
  function rr(x) { return x == null || !isFinite(x) ? "—" : (x >= 0 ? "+" : "") + Number(x).toFixed(2) + "R"; }
  function base(sym) { return String(sym || "").toUpperCase().replace(/USDT$|INR$/, ""); }
  function ago(iso) {
    if (!iso) return "—";
    var m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    return m < 1 ? "just now" : m < 60 ? m + "m ago" : m < 2880 ? Math.round(m / 60) + "h ago" : Math.round(m / 1440) + "d ago";
  }
  function stratName(s) { return String(s || "").replace(/^swing_/, "").replace(/[_@:]/g, " "); }
  function coinBy(sym) {
    var u = String(sym || "").toUpperCase();
    for (var i = 0; i < S.coins.length; i++) if (S.coins[i].symbol === u) return S.coins[i];
    return null;
  }
  function getJSON(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return {}; }).then(function (b) { throw new Error(b.error || ("HTTP " + r.status)); });
      return r.json();
    });
  }

  // ---------- palette ----------
  function setPalette(p) {
    document.documentElement.setAttribute("data-palette", p);
    try { localStorage.setItem("portal-palette", p); } catch (e) { /* private mode */ }
    document.querySelectorAll(".sw").forEach(function (b) { var on = b.dataset.p === p; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); });
  }

  // ---------- routing ----------
  function show(hub) {
    if (HUBS.indexOf(hub) < 0) hub = "command";
    S.hub = hub;
    document.querySelectorAll("[data-view]").forEach(function (s) { s.hidden = s.dataset.view !== hub; });
    document.querySelectorAll(".tab").forEach(function (t) {
      var on = t.dataset.hub === hub; t.classList.toggle("on", on);
      if (on) t.setAttribute("aria-current", "page"); else t.removeAttribute("aria-current");
    });
    var word = hub.toUpperCase();
    $("logoWord").textContent = word; $("logoWord").setAttribute("data-text", word);
    document.title = "Signal Engine · " + hub.charAt(0).toUpperCase() + hub.slice(1);
    renderAll();
  }

  // ---------- data ----------
  function loadCoins() {
    return getJSON("/api/crypto/coins").then(function (c) {
      c = (Array.isArray(c) ? c : []).filter(function (x) { return x.price > 0; });
      c.sort(function (a, b) { return (b.volume_24h || 0) - (a.volume_24h || 0); });
      var changed = c.map(function (x) { return x.symbol; }).join() !== S.coins.map(function (x) { return x.symbol; }).join();
      S.coins = c; S.loaded.coins = true;
      if (!S.sel || !coinBy(S.sel)) S.sel = (coinBy("BTCUSDT") || c[0] || {}).symbol || null;
      if (changed) { buildTape(); buildRadar(); buildCoinButtons(); }
      if (S.sel && S.candleSym !== S.sel) loadCandles();
      updateLive();
    }).catch(function () { S.loaded.coins = true; });
  }
  function loadCandles() {
    var sym = S.sel; if (!sym) return Promise.resolve();
    S.candleSym = sym; S.candleErr = "";
    if (!S.candles.length) $("chartMsg").textContent = "loading 4h bars…";
    return getJSON("/api/portal/candles?symbol=" + encodeURIComponent(sym) + "&tf=4h&limit=48").then(function (b) {
      if (S.candleSym !== sym) return;
      S.candles = Array.isArray(b) ? b : []; S.form = null;
      $("chartMsg").textContent = S.candles.length ? "" : "no bars returned";
      renderChart();
    }).catch(function (e) {
      if (S.candleSym !== sym) return;
      S.candles = []; S.candleErr = e.message; $("chartMsg").textContent = "candles unavailable · " + e.message; renderChart();
    });
  }
  function loadSwing() { return getJSON("/api/swing").then(function (d) { S.swing = d; S.loaded.swing = true; renderAll(); }).catch(function () { S.loaded.swing = true; }); }
  function loadSignals() {
    return getJSON("/api/portal/signals?days=14&limit=40").then(function (d) {
      var list = d.signals || []; S.signalsErr = d.error || "";
      if (S.seen && list.length && !S.seen[list[0].id]) toast(list[0]);
      S.seen = {}; list.forEach(function (x) { S.seen[x.id] = 1; });
      S.signals = list; renderAll();
    }).catch(function (e) { S.signalsErr = e.message; renderAll(); });
  }
  function loadMeta() { return getJSON("/api/portal/meta").then(function (d) { S.meta = d; renderAll(); }).catch(function () {}); }
  function loadBudget() { return getJSON("/api/llm/budget").then(function (d) { S.budget = d; renderAll(); }).catch(function () {}); }
  function loadStatus() { return getJSON("/api/status").then(function (d) { S.status = d; renderAll(); }).catch(function () {}); }

  // ---------- derived ----------
  function regime() {
    var rf = (S.swing && S.swing.regime_filter) || {};
    var now = rf.now || {};
    var max = rf.vol_rank_max != null ? rf.vol_rank_max : (S.meta ? S.meta.vol_rank_max : 0.67);
    var mode = rf.mode || (S.meta && S.meta.regime_filter) || "";
    var rank = now.btc_vol_rank;
    var closed = mode === "on" && rank != null && rank >= max;
    return { rank: rank, max: max, mode: mode, closed: closed, known: rank != null, at: now.at };
  }
  function openTrades() {
    var open = (S.swing && S.swing.open) || [];
    return open.map(function (p) {
      var c = coinBy(p.symbol), mark = c ? c.price : null;
      var dir = String(p.side).toLowerCase() === "short" ? -1 : 1;
      var risk = Math.abs(p.entry - p.stop);
      var R = mark != null && risk > 0 ? dir * (mark - p.entry) / risk : null;
      return { p: p, mark: mark, dir: dir, R: R };
    });
  }
  function riskPct() { var r = S.swing && S.swing.rules && S.swing.rules.risk_per_trade; return r == null ? null : (r <= 1 ? r * 100 : r); }
  function topBudget() {
    var best = null;
    ((S.budget && S.budget.models) || []).forEach(function (m) {
      var lim = m.limits || {}, share = null;
      if (lim.tpd) share = m.today_tokens / lim.tpd; else if (lim.rpd) share = m.today_calls / lim.rpd;
      if (share != null && (!best || share > best.share)) best = { model: m.model, share: share };
    });
    return best;
  }

  // ---------- builders (DOM that only changes when the coin list changes) ----------
  function buildTape() {
    var html = S.coins.map(function (c) {
      return '<span data-t="' + esc(c.symbol) + '"><b>' + esc(base(c.symbol)) + "</b><em>—</em><b>—</b></span>";
    }).join("");
    $("tape").innerHTML = html + html;
  }
  function buildRadar() {
    var r = $("radar");
    r.querySelectorAll(".blip,.blip-l").forEach(function (n) { n.remove(); });
    var n = Math.min(S.coins.length, 16);
    for (var i = 0; i < n; i++) {
      var b = document.createElement("div"); b.className = "blip"; b.dataset.i = i;
      var l = document.createElement("span"); l.className = "blip-l"; l.dataset.i = i; l.textContent = base(S.coins[i].symbol);
      r.appendChild(b); r.appendChild(l);
    }
    $("radarN").textContent = n;
  }
  function buildCoinButtons() {
    $("coinButtons").innerHTML = S.coins.slice(0, 16).map(function (c) {
      return '<button class="coin" data-sym="' + esc(c.symbol) + '">' + esc(base(c.symbol)) + "</button>";
    }).join("");
  }

  // ---------- live updates ----------
  function updateLive() {
    document.querySelectorAll("#tape [data-t]").forEach(function (s) {
      var c = coinBy(s.dataset.t); if (!c) return;
      s.children[1].textContent = px(c.price);
      s.children[2].textContent = (c.change_24h_pct >= 0 ? "▲ " : "▼ ") + pct(c.change_24h_pct);
    });
    var n = Math.min(S.coins.length, 16), maxCh = 0.5;
    for (var i = 0; i < n; i++) maxCh = Math.max(maxCh, Math.abs(S.coins[i].change_24h_pct || 0));
    document.querySelectorAll("#radar .blip,#radar .blip-l").forEach(function (el) {
      var i = +el.dataset.i, c = S.coins[i]; if (!c) return;
      var ang = i * (360 / n) + 12, rad = ang * Math.PI / 180;
      var r = 12 + Math.min(1, Math.abs(c.change_24h_pct || 0) / maxCh) * 34;
      el.style.left = (50 + r * Math.sin(rad)).toFixed(1) + "%";
      el.style.top = (50 - r * Math.cos(rad)).toFixed(1) + "%";
      if (el.classList.contains("blip")) {
        el.style.background = (c.change_24h_pct || 0) >= 0 ? "var(--up)" : "var(--dn)";
        el.style.animationDelay = (ang / 360 * 4 - 4).toFixed(2) + "s";
      }
    });
    document.querySelectorAll(".coin").forEach(function (b) { b.classList.toggle("on", b.dataset.sym === S.sel); });
    var c = coinBy(S.sel);
    if (c && S.candles.length) {
      var last = S.candles[S.candles.length - 1];
      if (!S.form) S.form = { o: last.c, h: Math.max(last.c, c.price), l: Math.min(last.c, c.price), c: c.price };
      else { S.form.c = c.price; S.form.h = Math.max(S.form.h, c.price); S.form.l = Math.min(S.form.l, c.price); }
    }
    renderChart();
    renderStream();
    renderStrip();
    if (S.hub === "book") renderBook();
  }

  // ---------- renderers ----------
  function renderChart() {
    var c = coinBy(S.sel);
    $("chartLabel").textContent = "TARGET :: " + (S.sel ? base(S.sel) + "/USDT" : "—") + " · 4H · KELTNER 20 / 2.5";
    $("chartPrice").textContent = c ? px(c.price) : "—";
    var chg = $("chartChg"); chg.textContent = c ? pct(c.change_24h_pct) + " 24h" : "—";
    chg.classList.toggle("dn", !!c && c.change_24h_pct < 0);
    var ks = S.candles.slice(); if (S.form) ks.push(S.form);
    var box = $("candles");
    if (ks.length < 3) { box.innerHTML = ""; ["kBand", "kUp", "kDn", "kMid"].forEach(function (id) { $(id).setAttribute("points", ""); }); $("lastLine").style.display = $("lastTag").style.display = "none"; return; }
    var H = $("chart").clientHeight || 280, N = ks.length;
    var ema = [], atr = [], e = ks[0].c, a = ks[0].h - ks[0].l;
    ks.forEach(function (k, i) {
      e = i ? e + (k.c - e) * 2 / 21 : k.c; ema.push(e);
      var tr = i ? Math.max(k.h - k.l, Math.abs(k.h - ks[i - 1].c), Math.abs(k.l - ks[i - 1].c)) : k.h - k.l;
      a = i ? a + (tr - a) / 10 : tr; atr.push(a);
    });
    var up = ema.map(function (v, i) { return v + 2.5 * atr[i]; }), dn = ema.map(function (v, i) { return v - 2.5 * atr[i]; });
    var hi = -Infinity, lo = Infinity;
    ks.forEach(function (k, i) { hi = Math.max(hi, k.h, up[i]); lo = Math.min(lo, k.l, dn[i]); });
    var pv = (hi - lo) * 0.04 || 1; hi += pv; lo -= pv;
    function y(v) { return (hi - v) / (hi - lo) * H; }
    function X(i) { return ((i + 0.5) * 1000 / N).toFixed(1); }
    function line(arr) { return arr.map(function (v, i) { return X(i) + "," + (y(v) * 280 / H).toFixed(1); }).join(" "); }
    $("kUp").setAttribute("points", line(up)); $("kDn").setAttribute("points", line(dn)); $("kMid").setAttribute("points", line(ema));
    $("kBand").setAttribute("points", line(up) + " " + dn.map(function (v, i) { return X(i) + "," + (y(v) * 280 / H).toFixed(1); }).reverse().join(" "));
    var cw = 100 / N, html = "";
    ks.forEach(function (k, i) {
      var col = k.c >= k.o ? "var(--up)" : "var(--dn)";
      html += '<div class="cw" style="left:' + (i * cw + cw / 2).toFixed(3) + "%;top:" + y(k.h).toFixed(1) + "px;height:" + Math.max(1, y(k.l) - y(k.h)).toFixed(1) + "px;background:" + col + '"></div>' +
        '<div class="cb" style="left:' + (i * cw + cw * 0.15).toFixed(3) + "%;width:" + (cw * 0.7).toFixed(3) + "%;top:" + y(Math.max(k.o, k.c)).toFixed(1) + "px;height:" + Math.max(3, Math.abs(y(k.o) - y(k.c))).toFixed(1) + "px;background:" + col + (i === N - 1 && S.form ? ";opacity:.75" : "") + '"></div>';
    });
    box.innerHTML = html;
    var lastY = y(ks[N - 1].c).toFixed(1) + "px";
    $("lastLine").style.display = $("lastTag").style.display = "";
    $("lastLine").style.top = lastY; $("lastTag").style.top = lastY; $("lastTag").textContent = px(ks[N - 1].c);
  }

  function renderStrip() {
    var g = regime(), open = openTrades(), risk = riskPct();
    var gate = $("stripGate");
    gate.textContent = !g.known ? "FILTER · NO READING YET" : g.mode !== "on" ? "FILTER " + String(g.mode || "OFF").toUpperCase() : g.closed ? "FILTER CLOSED" : "FILTER OPEN";
    gate.classList.toggle("closed", g.closed);
    $("stripOpen").textContent = "OPEN SWING TRADES " + (S.swing ? open.length : "…");
    $("stripRisk").textContent = risk == null ? "WORST CASE …" : "WORST CASE −" + (open.length * risk).toFixed(1) + "% WALLET";
    var newest = S.coins.reduce(function (m, c) { return c.timestamp && c.timestamp > m ? c.timestamp : m; }, "");
    $("stripFeed").textContent = S.coins.length ? "FEED " + S.coins.length + " COINS · " + ago(newest) : "FEED …";
    var live = S.meta && S.meta.live_trading_mode && S.meta.live_trading_mode !== "off";
    $("stripMode").textContent = live ? "LIVE TRADING: " + String(S.meta.live_trading_mode).toUpperCase() : "LIVE TRADING: OFF · PAPER ONLY";
  }

  function renderAgents() {
    var g = regime(), open = openTrades(), scan = (S.swing && S.swing.scan) || {}, risk = riskPct(), tb = topBudget();
    var keys = Object.keys(scan), lastClose = 0;
    keys.forEach(function (k) { lastClose = Math.max(lastClose, scan[k].bar_close || 0); });
    var pending = S.signals.filter(function (s) { return !s.outcome || s.outcome === "pending"; }).length;
    var maxLev = S.swing && S.swing.rules ? S.swing.rules.max_leverage : null;
    var metrics = [
      S.coins.length + " coins live",
      (S.status && S.status.symbols_tracked != null ? S.status.symbols_tracked + " symbols" : "shadow"),
      g.known ? "BTC vol " + Math.round(g.rank * 100) + "th pctl" : "no reading",
      keys.length ? keys.length + " bars · " + (lastClose ? ago(new Date(lastClose).toISOString()) : "—") : "waiting",
      risk != null ? risk + "% · ≤" + (maxLev || "?") + "x" : "—",
      open.length + " open",
      pending + " running",
      tb ? Math.round(tb.share * 100) + "% " + tb.model.split("/")[0] : "—"
    ];
    $("agents").innerHTML = AGENTS.map(function (a, i) {
      var active = i === S.stage, blocked = i === 2 && g.closed;
      return '<div class="agent' + (active ? " active" : "") + (blocked ? " blocked" : "") + '"><div class="a-top"><span>0' + (i + 1) + "</span><span>" +
        (blocked ? "BLOCKING" : active ? "WORKING" : "READY") + "</span></div><b>" + esc(a[0]) + "</b><p>" + esc(a[1]) + '</p><span class="metric">' + esc(metrics[i]) + "</span></div>";
    }).join("");
    $("handoff").textContent = AGENTS[S.stage][0].toUpperCase();
    $("flowFill").style.width = ((S.stage + 1) / AGENTS.length * 100).toFixed(1) + "%";
  }

  function renderGate() {
    var g = regime(), p = $("gatePanel");
    $("gateVal").textContent = g.known ? Math.round(g.rank * 100) : "—";
    $("gateMax").textContent = Math.round(g.max * 100);
    $("gateFill").style.width = g.known ? Math.round(g.rank * 100) + "%" : "0";
    $("gateMark").style.left = (g.max * 100).toFixed(1) + "%";
    $("gateWord").textContent = !g.known ? "NO READING YET" : g.mode !== "on" ? "FILTER " + String(g.mode || "off").toUpperCase() : g.closed ? "CLOSED" : "OPEN";
    $("gateText").textContent = !g.known ? "The first swing scan after a restart records it." :
      g.closed ? "Top third of its past year: new swing signals are skipped and replayed on paper only." :
        g.mode === "on" ? "Below the top third of its past year: swing signals are traded." :
          "The filter is not deciding trades in this mode.";
    p.classList.toggle("closed", g.closed);
  }

  function traceFor(s) {
    var g = regime(), lines = [];
    var dist = s.price && s.stop ? Math.abs(s.price - s.stop) / s.price * 100 : null;
    lines.push("> scanner   :: " + stratName(s.strategy) + " fired " + String(s.direction).toUpperCase() + " on " + s.symbol + " " + (s.timeframe || "") + " @ " + px(s.price));
    lines.push(s.btc_vol_rank == null ? "> filter    :: no market-filter tag on this signal" :
      "> filter    :: btc 30d vol pctl " + Math.round(s.btc_vol_rank * 100) + (s.filter_skip ? " >= " : " < ") + Math.round(g.max * 100) + " -> " + (s.filter_skip ? "SKIP" : "PASS"));
    lines.push("> risk      :: stop " + px(s.stop) + (dist != null ? " (" + dist.toFixed(1) + "%)" : "") + " · target " + px(s.target));
    lines.push("> exec      :: " + (s.status === "TRADED" ? "paper trade opened" : "not traded · " + (s.skip_reason_text || s.skip_reason)));
    lines.push("> replay    :: " + (!s.outcome || s.outcome === "pending" ? "running on 1h bars -> stop | 3R | time limit" : "resolved " + s.outcome.toUpperCase() + " " + pct(s.pnl_pct)));
    lines.push("> logged    :: " + ago(s.time));
    return lines.join("\n");
  }
  function renderTrace() {
    var s = S.signals[0], pill = $("traceStatus");
    if (!s) {
      $("traceTitle").textContent = "DECISION TRACE";
      pill.textContent = "—"; pill.className = "st-pill neutral";
      S.traceId = null; S.traceFull = S.signalsErr ? "> signals unavailable :: " + S.signalsErr : "> no swing signals in the last 14 days"; S.traceN = S.traceFull.length;
      $("trace").textContent = S.traceFull; return;
    }
    $("traceTitle").textContent = "DECISION TRACE · " + base(s.symbol) + " " + String(s.direction).toUpperCase();
    pill.textContent = s.status; pill.className = "st-pill" + (s.status === "TRADED" ? "" : s.status === "FILTER BLOCK" ? " skip" : " neutral");
    if (S.traceId !== s.id) { S.traceId = s.id; S.traceFull = traceFor(s); S.traceN = 0; }
    else S.traceFull = traceFor(s);
  }

  function renderStream() {
    var cols = $("stream");
    if (!S.coins.length) { cols.innerHTML = ""; return; }
    if (cols.dataset.n === String(S.coins.length) && cols.dataset.t && Date.now() - +cols.dataset.t < 30000) return;
    var lines = S.coins.map(function (c) {
      return base(c.symbol).padEnd(5) + " " + pct(c.change_24h_pct) + "\n " + px(c.price) +
        "\n rsi " + (c.rsi_14 != null ? Math.round(c.rsi_14) : "—") + " vr " + (c.volume_ratio != null ? c.volume_ratio.toFixed(1) : "—");
    });
    var html = "";
    for (var k = 0; k < 2; k++) {
      var part = lines.filter(function (_, i) { return i % 2 === k; }).join("\n");
      html += '<div class="stream-col" style="animation-duration:' + (14 + k * 6) + "s;color:" + (k ? "var(--dn)" : "var(--up)") + '">' + esc(part + "\n" + part) + "</div>";
    }
    cols.innerHTML = html; cols.dataset.n = String(S.coins.length); cols.dataset.t = String(Date.now());
    $("streamAge").textContent = S.coins.length + " coins";
  }

  function renderFeed() {
    var box = $("feedRows");
    if (!S.signals.length) { box.innerHTML = '<div class="empty">' + (S.signalsErr ? "Signals unavailable: " + esc(S.signalsErr) : "No swing signals in the last 14 days.") + "</div>"; return; }
    var prev = box.dataset.top;
    box.innerHTML = S.signals.slice(0, 12).map(function (s, i) {
      var short = String(s.direction).toLowerCase() === "short";
      var cls = s.status === "TRADED" ? "" : s.status === "FILTER BLOCK" ? " skip" : " neutral";
      return '<div class="frow' + (i === 0 && prev && prev !== String(s.id) ? " new" : "") + '"><span class="dim">' + esc(ago(s.time)) + "</span><b>" + esc(base(s.symbol)) +
        '</b><span class="side-tag' + (short ? " short" : "") + '">' + (short ? "SHORT" : "LONG") + "</span><span>" + esc(stratName(s.strategy)) + " · " + esc(s.timeframe || "") +
        '</span><span class="dim">' + esc(px(s.price)) + (s.outcome && s.outcome !== "pending" ? " · " + esc(s.outcome) + " " + esc(pct(s.pnl_pct)) : "") +
        '</span><span class="st-tag' + cls + '" title="' + esc(s.skip_reason_text) + '">' + esc(s.status) + "</span></div>";
    }).join("");
    box.dataset.top = String(S.signals[0].id);
  }

  function answer(i) {
    var g = regime(), open = openTrades(), risk = riskPct();
    if (i === 0) {
      if (!g.known) return "No market-filter reading yet. The first swing scan after a restart records Bitcoin's 30-day volatility rank.";
      return "The filter is " + (g.mode !== "on" ? "in '" + g.mode + "' mode" : g.closed ? "CLOSED" : "OPEN") + ". Bitcoin's 30-day volatility is at the " +
        Math.round(g.rank * 100) + "th percentile of its past year; at or above " + Math.round(g.max * 100) + " new swing signals are skipped.";
    }
    if (i === 1) {
      var sk = S.signals.filter(function (s) { return s.status !== "TRADED"; })[0];
      return sk ? base(sk.symbol) + " " + sk.direction + " (" + stratName(sk.strategy) + ", " + ago(sk.time) + ") was not traded: " + (sk.skip_reason_text || sk.skip_reason) +
        (sk.btc_vol_rank != null ? ". BTC vol was at the " + Math.round(sk.btc_vol_rank * 100) + "th percentile." : ".") : "Every swing signal in the last 14 days became a paper trade.";
    }
    if (i === 2) {
      if (!S.swing) return "Still loading the swing book.";
      var live = open.filter(function (o) { return o.R != null; });
      var sumR = live.reduce(function (a, o) { return a + o.R; }, 0);
      return open.length + " open swing trade" + (open.length === 1 ? "" : "s") + (risk != null ? " at " + risk + "% risk each: worst case −" + (open.length * risk).toFixed(1) + "% of the wallet if every stop hits" : "") +
        (live.length ? ". Right now they sum to " + rr(sumR) + "." : ".");
    }
    var mode = S.meta ? S.meta.live_trading_mode : "off";
    return mode && mode !== "off" ? "Live trading mode is '" + mode + "'. Check Settings before doing anything else." :
      "Nothing is live. Every trade on this page is a paper trade; the live execution book is not wired into the engine.";
  }
  function renderAsk() {
    $("askBtns").innerHTML = ASKS.map(function (q, i) { return '<button data-ask="' + i + '" class="' + (S.ask === i ? "on" : "") + '">' + esc(q) + "</button>"; }).join("");
    if (S.ask >= 0) $("askAnswer").textContent = answer(S.ask);
  }

  function renderBook() {
    var open = openTrades();
    $("positions").innerHTML = !S.swing ? '<div class="empty">Loading the swing book…</div>' : !open.length ? '<div class="empty">No open swing trades.</div>' :
      open.map(function (o) {
        var p = o.p, short = o.dir < 0, R = o.R;
        var prog = R == null ? 25 : (Math.max(-1, Math.min(3, R)) + 1) / 4 * 100;
        var left = p.expires_at ? (new Date(p.expires_at).getTime() - Date.now()) / 86400000 : null;
        return '<div class="pos"><div class="pos-top"><b>' + esc(base(p.symbol)) + '</b><span class="side-tag' + (short ? " short" : "") + '">' + (short ? "SHORT" : "LONG") + "</span></div>" +
          '<div class="mono small dim">' + esc(stratName(p.strategy)) + (p.leverage ? " · " + esc(p.leverage) + "x" : "") + "</div>" +
          '<div class="pos-r' + (R != null && R < 0 ? " dn" : "") + '">' + rr(R) + "</div>" +
          '<div class="rbar"><div class="rbar-fill' + (R != null && R < 0 ? " dn" : "") + '" style="width:' + prog.toFixed(1) + '%"></div><div class="rbar-zero"></div></div>' +
          '<div class="pos-scale"><span>STOP ' + esc(px(p.stop)) + "</span><span>0</span><span>TARGET " + esc(px(p.target)) + "</span></div>" +
          '<div class="pos-foot"><span>in ' + esc(px(p.entry)) + "</span><span>now " + esc(px(o.mark)) + "</span><b>" + (left == null ? "—" : left > 0 ? left.toFixed(1) + "d left" : "expiring") + "</b></div></div>";
      }).join("");
    var cl = S.swing && S.swing.closed;
    $("closedSummary").textContent = cl ? cl.n + " closed · win rate " + (cl.win_rate != null ? Math.round(cl.win_rate * 100) + "%" : "—") + " · avg " + rr(cl.avg_r) + " · total " + rr(cl.total_r) : "";
    var cols = "grid-template-columns:80px 70px minmax(0,1.4fr) 90px minmax(0,1fr) 110px";
    $("closedRows").innerHTML = !cl || !cl.last || !cl.last.length ? '<div class="empty">No closed swing trades yet.</div>' :
      '<div class="trow head" style="' + cols + '"><span>COIN</span><span>SIDE</span><span>STRATEGY</span><span>R</span><span>EXIT</span><span>CLOSED</span></div>' +
      cl.last.slice().reverse().map(function (t) {
        return '<div class="trow" style="' + cols + '"><b>' + esc(base(t.symbol)) + "</b><span>" + esc(t.side) + "</span><span>" + esc(stratName(t.strategy)) +
          '</span><b class="' + (t.r >= 0 ? "up" : "dn") + '">' + rr(t.r) + "</b><span>" + esc(t.exit_reason || "") + '</span><span class="dim">' + esc(ago(t.closed_at)) + "</span></div>";
      }).join("");
    var sb = S.swing && S.swing.regime_filter && S.swing.regime_filter.signals;
    if (!sb) { $("takeSkip").innerHTML = '<div class="empty">No tagged swing signals yet.</div>'; return; }
    var tk = sb.take || {}, sk = sb.skip || {}, tot = Math.max(1, (tk.n || 0) + (tk.running || 0) + (sk.n || 0) + (sk.running || 0));
    function bar(lbl, g, cls) {
      var n = (g.n || 0) + (g.running || 0);
      return '<div class="ts-bar ' + cls + '"><span>' + lbl + '</span><i style="width:' + (n / tot * 70).toFixed(1) + '%"></i><b>' + n + "</b></div>" +
        '<div class="mono tiny dim">' + (g.n || 0) + " resolved · avg " + rr(g.avg_r) + " · " + (g.running || 0) + " running</div>";
    }
    $("takeSkip").innerHTML = bar("TAKE", tk, "") + bar("SKIP", sk, "skip");
  }

  function renderEvidence() {
    var ex = S.swing && S.swing.expected_from_backtest;
    $("expected").innerHTML = ex ? "<b>" + esc(ex.r_per_trade) + " R / trade</b><span>Expected from the 5-year backtest: win rate " + esc(ex.win_rate) + ", losing streaks of " +
      esc(ex.losing_streaks) + ", about " + esc(ex.trades_per_day_all_coins) + " trades a day. Judge after " + esc(ex.judge_after_trades) + " trades.</span>" : "<b>Loading…</b>";
    var by = (S.swing && S.swing.closed && S.swing.closed.by_strategy) || {};
    var rules = (S.swing && S.swing.rules) || {};
    var names = Object.keys(by).sort(function (a, b) { return by[b].n - by[a].n; });
    var armed = String(rules.strategies || "").split(",").map(function (s) { return stratName(s.trim()); }).filter(Boolean);
    var cols = "grid-template-columns:minmax(0,1.6fr) 90px 90px 90px 110px";
    $("stratNote").textContent = rules.timeframes ? rules.timeframes + " · stop " + rules.stop + " · target " + rules.target : "";
    $("stratRows").innerHTML = (!names.length ? '<div class="empty">No closed swing trades yet, so nothing to score.</div>' :
      '<div class="trow head" style="' + cols + '"><span>STRATEGY</span><span>TRADES</span><span>WINS</span><span>AVG R</span><span>TOTAL R</span></div>' +
      names.map(function (n) {
        var b = by[n];
        var avg = b.n ? b.sum_r / b.n : null;
        return '<div class="trow" style="' + cols + '"><b style="font-family:var(--sans);font-size:16px">' + esc(stratName(n)) + "</b><span>" + b.n + "</span><span>" + b.wins +
          '</span><b class="' + (avg == null ? "dim" : avg >= 0 ? "up" : "dn") + '">' + rr(avg) + '</b><span class="' + (b.sum_r >= 0 ? "up" : "dn") + '">' + rr(b.n ? b.sum_r : null) + "</span></div>";
      }).join("")) +
      (armed.length ? '<p class="mono tiny dim" style="margin-top:12px">ARMED: ' + esc(armed.join(" · ")) + "</p>" : "");
  }

  function renderSystem() {
    var scan = (S.swing && S.swing.scan) || {}, keys = Object.keys(scan).sort();
    var cols = "grid-template-columns:minmax(0,1fr) 120px minmax(0,1fr) 90px";
    $("scanNote").textContent = keys.length + " coin × timeframe pairs";
    $("scanRows").innerHTML = !keys.length ? '<div class="empty">No swing scan since the last restart.</div>' :
      '<div class="trow head" style="' + cols + '"><span>COIN @ TF</span><span>BAR CLOSED</span><span>SIGNAL</span><span>AGE</span></div>' +
      keys.map(function (k) {
        var v = scan[k];
        return '<div class="trow" style="' + cols + '"><b>' + esc(k.toUpperCase()) + '</b><span class="dim">' + (v.bar_close ? hms(new Date(v.bar_close)) : "—") + "</span><span>" +
          (v.signal ? '<b class="' + (v.side < 0 ? "dn" : "up") + '">' + esc(stratName(v.signal)) + (v.side < 0 ? " SHORT" : " LONG") + "</b>" : '<span class="dim">none</span>') +
          '</span><span class="dim">' + (v.age_min != null ? Math.round(v.age_min) + "m" : "—") + "</span></div>";
      }).join("");
    var models = (S.budget && S.budget.models) || [];
    $("budgets").innerHTML = !models.length ? '<div class="empty">No AI calls recorded today.</div>' : models.map(function (m) {
      var lim = m.limits || {}, share = lim.tpd ? m.today_tokens / lim.tpd : lim.rpd ? m.today_calls / lim.rpd : null;
      var label = lim.tpd ? m.today_tokens.toLocaleString() + " / " + lim.tpd.toLocaleString() + " tok" : lim.rpd ? m.today_calls + " / " + lim.rpd + " calls" : m.today_calls + " calls";
      var w = share == null ? 0 : Math.min(100, share * 100);
      return '<div class="budget"><div><span>' + esc(m.model) + "</span><b>" + esc(label) + '</b></div><div class="meter"><i class="' + (share != null && share >= 0.75 ? "hot" : "") +
        '" style="width:' + w.toFixed(1) + '%"></i></div>' + (m.cooldown_s ? '<span class="dn tiny">cooling down ' + m.cooldown_s + "s</span>" : "") + "</div>";
    }).join("");
    $("budgetNote").textContent = S.budget && S.budget.safety_share ? "Calls stop at " + Math.round(S.budget.safety_share * 100) + "% of each free-tier limit." : "";
    var st = S.status || {}, up = st.uptime_seconds;
    var kv = [
      ["UPTIME", up != null ? Math.floor(up / 86400) + "d " + Math.floor(up / 3600) % 24 + "h " + Math.floor(up / 60) % 60 + "m" : "—"],
      ["SYMBOLS TRACKED", st.symbols_tracked != null ? st.symbols_tracked : "—"],
      ["PAPER TRADING", S.meta ? (S.meta.paper_trading_enabled ? "ON" : "OFF") : "—"],
      ["SWING BOOK", S.meta ? (S.meta.swing_enabled ? "ON" : "OFF") : "—"],
      ["MARKET FILTER", S.meta ? String(S.meta.regime_filter).toUpperCase() : "—"],
      ["LIVE TRADING", S.meta ? String(S.meta.live_trading_mode).toUpperCase() : "—"],
      ["WALLET", S.swing && S.swing.wallet != null ? "₹" + Number(S.swing.wallet).toLocaleString("en-IN", { maximumFractionDigits: 0 }) : "—"],
      ["EXCLUDED COINS", S.swing && S.swing.rules && S.swing.rules.excluded_coins ? String(S.swing.rules.excluded_coins).toUpperCase() : "—"]
    ];
    $("engine").innerHTML = kv.map(function (r) { return "<div><small>" + esc(r[0]) + "</small><b>" + esc(r[1]) + "</b></div>"; }).join("");
  }

  function links(id, list) {
    $(id).innerHTML = list.map(function (l) { return '<a href="' + esc(l[0]) + '"><b>' + esc(l[1]) + "</b><span>" + esc(l[2]) + "</span></a>"; }).join("");
  }

  function renderAll() {
    renderStrip();
    if (S.hub === "command") { renderAgents(); renderGate(); renderTrace(); renderFeed(); renderAsk(); }
    else if (S.hub === "book") renderBook();
    else if (S.hub === "evidence") renderEvidence();
    else renderSystem();
  }

  function toast(s) {
    var t = $("toast"), traded = s.status === "TRADED";
    t.className = "toast" + (traded ? "" : s.status === "FILTER BLOCK" ? " skip" : " neutral");
    t.innerHTML = "<small>" + (traded ? "SIGNAL LOCKED" : s.status === "FILTER BLOCK" ? "FILTER BLOCK" : "NOT TRADED") + "</small><b>" +
      esc(base(s.symbol) + " " + String(s.direction).toUpperCase() + " · " + stratName(s.strategy)) + "</b>";
    t.hidden = false; t.style.animation = "none"; void t.offsetWidth; t.style.animation = "";
    clearTimeout(toast.h); toast.h = setTimeout(function () { t.hidden = true; }, 3200);
  }

  // ---------- clocks ----------
  function tickClock() {
    var now = Date.now();
    $("clock").textContent = hms(new Date(now));
    $("next4").textContent = dur(4 * 3600e3 - (now % (4 * 3600e3)));
  }
  function tickType() {
    if (S.traceN < S.traceFull.length) { S.traceN = Math.min(S.traceFull.length, S.traceN + 3); $("trace").textContent = S.traceFull.slice(0, S.traceN); }
  }
  function tickStage() { S.stage = (S.stage + 1) % AGENTS.length; if (S.hub === "command") renderAgents(); }

  // ---------- events ----------
  document.addEventListener("click", function (e) {
    var tab = e.target.closest(".tab");
    if (tab && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
      e.preventDefault();
      if (tab.dataset.hub !== S.hub) { history.pushState({}, "", "/" + tab.dataset.hub); show(tab.dataset.hub); }
      return;
    }
    var sw = e.target.closest(".sw"); if (sw) { setPalette(sw.dataset.p); return; }
    var coin = e.target.closest(".coin");
    if (coin) { S.sel = coin.dataset.sym; S.form = null; S.candles = []; renderChart(); updateLive(); loadCandles(); return; }
    var ask = e.target.closest("[data-ask]"); if (ask) { S.ask = +ask.dataset.ask; renderAsk(); }
  });
  window.addEventListener("popstate", function () { show(location.pathname.slice(1)); });
  window.addEventListener("resize", function () { renderChart(); });

  // ---------- start ----------
  if (LEGACY_HASH.indexOf(location.hash) >= 0) { location.replace("/classic" + location.hash); return; }
  var pal = document.documentElement.getAttribute("data-palette") || "sage";
  setPalette(pal);
  links("evidenceLinks", EVIDENCE_LINKS); links("systemLinks", SYSTEM_LINKS);
  show(location.pathname.replace(/^\/|\/$/g, "") || "command");
  tickClock(); setInterval(tickClock, 1000);
  setInterval(tickType, 40);
  setInterval(tickStage, 1600);
  function every(fn, ms) { fn(); setInterval(function () { if (!document.hidden) fn(); }, ms); }
  every(loadCoins, 5000);
  every(loadSwing, 30000);
  every(loadSignals, 30000);
  every(loadMeta, 300000);
  every(loadBudget, 60000);
  every(loadStatus, 30000);
  setInterval(function () { if (!document.hidden && S.sel) { S.candleSym = null; loadCandles(); } }, 120000);
})();
