/* System hub: Health · Data · Diagnostics (tools: Settings, Keys, API list). */
(function () {
  "use strict";
  var U = UI.U, C = UI.C, F = UI.F, e = U.esc;

  // ------------------------------------------------------------- health
  UI.widget("engine", {
    title: "ENGINE", uses: ["status", "meta", "swing"],
    render: function (el, D) {
      var st = D.status || {}, up = st.uptime_seconds, m = D.meta, sw = D.swing;
      el.innerHTML = C.tiles([
        ["UPTIME", up != null ? Math.floor(up / 86400) + "d " + Math.floor(up / 3600) % 24 + "h " + Math.floor(up / 60) % 60 + "m" : "—"],
        ["SYMBOLS TRACKED", st.symbols_tracked != null ? st.symbols_tracked : "—"],
        ["PAPER TRADING", m ? (m.paper_trading_enabled ? "ON" : "OFF") : "—"],
        ["SWING BOOK", m ? (m.swing_enabled ? "ON" : "OFF") : "—"],
        ["MARKET FILTER", m ? String(m.regime_filter).toUpperCase() : "—"],
        ["LIVE TRADING", m ? String(m.live_trading_mode).toUpperCase() : "—", "", F.live(D) ? "dn" : ""],
        ["WALLET", sw && sw.wallet != null ? U.inr(sw.wallet) : "—"],
        ["EXCLUDED COINS", sw && sw.rules && sw.rules.excluded_coins ? String(sw.rules.excluded_coins).toUpperCase() : "—"]
      ]);
    }
  });
  UI.widget("scan", {
    title: "LAST SWING SCAN", uses: ["swing"],
    mount: function (el, ctx) { ctx.s.t = { sort: null }; },
    on: { sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); } },
    render: function (el, D, ctx) {
      if (!D.swing) { el.innerHTML = C.state(D, "swing"); return; }
      var scan = D.swing.scan || {}, rows = Object.keys(scan).sort().map(function (k) { return Object.assign({ key: k }, scan[k]); });
      ctx.right(rows.length + " coin × timeframe pairs");
      el.innerHTML = C.table([
        { label: "COIN @ TF", w: "minmax(0,1fr)", cell: function (r) { return "<b>" + e(r.key.toUpperCase()) + "</b>"; }, sort: function (r) { return r.key; } },
        { label: "BAR CLOSED", w: "110px", cell: function (r) { return '<span class="dim">' + (r.bar_close ? U.hms(new Date(r.bar_close)) : "—") + "</span>"; } },
        { label: "SIGNAL", w: "minmax(0,1fr)", cell: function (r) { return r.signal ? '<b class="' + (r.side < 0 ? "dn" : "up") + '">' + e(U.name(r.signal)) + (r.side < 0 ? " SHORT" : " LONG") + "</b>" : '<span class="dim">none</span>'; }, sort: function (r) { return r.signal ? 1 : 0; } },
        { label: "FILTER", w: "110px", cell: function (r) { return r.regime ? C.tag(r.regime.would_skip ? "SKIP" : "TAKE", r.regime.would_skip ? "skip" : "") : '<span class="dim">—</span>'; } },
        { label: "AGE", w: "80px", cell: function (r) { return '<span class="dim">' + (r.age_min != null ? Math.round(r.age_min) + "m" : "—") + "</span>"; }, sort: function (r) { return r.age_min; } }
      ], rows, { state: ctx.s.t, empty: "No swing scan since the last restart." });
    }
  });
  UI.widget("budget", {
    title: "AI BUDGET · TODAY", uses: ["budget"],
    render: function (el, D) {
      var b = D.budget; if (!b) { el.innerHTML = C.state(D, "budget"); return; }
      var models = b.models || [];
      el.innerHTML = (models.length ? models.map(function (m) {
        var l = m.limits || {}, sh = F.budgetShare(m);
        var lab = l.tpd ? m.today_tokens.toLocaleString() + " / " + l.tpd.toLocaleString() + " tok" : l.rpd ? m.today_calls + " / " + l.rpd + " calls" : m.today_calls + " calls";
        return '<div class="budget"><div><span>' + e(m.model) + "</span><b>" + e(lab) + "</b></div>" + C.meter(sh) + (m.cooldown_s ? '<span class="dn tiny">cooling down ' + m.cooldown_s + "s</span>" : "") +
          (m.last_error ? '<span class="dim tiny">' + e(String(m.last_error).slice(0, 90)) + "</span>" : "") + "</div>";
      }).join("") : C.empty("No AI calls recorded today.")) +
        (b.safety_share ? '<p class="small dim">Calls stop at ' + Math.round(b.safety_share * 100) + "% of each free-tier limit.</p>" : "");
    }
  });
  UI.widget("events", {
    title: "NEWS PAUSES", uses: ["events"],
    render: function (el, D) {
      var ev = D.events; if (!ev) { el.innerHTML = C.state(D, "events"); return; }
      el.innerHTML = (ev.blackout ? C.flag("amber", "Trading paused now: " + ev.blackout.name, "Paused from " + U.stamp(ev.blackout.pause_from) + " until " + U.stamp(ev.blackout.pause_until) + ".", ev.blackout.kind) : C.flag("ok", "No news pause right now", "New trades open normally.", "")) +
        ((ev.upcoming || []).length ? C.table([
          { label: "WHEN", w: "120px", cell: function (x) { return e(U.stamp(x.at)); } },
          { label: "EVENT", w: "minmax(0,1fr)", cell: function (x) { return e(x.name); } },
          { label: "KIND", w: "90px", cell: function (x) { return '<span class="dim">' + e(x.kind) + "</span>"; } }
        ], ev.upcoming.slice(0, 8), { minWidth: 360 }) : C.empty("Nothing scheduled."));
    }
  });
  UI.view("system/health", [[{ w: "engine", size: "grow" }, { w: "budget", size: "side" }], [{ w: "scan", size: "grow" }, { w: "events", size: "side" }]]);

  // ------------------------------------------------------------- data (was /data)
  UI.widget("tables", {
    title: "DATABASE TABLES", uses: ["tables"],
    mount: function (el, ctx) { ctx.s.open = ""; },
    on: {
      open: function (el, ev, t, ctx) { ctx.s.open = ctx.s.open === t.dataset.n ? "" : t.dataset.n; ctx.render(); },
      reload: function () { UI.Store.fetch("tables"); }
    },
    render: function (el, D, ctx) {
      var d = D.tables; if (!d) { el.innerHTML = C.state(D, "tables"); return; }
      ctx.right("generated " + U.ago(d.generated_at) + " · newest " + d.limit + " rows each");
      el.innerHTML = '<div class="toolbar"><button class="mini" data-act="reload">Reload</button></div>' + (d.tables || []).map(function (t) {
        var open = ctx.s.open === t.name;
        return '<div class="acc-item"><button class="acc-head' + (open ? " on" : "") + '" data-act="open" data-n="' + e(t.name) + '"><b>' + e(t.name) + '</b><span class="mono small dim">' +
          e((t.total != null ? t.total.toLocaleString() : "?") + " rows") + (t.error ? " · error" : "") + "</span></button>" +
          (open ? (t.error ? C.empty(t.error) : C.table((t.columns || []).map(function (c) {
            return { label: String(c).toUpperCase(), w: "minmax(120px,1fr)", cell: function (r) { var v = r[c]; return '<span class="mono small">' + e(v == null ? "—" : String(v).slice(0, 120)) + "</span>"; } };
          }), t.rows || [], { minWidth: Math.max(620, (t.columns || []).length * 130), empty: "Empty table." })) : "") + "</div>";
      }).join("");
    }
  });
  UI.view("system/data", [["tables"]]);

  // ------------------------------------------------------------- diagnostics
  UI.widget("feed-state", {
    title: "FEED STATE PER COIN", uses: ["debug"],
    mount: function (el, ctx) { ctx.s.t = { sort: null }; },
    on: { sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); } },
    render: function (el, D, ctx) {
      var d = D.debug; if (!d) { el.innerHTML = C.state(D, "debug"); return; }
      ctx.right("klines host " + (d.klines_host || "—") + (d.klines_last_error ? " · last error: " + String(d.klines_last_error).slice(0, 60) : ""));
      el.innerHTML = C.table([
        { label: "SYMBOL", w: "110px", cell: function (s) { return "<b>" + e(String(s.symbol).toUpperCase()) + "</b>"; }, sort: function (s) { return s.symbol; } },
        { label: "PRICE", w: "110px", cell: function (s) { return e(U.px(s.price)); } },
        { label: "1M CANDLES", w: "100px", cell: function (s) { return e(s.candles_1m); }, sort: function (s) { return s.candles_1m; } },
        { label: "RSI", w: "70px", cell: function (s) { return e(U.num(s.rsi_14) ? Math.round(s.rsi_14) : "—"); } },
        { label: "KLINES", w: "minmax(0,1fr)", cell: function (s) { return '<span class="dim">' + e(s.kline_status) + "</span>"; }, sort: function (s) { return String(s.kline_status); } }
      ], d.symbols || [], { state: ctx.s.t, empty: "No symbols tracked." });
    }
  });
  UI.widget("perf", {
    title: "SPEED", uses: ["perf"],
    render: function (el, D) {
      var p = D.perf; if (!p) { el.innerHTML = C.state(D, "perf"); return; }
      var db = p.db_ping_ms || {};
      el.innerHTML = C.tiles([["DB PING p50", (db.p50 != null ? db.p50 : "—") + " ms"], ["DB PING MAX", (db.max != null ? db.max : "—") + " ms"], ["DB FAILURES", db.failures || 0, "of " + (db.n || 0)], ["LOOP STALLS", (p.stalls || []).length, "recent"]]) +
        '<h3 class="h3">Slowest routes (p95)</h3>' + C.table([
          { label: "ROUTE", w: "minmax(0,1fr)", cell: function (r) { return '<span class="mono small">' + e(r.route) + "</span>"; } },
          { label: "N", w: "60px", cell: function (r) { return e(r.n); } },
          { label: "P50", w: "70px", cell: function (r) { return e(r.p50_ms + "ms"); } },
          { label: "P95", w: "70px", cell: function (r) { return '<b class="' + (r.p95_ms > 1000 ? "dn" : "") + '">' + e(r.p95_ms + "ms") + "</b>"; } }
        ], (p.routes || []).slice(0, 10), { minWidth: 420 }) +
        '<h3 class="h3">Slowest jobs</h3>' + C.table([
          { label: "JOB", w: "minmax(0,1fr)", cell: function (r) { return '<span class="mono small">' + e(r.job) + "</span>"; } },
          { label: "N", w: "60px", cell: function (r) { return e(r.n); } },
          { label: "P95", w: "80px", cell: function (r) { return e(r.p95_ms + "ms"); } },
          { label: "MAX", w: "80px", cell: function (r) { return e(r.max_ms + "ms"); } }
        ], (p.jobs || []).slice(0, 10), { minWidth: 420 });
    }
  });
  UI.widget("diag-links", {
    title: "PROBES",
    render: function (el) {
      el.innerHTML = C.links([
        ["/api/debug/binance", "Binance probe", "can this server reach Binance?"], ["/api/debug/signals", "Why no signal", "per-coin signal reasons right now"],
        ["/api/pipeline", "Signal funnel", "24h: setups, filters, skips"], ["/api/debug/null-test", "Null test", "do signals beat random entries?"],
        ["/api-docs", "API list", "every endpoint with Fetch and Copy"]
      ]);
    }
  });
  UI.view("system/diagnostics", [[{ w: "feed-state", size: "grow" }, { w: "diag-links", size: "side" }], ["perf"]]);
})();
