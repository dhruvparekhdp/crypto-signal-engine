/* Book hub: Swing book · Paper cycle · Session guard (tools: Simulator, Journal). */
(function () {
  "use strict";
  var U = UI.U, C = UI.C, F = UI.F, e = U.esc;

  // ------------------------------------------------------------- swing book
  UI.widget("swing-positions", {
    bare: true, uses: ["swing", "coins"],
    render: function (el, D) {
      if (!D.swing) { el.innerHTML = C.state(D, "swing"); return; }
      var open = F.openSwing(D);
      if (!open.length) { el.innerHTML = C.empty("No open swing trades."); return; }
      el.innerHTML = '<div class="cards">' + open.map(function (o) {
        var p = o.p, left = p.expires_at ? (new Date(p.expires_at).getTime() - Date.now()) / 86400000 : null;
        return '<div class="pos"><div class="pos-top"><b>' + e(U.base(p.symbol)) + "</b>" + C.side(p.side) + "</div>" +
          '<div class="mono small dim">' + e(U.name(p.strategy)) + (p.leverage ? " · " + e(p.leverage) + "x" : "") + "</div>" +
          '<div class="pos-r' + (o.R < 0 ? " dn" : "") + '">' + U.rr(o.R) + "</div>" + C.rbar(o.R) +
          '<div class="pos-scale"><span>STOP ' + e(U.px(p.stop)) + "</span><span>0</span><span>TARGET " + e(U.px(p.target)) + "</span></div>" +
          '<div class="pos-foot"><span>in ' + e(U.px(p.entry)) + "</span><span>now " + e(U.px(o.mark)) + "</span><b>" + (left == null ? "—" : left > 0 ? left.toFixed(1) + "d left" : "expiring") + "</b></div></div>";
      }).join("") + "</div>";
    }
  });
  UI.widget("swing-closed", {
    title: "CLOSED SWING TRADES", uses: ["swing"],
    mount: function (el, ctx) { ctx.s.t = { sort: null }; },
    on: { sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); } },
    render: function (el, D, ctx) {
      if (!D.swing) { el.innerHTML = C.state(D, "swing"); return; }
      var cl = D.swing.closed || {};
      ctx.right(cl.n ? cl.n + " closed · win rate " + (cl.win_rate != null ? Math.round(cl.win_rate * 100) + "%" : "—") + " · avg " + U.rr(cl.avg_r) + " · total " + U.rr(cl.total_r) : "");
      el.innerHTML = C.table([
        { label: "COIN", w: "80px", cell: function (t) { return "<b>" + e(U.base(t.symbol)) + "</b>"; }, sort: function (t) { return t.symbol; } },
        { label: "SIDE", w: "80px", cell: function (t) { return C.side(t.side); } },
        { label: "STRATEGY", w: "minmax(0,1.4fr)", cell: function (t) { return e(U.name(t.strategy)); } },
        { label: "R", w: "90px", cell: function (t) { return C.signed(t.r, U.rr); }, sort: function (t) { return t.r; } },
        { label: "NET", w: "100px", cell: function (t) { return C.signed(t.net_pnl, function (x) { return U.inr(x, true); }); }, sort: function (t) { return t.net_pnl; } },
        { label: "EXIT", w: "minmax(0,1fr)", cell: function (t) { return e(t.exit_reason || ""); } },
        { label: "CLOSED", w: "100px", cell: function (t) { return '<span class="dim">' + e(U.ago(t.closed_at)) + "</span>"; }, sort: function (t) { return t.closed_at || ""; } }
      ], (cl.last || []).slice().reverse(), { state: ctx.s.t, empty: "No closed swing trades yet." });
    }
  });
  UI.widget("swing-takeskip", {
    title: "TAKEN vs SKIPPED", uses: ["swing"],
    render: function (el, D) {
      var sb = D.swing && D.swing.regime_filter && D.swing.regime_filter.signals;
      if (!D.swing) { el.innerHTML = C.state(D, "swing"); return; }
      if (!sb) { el.innerHTML = C.empty("No tagged swing signals yet."); return; }
      var tk = sb.take || {}, sk = sb.skip || {};
      function n(g) { return (g.n || 0) + (g.running || 0); }
      el.innerHTML = '<p class="small dim">Every swing signal, traded or skipped by the market filter, is replayed on 1h bars so the filter can be audited.</p>' +
        C.bars([["TAKE", n(tk), n(tk) + " · avg " + U.rr(tk.avg_r), ""], ["SKIP", n(sk), n(sk) + " · avg " + U.rr(sk.avg_r), "dn"]]) +
        '<p class="mono tiny dim">' + (tk.n || 0) + " + " + (sk.n || 0) + " resolved · " + ((tk.running || 0) + (sk.running || 0)) + " running</p>";
    }
  });
  UI.widget("swing-rules", {
    title: "RULES IN FORCE", uses: ["swing"],
    render: function (el, D) {
      var r = D.swing && D.swing.rules; if (!r) { el.innerHTML = C.state(D, "swing"); return; }
      el.innerHTML = C.tiles([
        ["TIMEFRAMES", r.timeframes || "—"], ["STOP", r.stop || "—"], ["TARGET", r.target || "—"],
        ["TIME LIMIT", r.time_limit_minutes ? Math.round(r.time_limit_minutes / 1440) + " days" : "—"],
        ["RISK / TRADE", F.riskPct(D) + "%"], ["MAX LEVERAGE", (r.max_leverage || "—") + "x"],
        ["MAX OPEN", r.max_open ? r.max_open : "no limit"], ["EXCLUDED", String(r.excluded_coins || "—").toUpperCase()]
      ]);
    }
  });
  UI.view("book/swing", [["swing-positions"], [{ w: "swing-closed", size: "grow" }, { stack: ["swing-takeskip", "swing-rules"], size: "side" }]]);

  // ------------------------------------------------------------- paper cycle (was Paper Trading)
  UI.widget("paper-strip", {
    bare: true, uses: ["paper"],
    render: function (el, D) {
      var p = D.paper; if (!p) { el.innerHTML = C.state(D, "paper"); return; }
      if (!p.running) {
        el.innerHTML = '<div class="hero-band"><b>No paper cycle running</b><span>' + (p.enabled ? "Paper trading is on; the next signal that passes the gates starts a cycle." : "Paper trading is switched off in Settings.") + "</span></div>";
        return;
      }
      var c = p.cycle || {}, s = p.summary || {};
      var prog = c.target_wallet > c.starting_wallet ? (p.equity - c.starting_wallet) / (c.target_wallet - c.starting_wallet) : null;
      el.innerHTML = C.tiles([
        ["EQUITY", U.inr(p.equity), "from " + U.inr(c.starting_wallet), p.equity >= c.starting_wallet ? "up" : "dn"],
        ["WALLET", U.inr(c.wallet), "peak " + U.inr(c.peak_wallet)],
        ["UNREALISED", U.inr(p.unrealised, true), (p.positions || []).length + " open", p.unrealised >= 0 ? "up" : "dn"],
        ["TARGET", U.inr(c.target_wallet), prog != null ? Math.round(prog * 100) + "% of the way" : ""],
        ["HIT RATE", s.trades ? s.win_rate_pct + "%" : "—", s.trades ? s.trades + " closed" : "needs closed trades"],
        ["NET P&L", U.inr(s.net_pnl, true), "expectancy " + U.inr(s.expectancy_per_trade, true), s.net_pnl >= 0 ? "up" : "dn"],
        ["COSTS", U.inr((s.trading_fees || 0) + (s.funding_paid || 0)), s.costs_as_pct_of_gross != null ? s.costs_as_pct_of_gross + "% of gross" : "", "dn"],
        ["CYCLE", "#" + c.id, "started " + U.ago(c.started_at)]
      ]);
    }
  });
  UI.widget("paper-open", {
    title: "OPEN POSITIONS", uses: ["paper"],
    mount: function (el, ctx) { ctx.s.t = { sort: null }; },
    on: { sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); } },
    render: function (el, D, ctx) {
      var p = D.paper; if (!p) { el.innerHTML = C.state(D, "paper"); return; }
      var rows = p.positions || [];
      ctx.right(rows.length + " open");
      el.innerHTML = C.table([
        { label: "COIN", w: "80px", cell: function (r) { return "<b>" + e(U.base(r.symbol)) + "</b>"; }, sort: function (r) { return r.symbol; } },
        { label: "SIDE", w: "80px", cell: function (r) { return C.side(r.side); } },
        { label: "MODE", w: "80px", cell: function (r) { return '<span class="dim">' + e(r.trade_mode) + "</span>"; } },
        { label: "ENTRY", w: "100px", cell: function (r) { return e(U.px(r.entry)); } },
        { label: "MARK", w: "100px", cell: function (r) { return e(U.px(r.mark)); } },
        { label: "STOP", w: "100px", cell: function (r) { return e(U.px(r.stop)) + (r.trailing ? ' <span class="acc tiny">trail</span>' : ""); } },
        { label: "TARGET", w: "100px", cell: function (r) { return e(U.px(r.target)); } },
        { label: "MARGIN", w: "90px", cell: function (r) { return e(U.inr(r.margin)); }, sort: function (r) { return r.margin; } },
        { label: "NET", w: "100px", cell: function (r) { return C.signed(r.unrealised, function (x) { return U.inr(x, true); }); }, sort: function (r) { return r.unrealised; } },
        { label: "ROE", w: "80px", cell: function (r) { return C.signed(r.roe_pct); }, sort: function (r) { return r.roe_pct; } },
        { label: "OPENED", w: "90px", cell: function (r) { return '<span class="dim">' + e(U.ago(r.opened_at)) + "</span>"; }, sort: function (r) { return r.opened_at || ""; } }
      ], rows, { state: ctx.s.t, minWidth: 1000, empty: p.running ? "No open positions." : "No cycle running." });
    }
  });
  UI.widget("paper-history", {
    title: "HISTORY", uses: ["paper"],
    mount: function (el, ctx) {
      ctx.s.f = { sym: "", side: "", setup: "", exit: "", res: "", q: "" }; ctx.s.t = { sort: null };
      el.innerHTML = '<div class="toolbar"></div><div data-slot="t"></div><div data-slot="sum"></div>';
    },
    on: {
      sym: function (el, ev, t, ctx) { ctx.s.f.sym = t.value; ctx.render(); },
      side: function (el, ev, t, ctx) { ctx.s.f.side = t.value; ctx.render(); },
      setup: function (el, ev, t, ctx) { ctx.s.f.setup = t.value; ctx.render(); },
      exit: function (el, ev, t, ctx) { ctx.s.f.exit = t.value; ctx.render(); },
      res: function (el, ev, t, ctx) { ctx.s.f.res = ctx.s.f.res === t.dataset.v ? "" : t.dataset.v; ctx.s.tb = null; ctx.render(); },
      q: function (el, ev, t, ctx) { ctx.s.f.q = t.value.toLowerCase(); ctx.render(); },
      reset: function (el, ev, t, ctx) { ctx.s.f = { sym: "", side: "", setup: "", exit: "", res: "", q: "" }; ctx.s.tb = null; ctx.render(); },
      sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); }
    },
    render: function (el, D, ctx) {
      var p = D.paper; if (!p) { el.querySelector('[data-slot="t"]').innerHTML = C.state(D, "paper"); return; }
      var all = p.trades || [], f = ctx.s.f;
      function uniq(k) { return Array.from(new Set(all.map(function (t) { return t[k]; }).filter(Boolean))).sort(); }
      var key = [all.length, f.res].join();
      if (ctx.s.tb !== key) {
        ctx.s.tb = key;
        el.querySelector(".toolbar").innerHTML =
          C.select("sym", "Coin", [["", "All"]].concat(uniq("symbol").map(function (s) { return [s, U.base(s)]; })), f.sym) +
          C.select("side", "Side", [["", "Both"], ["long", "Long"], ["short", "Short"]], f.side) +
          C.select("setup", "Setup", [["", "All"]].concat(uniq("signal_type").map(function (s) { return [s, U.name(s)]; })), f.setup) +
          C.select("exit", "Exit", [["", "All"]].concat(uniq("reason")), f.exit) +
          '<span class="chipset">' + '<button class="chipbtn' + (f.res === "win" ? " on" : "") + '" data-act="res" data-v="win">Wins</button>' +
          '<button class="chipbtn' + (f.res === "loss" ? " on" : "") + '" data-act="res" data-v="loss">Losses</button></span>' +
          C.search("q", "Search…", f.q) + '<button class="mini" data-act="reset">Reset</button>';
      }
      var rows = all.filter(function (t) {
        return (!f.sym || t.symbol === f.sym) && (!f.side || t.side === f.side) && (!f.setup || t.signal_type === f.setup) && (!f.exit || t.reason === f.exit) &&
          (!f.res || (f.res === "win" ? t.net > 0 : t.net <= 0)) && (!f.q || (t.symbol + " " + t.signal_type + " " + t.reason).toLowerCase().indexOf(f.q) >= 0);
      });
      ctx.right(rows.length + " of " + all.length + " (latest 60)");
      el.querySelector('[data-slot="t"]').innerHTML = C.table([
        { label: "CLOSED", w: "110px", cell: function (t) { return '<span class="dim">' + e(U.stamp(t.closed_at)) + "</span>"; }, sort: function (t) { return t.closed_at || ""; } },
        { label: "COIN", w: "70px", cell: function (t) { return "<b>" + e(U.base(t.symbol)) + "</b>"; }, sort: function (t) { return t.symbol; } },
        { label: "SIDE", w: "80px", cell: function (t) { return C.side(t.side); } },
        { label: "SETUP", w: "minmax(0,1.2fr)", cell: function (t) { return e(U.name(t.signal_type)); } },
        { label: "HELD", w: "70px", cell: function (t) { return e(U.hours(t.hours_held)); }, sort: function (t) { return t.hours_held; } },
        { label: "GROSS", w: "90px", cell: function (t) { return C.signed(t.gross, function (x) { return U.inr(x, true); }); }, sort: function (t) { return t.gross; } },
        { label: "FEES", w: "80px", cell: function (t) { return '<span class="dn">' + e(U.inr(-(t.fees || 0))) + "</span>"; } },
        { label: "FUNDING", w: "80px", cell: function (t) { return '<span class="dn">' + e(U.inr(-(t.funding || 0))) + "</span>"; } },
        { label: "NET", w: "90px", cell: function (t) { return C.signed(t.net, function (x) { return U.inr(x, true); }); }, sort: function (t) { return t.net; } },
        { label: "EXIT", w: "minmax(0,1fr)", cell: function (t) { return '<span class="dim">' + e(t.reason || "") + "</span>"; } }
      ], rows, { state: ctx.s.t, minWidth: 1000, empty: all.length ? "No trades match these filters." : "No closed trades in this cycle yet." });
      var net = rows.reduce(function (a, t) { return a + (t.net || 0); }, 0), fees = rows.reduce(function (a, t) { return a + (t.fees || 0) + (t.funding || 0); }, 0);
      var wins = rows.filter(function (t) { return t.net > 0; }).length;
      el.querySelector('[data-slot="sum"]').innerHTML = rows.length ? '<p class="mono small">' + rows.length + " trades · " + wins + " wins (" + Math.round(wins / rows.length * 100) + "%) · net " +
        e(U.inr(net, true)) + " · costs " + e(U.inr(fees)) + "</p>" : "";
    }
  });
  UI.view("book/paper", [["paper-strip"], ["paper-open"], ["paper-history"]]);

  // ------------------------------------------------------------- session guard
  function guard(trades) {
    var notional = function (t) { return Math.abs(t.margin) * (t.leverage || 1); };
    var kept = function (t) { return t.gross ? t.net / t.gross * 100 : null; };
    var first = trades[0], last = trades[trades.length - 1];
    var drift = notional(last) / notional(first) - 1, k0 = kept(first), k1 = kept(last), flags = [];
    if (drift > 0.25 && k1 != null && k0 != null && k1 < k0)
      flags.push(["red", "Escalating size while the edge shrank", "Each trade got larger while less of the gross survived costs. Fees scale with size; the edge did not.",
        U.inr(notional(first)) + " → " + U.inr(notional(last)) + " · kept " + k0.toFixed(0) + "% → " + k1.toFixed(0) + "%"]);
    var thin = trades.filter(function (t) { return t.gross && (t.fees + t.funding) / Math.abs(t.gross) > 0.33; });
    if (thin.length) flags.push(["red", "Trades where costs took a third or more", "At this size the round trip is eating the result. The target has to clear the cost floor by a wide margin.",
      thin.slice(0, 6).map(function (t) { return t.symbol + " " + U.inr(t.gross) + " gross, " + U.inr(t.fees + t.funding) + " fees"; }).join(" · ")]);
    var quick = trades.filter(function (t) { return t.hours_held != null && t.hours_held < 0.1; });
    if (quick.length >= 2) flags.push(["amber", "Several trades held under six minutes", "Short holds capture small moves, and a small move is where the fee share is largest.", quick.length + " of " + trades.length + " trades"]);
    var flips = [];
    for (var i = 1; i < trades.length; i++) {
      var a = trades[i - 1], b = trades[i];
      if (a.symbol === b.symbol && a.side !== b.side && Math.abs(new Date(b.closed_at) - new Date(a.closed_at)) < 45 * 60000) flips.push(a.symbol + " " + a.side + "→" + b.side);
    }
    if (flips.length) flags.push(["amber", "Direction reversed in the same symbol within the hour", "Closing one side and opening the other shortly after usually means the exit was about discomfort rather than the setup changing.", flips.join(" · ")]);
    if (!flags.length) flags.push(["ok", "Nothing to flag", "Size, hold time and cost share are all steady across this session.", trades.length + " trades compared"]);
    return { drift: drift, k0: k0, k1: k1, notional: notional, kept: kept, flags: flags };
  }
  UI.widget("guard-summary", {
    bare: true, uses: ["paper"],
    render: function (el, D) {
      var p = D.paper; if (!p) { el.innerHTML = C.state(D, "paper"); return; }
      var tr = (p.trades || []).slice().reverse();
      if (!tr.length) { el.innerHTML = C.empty("No closed trades yet: flags appear once a session has trades to compare."); return; }
      var g = guard(tr);
      el.innerHTML = C.tiles([
        ["TRADES", tr.length, "this cycle"],
        ["SIZE TREND", (g.drift >= 0 ? "+" : "") + (g.drift * 100).toFixed(0) + "%", U.inr(g.notional(tr[0])) + " → " + U.inr(g.notional(tr[tr.length - 1])), g.drift > 0.25 ? "dn" : ""],
        ["MEDIAN HOLD", U.hours(U.median(tr.map(function (t) { return t.hours_held; }))), "per trade"],
        ["KEPT OF GROSS", g.k1 == null ? "—" : g.k1.toFixed(0) + "%", g.k0 != null ? "was " + g.k0.toFixed(0) + "% on the first" : "", g.k1 != null && g.k1 < 60 ? "dn" : "up"]
      ]);
    }
  });
  UI.widget("guard-flags", {
    title: "FLAGS", uses: ["paper"],
    render: function (el, D) {
      var p = D.paper; if (!p) { el.innerHTML = C.state(D, "paper"); return; }
      var tr = (p.trades || []).slice().reverse();
      el.innerHTML = tr.length ? guard(tr).flags.map(function (f) { return C.flag(f[0], f[1], f[2], f[3]); }).join("") : C.empty("Nothing to compare yet.");
    }
  });
  UI.widget("guard-drift", {
    title: "SESSION DRIFT", uses: ["paper"],
    render: function (el, D) {
      var p = D.paper; if (!p) { el.innerHTML = C.state(D, "paper"); return; }
      var tr = (p.trades || []).slice().reverse(); if (!tr.length) { el.innerHTML = C.empty("No closed trades yet."); return; }
      var g = guard(tr);
      el.innerHTML = C.table([
        { label: "#", w: "40px", cell: function (t) { return '<span class="dim">' + (tr.indexOf(t) + 1) + "</span>"; } },
        { label: "COIN", w: "80px", cell: function (t) { return "<b>" + e(U.base(t.symbol)) + "</b>"; } },
        { label: "NOTIONAL", w: "110px", cell: function (t) { return e(U.inr(g.notional(t))); } },
        { label: "HELD", w: "70px", cell: function (t) { return e(U.hours(t.hours_held)); } },
        { label: "GROSS", w: "100px", cell: function (t) { return C.signed(t.gross, function (x) { return U.inr(x, true); }); } },
        { label: "COSTS", w: "100px", cell: function (t) { return '<span class="dn">' + e(U.inr(-((t.fees || 0) + (t.funding || 0)))) + "</span>"; } },
        { label: "KEPT", w: "80px", cell: function (t) { var k = g.kept(t); return '<b class="' + (k != null && k < 60 ? "dn" : "up") + '">' + (k == null ? "—" : k.toFixed(0) + "%") + "</b>"; } }
      ], tr, { minWidth: 600 });
    }
  });
  UI.view("book/guard", [["guard-summary"], [{ w: "guard-flags", size: "side" }, { w: "guard-drift", size: "grow" }]]);
})();
