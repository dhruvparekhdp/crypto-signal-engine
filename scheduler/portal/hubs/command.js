/* Command hub: Overview · Signals · Watchlist (tools: Price outlook, Market moves, Chart). */
(function () {
  "use strict";
  var U = UI.U, C = UI.C, F = UI.F, e = U.esc;

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

  // ------------------------------------------------------------- overview
  UI.widget("pipeline", {
    title: "AGENT PIPELINE", uses: ["coins", "swing", "signals", "status", "budget"],
    mount: function (el, ctx) {
      ctx.s.stage = 0;
      el.innerHTML = '<div class="flow"><div class="flow-fill"></div></div><div class="agents"></div>';
      ctx.every(function () { ctx.s.stage = (ctx.s.stage + 1) % AGENTS.length; ctx.render(); }, 1600);
    },
    render: function (el, D, ctx) {
      var g = F.regime(D), open = F.openSwing(D), scan = (D.swing && D.swing.scan) || {}, risk = F.riskPct(D);
      var keys = Object.keys(scan), lastClose = 0;
      keys.forEach(function (k) { lastClose = Math.max(lastClose, scan[k].bar_close || 0); });
      var sigs = (D.signals && D.signals.signals) || [];
      var pending = sigs.filter(function (s) { return !s.outcome || s.outcome === "pending"; }).length;
      var top = null; ((D.budget && D.budget.models) || []).forEach(function (m) { var sh = F.budgetShare(m); if (sh != null && (!top || sh > top.sh)) top = { m: m.model, sh: sh }; });
      var maxLev = D.swing && D.swing.rules ? D.swing.rules.max_leverage : null;
      var metrics = [
        (D.coins || []).length + " coins live",
        D.status && D.status.symbols_tracked != null ? D.status.symbols_tracked + " symbols" : "shadow",
        g.known ? "BTC vol " + Math.round(g.rank * 100) + "th pctl" : "no reading",
        keys.length ? keys.length + " bars · " + (lastClose ? U.ago(lastClose) : "—") : "waiting",
        risk != null ? risk + "% · ≤" + (maxLev || "?") + "x" : "—",
        open.length + " open",
        pending + " running",
        top ? Math.round(top.sh * 100) + "% " + top.m.split("/")[0] : "—"
      ];
      var st = ctx.s.stage;
      el.querySelector(".agents").innerHTML = AGENTS.map(function (a, i) {
        var active = i === st, blocked = i === 2 && g.closed;
        return '<div class="agent' + (active ? " active" : "") + (blocked ? " blocked" : "") + '"><div class="a-top"><span>0' + (i + 1) + "</span><span>" +
          (blocked ? "BLOCKING" : active ? "WORKING" : "READY") + "</span></div><b>" + e(a[0]) + "</b><p>" + e(a[1]) + '</p><span class="metric">' + e(metrics[i]) + "</span></div>";
      }).join("");
      el.querySelector(".flow-fill").style.width = ((st + 1) / AGENTS.length * 100).toFixed(1) + "%";
      ctx.right("HANDOFF → " + AGENTS[st][0].toUpperCase());
    }
  });

  UI.widget("chart", {
    uses: ["coins"],
    mount: function (el, ctx) {
      el.innerHTML = '<div class="chart-head"><div class="col"><span class="mono dim small" data-r="label"></span><span class="big-price" data-r="price">—</span></div><span class="chg-pill" data-r="chg">—</span></div>' +
        '<div class="chart"><svg viewBox="0 0 1000 280" preserveAspectRatio="none" aria-hidden="true"><polygon class="k-band"></polygon><polyline class="k-line k-up"></polyline><polyline class="k-line k-dn"></polyline><polyline class="k-mid"></polyline></svg>' +
        '<div data-r="candles"></div><div class="last-line"></div><div class="last-tag"></div><div class="chart-msg"></div></div><div class="coins" role="group" aria-label="Pick a coin"></div>';
      ctx.s.use = function (sym) {
        if (ctx.s.sym === sym) return;
        if (ctx.s.unc) ctx.s.unc();
        ctx.s.sym = sym; ctx.s.form = null;
        var key = "candles:" + sym;
        UI.Store.def(key, "/api/portal/candles?symbol=" + encodeURIComponent(sym) + "&tf=4h&limit=48", 120000);
        ctx.s.unc = UI.Store.use(key, function () { ctx.s.form = null; ctx.render(); });
      };
    },
    unmount: function (el, ctx) { if (ctx.s.unc) ctx.s.unc(); },
    on: { coin: function (el, ev, t, ctx) { ctx.s.use(t.dataset.sym); ctx.render(); } },
    render: function (el, D, ctx) {
      var s = ctx.s, coins = (D.coins || []).slice(0, 16);
      if (!s.sym && coins.length) s.use((F.coin(D, "BTCUSDT") || coins[0]).symbol);
      var key = coins.map(function (c) { return c.symbol; }).join();
      if (key !== s.btnKey) {
        s.btnKey = key;
        el.querySelector(".coins").innerHTML = coins.map(function (c) { return '<button class="coin" data-act="coin" data-sym="' + e(c.symbol) + '">' + e(U.base(c.symbol)) + "</button>"; }).join("");
      }
      el.querySelectorAll(".coin").forEach(function (b) { b.classList.toggle("on", b.dataset.sym === s.sym); });
      var c = F.coin(D, s.sym), store = UI.Store.src["candles:" + s.sym];
      var bars = (store && Array.isArray(store.data)) ? store.data : [];
      el.querySelector('[data-r="label"]').textContent = "TARGET :: " + (s.sym ? U.base(s.sym) + "/USDT" : "—") + " · 4H · KELTNER 20 / 2.5";
      el.querySelector('[data-r="price"]').textContent = c ? U.px(c.price) : "—";
      var chg = el.querySelector('[data-r="chg"]'); chg.textContent = c ? U.pct(c.change_24h_pct) + " 24h" : "—"; chg.classList.toggle("dn", !!c && c.change_24h_pct < 0);
      el.querySelector(".chart-msg").textContent = !store || (!store.at) ? "loading 4h bars…" : store.err ? "candles unavailable · " + store.err : bars.length ? "" : "no bars returned";
      if (c && bars.length) {
        var last = bars[bars.length - 1];
        if (!s.form) s.form = { o: last.c, h: Math.max(last.c, c.price), l: Math.min(last.c, c.price), c: c.price };
        else { s.form.c = c.price; s.form.h = Math.max(s.form.h, c.price); s.form.l = Math.min(s.form.l, c.price); }
      }
      drawCandles(el, bars.concat(s.form ? [s.form] : []), !!s.form);
    }
  });
  function drawCandles(el, ks, forming) {
    var box = el.querySelector('[data-r="candles"]'), svg = el.querySelector("svg"), ll = el.querySelector(".last-line"), lt = el.querySelector(".last-tag");
    if (ks.length < 3) { box.innerHTML = ""; svg.querySelectorAll("polygon,polyline").forEach(function (p) { p.setAttribute("points", ""); }); ll.style.display = lt.style.display = "none"; return; }
    var H = el.querySelector(".chart").clientHeight || 280, N = ks.length, ema = [], atr = [], m = ks[0].c, a = ks[0].h - ks[0].l;
    ks.forEach(function (k, i) {
      m = i ? m + (k.c - m) * 2 / 21 : k.c; ema.push(m);
      var tr = i ? Math.max(k.h - k.l, Math.abs(k.h - ks[i - 1].c), Math.abs(k.l - ks[i - 1].c)) : k.h - k.l;
      a = i ? a + (tr - a) / 10 : tr; atr.push(a);
    });
    var up = ema.map(function (v, i) { return v + 2.5 * atr[i]; }), dn = ema.map(function (v, i) { return v - 2.5 * atr[i]; });
    var hi = -Infinity, lo = Infinity;
    ks.forEach(function (k, i) { hi = Math.max(hi, k.h, up[i]); lo = Math.min(lo, k.l, dn[i]); });
    var pv = (hi - lo) * 0.04 || 1; hi += pv; lo -= pv;
    function y(v) { return (hi - v) / (hi - lo) * H; }
    function pts(arr) { return arr.map(function (v, i) { return ((i + 0.5) * 1000 / N).toFixed(1) + "," + (y(v) * 280 / H).toFixed(1); }); }
    svg.querySelector(".k-up").setAttribute("points", pts(up).join(" "));
    svg.querySelector(".k-dn").setAttribute("points", pts(dn).join(" "));
    svg.querySelector(".k-mid").setAttribute("points", pts(ema).join(" "));
    svg.querySelector(".k-band").setAttribute("points", pts(up).concat(pts(dn).reverse()).join(" "));
    var cw = 100 / N, html = "";
    ks.forEach(function (k, i) {
      var col = k.c >= k.o ? "var(--up)" : "var(--dn)";
      html += '<div class="cw" style="left:' + (i * cw + cw / 2).toFixed(3) + "%;top:" + y(k.h).toFixed(1) + "px;height:" + Math.max(1, y(k.l) - y(k.h)).toFixed(1) + "px;background:" + col + '"></div>' +
        '<div class="cb" style="left:' + (i * cw + cw * 0.15).toFixed(3) + "%;width:" + (cw * 0.7).toFixed(3) + "%;top:" + y(Math.max(k.o, k.c)).toFixed(1) + "px;height:" + Math.max(3, Math.abs(y(k.o) - y(k.c))).toFixed(1) + "px;background:" + col + (forming && i === N - 1 ? ";opacity:.75" : "") + '"></div>';
    });
    box.innerHTML = html;
    var ly = y(ks[N - 1].c).toFixed(1) + "px";
    ll.style.display = lt.style.display = ""; ll.style.top = ly; lt.style.top = ly; lt.textContent = U.px(ks[N - 1].c);
  }

  UI.widget("radar", {
    title: "MARKET RADAR · 24H", uses: ["coins"],
    mount: function (el) {
      el.innerHTML = '<div class="radar"><div class="radar-x"></div><div class="radar-y"></div><div class="sweep"></div></div><p class="mono dim tiny center">distance from centre = 24h move · colour = direction</p>';
    },
    render: function (el, D, ctx) {
      var coins = (D.coins || []).slice(0, 16), n = coins.length, r = el.querySelector(".radar"), key = coins.map(function (c) { return c.symbol; }).join();
      if (key !== ctx.s.key) {
        ctx.s.key = key;
        r.querySelectorAll(".blip,.blip-l").forEach(function (x) { x.remove(); });
        coins.forEach(function (c, i) {
          var b = document.createElement("div"); b.className = "blip"; b.dataset.i = i;
          var l = document.createElement("span"); l.className = "blip-l"; l.dataset.i = i; l.textContent = U.base(c.symbol);
          r.appendChild(b); r.appendChild(l);
        });
      }
      var mx = 0.5; coins.forEach(function (c) { mx = Math.max(mx, Math.abs(c.change_24h_pct || 0)); });
      r.querySelectorAll(".blip,.blip-l").forEach(function (x) {
        var i = +x.dataset.i, c = coins[i]; if (!c) return;
        var ang = i * (360 / n) + 12, rad = ang * Math.PI / 180, rr = 12 + Math.min(1, Math.abs(c.change_24h_pct || 0) / mx) * 34;
        x.style.left = (50 + rr * Math.sin(rad)).toFixed(1) + "%"; x.style.top = (50 - rr * Math.cos(rad)).toFixed(1) + "%";
        if (x.classList.contains("blip")) { x.style.background = (c.change_24h_pct || 0) >= 0 ? "var(--up)" : "var(--dn)"; x.style.animationDelay = (ang / 360 * 4 - 4).toFixed(2) + "s"; }
      });
      ctx.right("SCANNING " + n);
    }
  });

  UI.widget("gate", {
    uses: ["swing", "meta"], cls: "gate-panel",
    render: function (el, D, ctx) {
      var g = F.regime(D);
      ctx.el.classList.toggle("closed", g.closed);
      el.innerHTML = '<span class="mono small strong">MARKET FILTER · BTC 30-DAY VOL</span>' +
        '<div class="gate-num"><span class="gate-val">' + (g.known ? Math.round(g.rank * 100) : "—") + '</span><span class="strong">th pctl · threshold ' + Math.round(g.max * 100) + "</span></div>" +
        '<div class="gauge"><div class="gauge-fill" style="width:' + (g.known ? Math.round(g.rank * 100) : 0) + '%"></div><div class="gauge-mark" style="left:' + (g.max * 100).toFixed(1) + '%"></div></div>' +
        '<div class="gate-word">' + (!g.known ? "NO READING YET" : g.mode !== "on" ? "FILTER " + e(String(g.mode || "off").toUpperCase()) : g.closed ? "CLOSED" : "OPEN") + "</div>" +
        '<span class="small">' + (!g.known ? "The first swing scan after a restart records it." : g.closed ? "Top third of its past year: new swing signals are skipped and replayed on paper only." :
          g.mode === "on" ? "Below the top third of its past year: swing signals are traded." : "The filter is not deciding trades in this mode.") + "</span>";
    }
  });

  function traceFor(s, g) {
    var dist = s.price && s.stop ? Math.abs(s.price - s.stop) / s.price * 100 : null;
    return [
      "> scanner   :: " + U.name(s.strategy) + " fired " + String(s.direction).toUpperCase() + " on " + s.symbol + " " + (s.timeframe || "") + " @ " + U.px(s.price),
      s.btc_vol_rank == null ? "> filter    :: no market-filter tag on this signal" :
        "> filter    :: btc 30d vol pctl " + Math.round(s.btc_vol_rank * 100) + (s.filter_skip ? " >= " : " < ") + Math.round(g.max * 100) + " -> " + (s.filter_skip ? "SKIP" : "PASS"),
      "> risk      :: stop " + U.px(s.stop) + (dist != null ? " (" + dist.toFixed(1) + "%)" : "") + " · target " + U.px(s.target),
      "> exec      :: " + (s.status === "TRADED" ? "paper trade opened" : "not traded · " + (s.skip_reason_text || s.skip_reason)),
      "> replay    :: " + (!s.outcome || s.outcome === "pending" ? "running on 1h bars -> stop | 3R | time limit" : "resolved " + s.outcome.toUpperCase() + " " + U.pct(s.pnl_pct)),
      "> logged    :: " + U.ago(s.time)
    ].join("\n");
  }
  UI.widget("trace", {
    uses: ["signals", "swing", "meta"], cls: "trace",
    mount: function (el, ctx) {
      el.innerHTML = '<div class="trace-head"><h2>DECISION TRACE</h2><span class="st-pill neutral">—</span></div><pre class="trace-body"></pre>';
      ctx.s.full = ""; ctx.s.n = 0;
      ctx.every(function () { if (ctx.s.n < ctx.s.full.length) { ctx.s.n = Math.min(ctx.s.full.length, ctx.s.n + 3); el.querySelector(".trace-body").textContent = ctx.s.full.slice(0, ctx.s.n); } }, 40);
    },
    render: function (el, D, ctx) {
      var list = (D.signals && D.signals.signals) || [], s = list[0], pill = el.querySelector(".st-pill"), h = el.querySelector("h2");
      if (!s) {
        h.textContent = "DECISION TRACE"; pill.textContent = "—"; pill.className = "st-pill neutral";
        var err = UI.Store.src.signals.err;
        ctx.s.full = err ? "> signals unavailable :: " + err : D.signals ? "> no swing signals in the last 14 days" : "> loading…";
        ctx.s.n = ctx.s.full.length; el.querySelector(".trace-body").textContent = ctx.s.full; return;
      }
      h.textContent = "DECISION TRACE · " + U.base(s.symbol) + " " + String(s.direction).toUpperCase();
      pill.textContent = s.status; pill.className = "st-pill" + (s.status === "TRADED" ? "" : s.status === "FILTER BLOCK" ? " skip" : " neutral");
      var full = traceFor(s, F.regime(D));
      if (ctx.s.id !== s.id) { ctx.s.id = s.id; ctx.s.n = 0; }
      ctx.s.full = full;
    }
  });

  UI.widget("stream", {
    uses: ["coins"], bare: true,
    mount: function (el) { el.innerHTML = '<div class="panel stream"><div class="stream-cols"></div><div class="stream-foot mono tiny"><span>MARKET STREAM</span><span class="acc"></span></div></div>'; },
    render: function (el, D, ctx) {
      var coins = D.coins || [];
      if (!coins.length || (ctx.s.at && Date.now() - ctx.s.at < 30000 && ctx.s.n === coins.length)) return;
      ctx.s.at = Date.now(); ctx.s.n = coins.length;
      var lines = coins.map(function (c) {
        return U.base(c.symbol).padEnd(5) + " " + U.pct(c.change_24h_pct) + "\n " + U.px(c.price) + "\n rsi " + (U.num(c.rsi_14) ? Math.round(c.rsi_14) : "—") + " vr " + (U.num(c.volume_ratio) ? c.volume_ratio.toFixed(1) : "—");
      });
      var html = "";
      for (var k = 0; k < 2; k++) {
        var part = lines.filter(function (_, i) { return i % 2 === k; }).join("\n");
        html += '<div class="stream-col" style="animation-duration:' + (14 + k * 6) + "s;color:" + (k ? "var(--dn)" : "var(--up)") + '">' + e(part + "\n" + part) + "</div>";
      }
      el.querySelector(".stream-cols").innerHTML = html;
      el.querySelector(".stream-foot .acc").textContent = coins.length + " coins";
    }
  });

  function statusCls(s) { return s === "TRADED" ? "" : s === "FILTER BLOCK" ? "skip" : "neutral"; }
  UI.widget("feed", {
    title: "SIGNAL FEED", uses: ["signals"],
    render: function (el, D, ctx) {
      ctx.right("SWING BOOK · LAST 14 DAYS");
      var list = (D.signals && D.signals.signals) || [];
      if (!list.length) { el.innerHTML = C.state(D, "signals", D.signals && D.signals.error ? "Signals unavailable: " + D.signals.error : "No swing signals in the last 14 days."); return; }
      var prev = ctx.s.top; ctx.s.top = list[0].id;
      el.innerHTML = '<div class="scroll-x"><div class="feed">' + list.slice(0, 12).map(function (s, i) {
        return '<div class="frow' + (i === 0 && prev && prev !== s.id ? " new" : "") + '"><span class="dim">' + e(U.ago(s.time)) + "</span><b>" + e(U.base(s.symbol)) + "</b>" + C.side(s.direction) +
          "<span>" + e(U.name(s.strategy)) + " · " + e(s.timeframe || "") + '</span><span class="dim">' + e(U.px(s.price)) + (s.outcome && s.outcome !== "pending" ? " · " + e(s.outcome) + " " + e(U.pct(s.pnl_pct)) : "") +
          '</span><span title="' + e(s.skip_reason_text) + '">' + C.tag(s.status, statusCls(s.status)) + "</span></div>";
      }).join("") + "</div></div>";
    }
  });

  var ASKS = ["Is the filter open?", "Why the last skip?", "What is my risk?", "Is anything live?"];
  function answer(i, D) {
    var g = F.regime(D), open = F.openSwing(D), risk = F.riskPct(D), sigs = (D.signals && D.signals.signals) || [];
    if (i === 0) return !g.known ? "No market-filter reading yet. The first swing scan after a restart records Bitcoin's 30-day volatility rank." :
      "The filter is " + (g.mode !== "on" ? "in '" + g.mode + "' mode" : g.closed ? "CLOSED" : "OPEN") + ". Bitcoin's 30-day volatility is at the " + Math.round(g.rank * 100) +
      "th percentile of its past year; at or above " + Math.round(g.max * 100) + " new swing signals are skipped.";
    if (i === 1) {
      var sk = sigs.filter(function (s) { return s.status !== "TRADED"; })[0];
      return sk ? U.base(sk.symbol) + " " + sk.direction + " (" + U.name(sk.strategy) + ", " + U.ago(sk.time) + ") was not traded: " + (sk.skip_reason_text || sk.skip_reason) +
        (sk.btc_vol_rank != null ? ". BTC vol was at the " + Math.round(sk.btc_vol_rank * 100) + "th percentile." : ".") : "Every swing signal in the last 14 days became a paper trade.";
    }
    if (i === 2) {
      if (!D.swing) return "Still loading the swing book.";
      var live = open.filter(function (o) { return o.R != null; }), sum = live.reduce(function (a, o) { return a + o.R; }, 0);
      return open.length + " open swing trade" + (open.length === 1 ? "" : "s") + (risk != null ? " at " + risk + "% risk each: worst case −" + (open.length * risk).toFixed(1) + "% of the wallet if every stop hits" : "") +
        (live.length ? ". Right now they sum to " + U.rr(sum) + "." : ".");
    }
    return F.live(D) ? "Live trading mode is '" + D.meta.live_trading_mode + "'. Check Settings before doing anything else." :
      "Nothing is live. Every trade on this page is a paper trade; the live execution book is not wired into the engine.";
  }
  UI.widget("ask", {
    title: "ASK THE DESK", uses: ["swing", "signals", "meta", "coins"],
    on: { ask: function (el, ev, t, ctx) { ctx.s.q = +t.dataset.i; ctx.render(); } },
    render: function (el, D, ctx) {
      var q = ctx.s.q == null ? -1 : ctx.s.q;
      el.innerHTML = '<div class="ask-btns">' + ASKS.map(function (a, i) { return '<button data-act="ask" data-i="' + i + '" class="' + (q === i ? "on" : "") + '">' + e(a) + "</button>"; }).join("") +
        '</div><div class="ask-answer">' + e(q < 0 ? "Pick a question. Answers are built from the live state on this page." : answer(q, D)) + "</div>";
    }
  });

  UI.view("command/overview", [
    ["pipeline"],
    [{ w: "chart", size: "grow" }, { stack: ["radar", "gate"], size: "side" }],
    [{ w: "trace", size: "grow" }, { w: "stream", size: "side" }],
    [{ w: "feed", size: "grow" }, { w: "ask", size: "side" }]
  ]);

  // ------------------------------------------------------------- signals (was Dashboard + Signals)
  function moveOf(s) { return s.target_price && s.current_price ? Math.abs(s.target_price - s.current_price) / s.current_price * 100 : 0; }
  function minTarget(D) { return D.accuracy && U.num(D.accuracy.min_target_pct) ? D.accuracy.min_target_pct : 0.4; }
  function sigStatus(s, min) { return s.veto_reason ? "VETOED" : moveOf(s) >= min ? "VIABLE" : "SUB-COST"; }

  UI.widget("signals-summary", {
    bare: true, uses: ["history7", "accuracy"],
    render: function (el, D) {
      var list = Array.isArray(D.history7) ? D.history7 : [], min = minTarget(D);
      if (!Array.isArray(D.history7)) { el.innerHTML = C.state(D, "history7"); return; }
      var viable = list.filter(function (s) { return sigStatus(s, min) === "VIABLE"; }).length;
      var vetoed = list.filter(function (s) { return s.veto_reason; }).length;
      var swing = list.filter(function (s) { return (s.trade_mode || "") === "swing"; }).length;
      el.innerHTML = '<div class="hero-band"><b>' + list.length + " signals · 7 days</b><span>" + viable + " cleared the " + min.toFixed(2) + "% cost floor, " + vetoed +
        " were vetoed or skipped, " + swing + " came from the swing book. Everything older is in Evidence › History.</span></div>";
    }
  });
  UI.widget("signals-table", {
    title: "EVERY SIGNAL · LAST 7 DAYS", uses: ["history7", "accuracy"],
    mount: function (el, ctx) {
      ctx.s.f = { sym: "", dir: "", st: "", q: "" }; ctx.s.t = { sort: null };
      el.innerHTML = '<div class="toolbar"></div><div data-slot="t"></div>';
    },
    on: {
      sym: function (el, ev, t, ctx) { ctx.s.f.sym = t.value; ctx.render(); },
      dir: function (el, ev, t, ctx) { ctx.s.f.dir = t.value; ctx.render(); },
      st: function (el, ev, t, ctx) { ctx.s.f.st = t.value; ctx.render(); },
      q: function (el, ev, t, ctx) { ctx.s.f.q = t.value.toLowerCase(); ctx.render(); },
      sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); }
    },
    render: function (el, D, ctx) {
      if (!Array.isArray(D.history7)) { el.querySelector('[data-slot="t"]').innerHTML = C.state(D, "history7"); return; }
      var all = D.history7, min = minTarget(D), f = ctx.s.f;
      var syms = Array.from(new Set(all.map(function (s) { return s.symbol; }))).sort();
      if (!ctx.s.tb || ctx.s.tb !== syms.join()) {
        ctx.s.tb = syms.join();
        el.querySelector(".toolbar").innerHTML = C.select("sym", "Coin", [["", "All"]].concat(syms.map(function (s) { return [s, U.base(s)]; })), f.sym) +
          C.select("dir", "Side", [["", "Both"], ["long", "Long"], ["short", "Short"]], f.dir) +
          C.select("st", "Status", [["", "All"], ["VIABLE", "Viable"], ["SUB-COST", "Sub-cost"], ["VETOED", "Vetoed"]], f.st) + C.search("q", "Search setup or reason…", f.q);
      }
      var rows = all.filter(function (s) {
        var st = sigStatus(s, min);
        return (!f.sym || s.symbol === f.sym) && (!f.dir || s.direction === f.dir) && (!f.st || st === f.st) &&
          (!f.q || (s.signal_type + " " + (s.veto_reason || "")).toLowerCase().indexOf(f.q) >= 0);
      });
      ctx.right(rows.length + " of " + all.length);
      el.querySelector('[data-slot="t"]').innerHTML = C.table([
        { label: "FIRED", w: "110px", cell: function (s) { return '<span class="dim">' + e(U.stamp(s.timestamp)) + "</span>"; }, sort: function (s) { return s.timestamp; } },
        { label: "COIN", w: "70px", cell: function (s) { return "<b>" + e(U.base(s.symbol)) + "</b>"; }, sort: function (s) { return s.symbol; } },
        { label: "SETUP", w: "minmax(0,1.3fr)", cell: function (s) { return e(U.name(s.signal_type)) + ' <span class="dim">' + e(s.timeframe || "") + "</span>"; } },
        { label: "SIDE", w: "70px", cell: function (s) { return C.side(s.direction); } },
        { label: "MODE", w: "80px", cell: function (s) { return '<span class="dim">' + e(s.trade_mode || "intraday") + "</span>"; } },
        { label: "MOVE", w: "80px", cell: function (s) { return e(moveOf(s).toFixed(2) + "%"); }, sort: moveOf },
        { label: "CONF", w: "60px", cell: function (s) { return e(s.confidence + "%"); }, sort: function (s) { return s.confidence; } },
        { label: "STATUS", w: "minmax(0,1fr)", cell: function (s) { var st = sigStatus(s, min); return C.tag(st, st === "VIABLE" ? "" : st === "VETOED" ? "skip" : "neutral") + (s.veto_reason ? ' <span class="dim tiny">' + e(s.veto_reason) + "</span>" : ""); } },
        { label: "OUTCOME", w: "90px", cell: function (s) { return '<span class="dim">' + e(s.outcome || "pending") + "</span>"; } }
      ], rows, { state: ctx.s.t, minWidth: 900, empty: "No signals match these filters." });
    }
  });
  UI.widget("signals-refused", {
    title: "WHY SIGNALS WERE REFUSED", uses: ["history7", "accuracy"],
    render: function (el, D) {
      if (!Array.isArray(D.history7)) { el.innerHTML = C.state(D, "history7"); return; }
      var min = minTarget(D), why = {};
      D.history7.forEach(function (s) {
        if (moveOf(s) < min) why["Below the " + min.toFixed(2) + "% cost floor"] = (why["Below the " + min.toFixed(2) + "% cost floor"] || 0) + 1;
        if (s.veto_reason) why[s.veto_reason] = (why[s.veto_reason] || 0) + 1;
      });
      var rows = Object.keys(why).map(function (k) { return [k, why[k], String(why[k]), "dn"]; }).sort(function (a, b) { return b[1] - a[1]; });
      el.innerHTML = rows.length ? C.bars(rows) : C.empty("Nothing refused in the last 7 days.");
    }
  });
  UI.view("command/signals", [["signals-summary"], [{ w: "signals-table", size: "grow" }, { w: "signals-refused", size: "side" }]]);

  // ------------------------------------------------------------- watchlist
  UI.widget("watch-list", {
    title: "ON THE WATCHLIST", uses: ["coins"],
    mount: function (el, ctx) { ctx.s.t = { sort: null }; },
    on: {
      sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); },
      remove: function (el, ev, t) {
        var sym = t.dataset.sym;
        if (!window.confirm("Stop tracking " + U.base(sym) + "?")) return;
        t.disabled = true;
        UI.Net.post("/api/crypto/watchlist/remove", { symbol: sym.toLowerCase() })
          .then(function () { UI.toast({ status: "REMOVED", symbol: sym, direction: "", strategy: "watchlist" }); UI.Store.fetch("coins"); })
          .catch(function (err) { window.alert("Could not remove: " + err.message); t.disabled = false; });
      }
    },
    render: function (el, D, ctx) {
      if (!D.coins) { el.innerHTML = C.state(D, "coins"); return; }
      ctx.right(D.coins.length + " pairs");
      el.innerHTML = C.table([
        { label: "PAIR", w: "110px", cell: function (c) { return "<b>" + e(c.symbol) + "</b>"; }, sort: function (c) { return c.symbol; } },
        { label: "PRICE", w: "120px", cell: function (c) { return e(U.px(c.price)); } },
        { label: "24H", w: "90px", cell: function (c) { return C.signed(c.change_24h_pct); }, sort: function (c) { return c.change_24h_pct; } },
        { label: "VOLUME 24H", w: "110px", cell: function (c) { return e(U.compact(c.volume_24h)); }, sort: function (c) { return c.volume_24h || 0; } },
        { label: "VOL RATIO", w: "90px", cell: function (c) { return e(U.num(c.volume_ratio) ? c.volume_ratio.toFixed(2) : "—"); }, sort: function (c) { return c.volume_ratio || 0; } },
        { label: "RSI", w: "60px", cell: function (c) { return e(U.num(c.rsi_14) ? Math.round(c.rsi_14) : "—"); }, sort: function (c) { return c.rsi_14 || 0; } },
        { label: "ATR", w: "80px", cell: function (c) { return e(U.num(c.atr_pct) ? c.atr_pct.toFixed(2) + "%" : "—"); }, sort: function (c) { return c.atr_pct || 0; } },
        { label: "UPDATED", w: "90px", cell: function (c) { return '<span class="dim">' + e(U.ago(c.timestamp)) + "</span>"; } },
        { label: "", w: "90px", cell: function (c) { return '<button class="mini" data-act="remove" data-sym="' + e(c.symbol) + '">Remove</button>'; } }
      ], D.coins, { state: ctx.s.t, minWidth: 860, empty: "No pairs on the watchlist." });
    }
  });
  UI.widget("watch-add", {
    title: "ADD A PAIR", uses: [],
    mount: function (el, ctx) {
      el.innerHTML = '<p class="small dim">Search Binance, check price and volume, then add the exact pair. For gold, search <b>gold</b> (PAXG, XAUT).</p>' +
        '<div class="toolbar">' + C.search("find", "Search Binance: gold, sol, pepe…", "") + '</div><div data-slot="r" aria-live="polite"></div>';
      ctx.s.seq = 0;
    },
    on: {
      find: function (el, ev, t, ctx) {
        clearTimeout(ctx.s.timer);
        var q = t.value.trim();
        ctx.s.timer = setTimeout(function () {
          var seq = ++ctx.s.seq, box = el.querySelector('[data-slot="r"]');
          box.innerHTML = C.empty("Searching Binance…");
          UI.Net.get("/api/binance/symbols?q=" + encodeURIComponent(q)).then(function (d) {
            if (seq !== ctx.s.seq) return;
            if (!d.available) { box.innerHTML = C.empty(d.error || "Binance did not answer. Try again in a minute."); return; }
            if (!d.results.length) { box.innerHTML = C.empty('No Binance USDT pair matches "' + q + '".'); return; }
            box.innerHTML = '<div class="results">' + d.results.map(function (r) {
              return '<div class="result"><div><b>' + e(r.symbol.toUpperCase()) + "</b> " + C.tag(r.pair || "", "neutral") + (r.futures === true ? " " + C.tag("perp", "") : r.futures === false ? " " + C.tag("spot only", "neutral") : "") +
                '<div class="mono tiny dim">' + e(r.price != null ? "$" + U.px(r.price) : "—") + " · 24h " + e(U.pct(r.change_pct)) + " · vol " + e(U.compact(r.quote_volume_24h)) + "</div></div>" +
                (r.on_watchlist ? '<span class="mono tiny dim">on list</span>' : '<button class="mini" data-act="add" data-sym="' + e(r.symbol) + '">Add</button>') + "</div>";
            }).join("") + "</div>";
          }).catch(function (err) { if (seq === ctx.s.seq) box.innerHTML = C.empty("Search failed: " + err.message); });
        }, 300);
      },
      add: function (el, ev, t) {
        t.disabled = true;
        UI.Net.post("/api/crypto/watchlist/add", { symbol: t.dataset.sym.toLowerCase() })
          .then(function () { t.textContent = "Added"; UI.Store.fetch("coins"); })
          .catch(function (err) { window.alert("Could not add: " + err.message); t.disabled = false; });
      }
    },
    render: function () {}
  });
  UI.view("command/watchlist", [[{ w: "watch-list", size: "grow" }, { w: "watch-add", size: "side" }]]);
})();
