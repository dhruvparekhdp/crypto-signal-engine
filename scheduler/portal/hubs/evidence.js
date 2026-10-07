/* Evidence hub: Strategies · Accuracy · History · Research (tools: Audit, Mirror, Pipeline, v2 shadow). */
(function () {
  "use strict";
  var U = UI.U, C = UI.C, e = U.esc;

  // ------------------------------------------------------------- strategies
  UI.widget("expected", {
    bare: true, uses: ["swing"],
    render: function (el, D) {
      var ex = D.swing && D.swing.expected_from_backtest;
      el.innerHTML = ex ? '<div class="hero-band"><b>' + e(ex.r_per_trade) + " R / trade</b><span>Expected from the 5-year backtest: win rate " + e(ex.win_rate) + ", losing streaks of " +
        e(ex.losing_streaks) + ", about " + e(ex.trades_per_day_all_coins) + " trades a day. Judge after " + e(ex.judge_after_trades) + " trades.</span></div>" : C.state(D, "swing");
    }
  });
  UI.widget("strategies", {
    title: "STRATEGIES · LIVE vs BACKTEST", uses: ["swing"],
    mount: function (el, ctx) { ctx.s.t = { sort: 1 , dir: -1 }; },
    on: { sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); } },
    render: function (el, D, ctx) {
      if (!D.swing) { el.innerHTML = C.state(D, "swing"); return; }
      var by = (D.swing.closed && D.swing.closed.by_strategy) || {}, rules = D.swing.rules || {};
      ctx.right(rules.timeframes ? rules.timeframes + " · stop " + rules.stop + " · target " + rules.target : "");
      var rows = Object.keys(by).map(function (k) { var b = by[k]; return { name: k, n: b.n, wins: b.wins, sum: b.sum_r, avg: b.n ? b.sum_r / b.n : null }; });
      var armed = String(rules.strategies || "").split(",").map(function (s) { return U.name(s.trim()); }).filter(Boolean);
      el.innerHTML = C.table([
        { label: "STRATEGY", w: "minmax(0,1.6fr)", cell: function (r) { return "<b>" + e(U.name(r.name)) + "</b>"; }, sort: function (r) { return r.name; } },
        { label: "TRADES", w: "90px", cell: function (r) { return String(r.n); }, sort: function (r) { return r.n; } },
        { label: "WIN RATE", w: "100px", cell: function (r) { return r.n ? Math.round(r.wins / r.n * 100) + "%" : "—"; }, sort: function (r) { return r.n ? r.wins / r.n : -1; } },
        { label: "AVG R", w: "100px", cell: function (r) { return C.signed(r.avg, U.rr); }, sort: function (r) { return r.avg; } },
        { label: "TOTAL R", w: "100px", cell: function (r) { return C.signed(r.sum, U.rr); }, sort: function (r) { return r.sum; } }
      ], rows, { state: ctx.s.t, empty: "No closed swing trades yet, so nothing to score." }) +
        (armed.length ? '<p class="mono tiny dim">ARMED: ' + e(armed.join(" · ")) + "</p>" : "");
    }
  });
  UI.view("evidence/strategies", [["expected"], ["strategies"]]);

  // ------------------------------------------------------------- accuracy (all detectors, archive-wide)
  UI.widget("acc-tiles", {
    bare: true, uses: ["accuracy"],
    render: function (el, D) {
      var a = D.accuracy; if (!a) { el.innerHTML = C.state(D, "accuracy"); return; }
      el.innerHTML = (!a.resolved ? C.flag("amber", "Outcomes are not resolved yet", "The resolver walks stored candles forward from each signal to see whether target or stop came first. Until it runs, the charts here stay empty rather than wrong.", (a.pending || 0) + " signals pending") : "") +
        C.tiles([
          ["SIGNALS", a.total || 0, "in the archive"], ["RESOLVED", a.resolved || 0, a.pending ? a.pending + " pending" : ""],
          ["WIN RATE", a.win_rate_pct != null ? a.win_rate_pct + "%" : "—", "of resolved"],
          ["MEDIAN MOVE", a.median_move_pct != null ? a.median_move_pct.toFixed(3) + "%" : "—", "cost floor " + (a.min_target_pct != null ? a.min_target_pct.toFixed(3) + "%" : "—")]
        ]);
    }
  });
  UI.widget("acc-calibration", {
    title: "IS CONFIDENCE HONEST?", uses: ["accuracy"],
    render: function (el, D) {
      var a = D.accuracy; if (!a) { el.innerHTML = C.state(D, "accuracy"); return; }
      var rows = (a.calibration || []).map(function (b) { return [b.bucket + "% stated", b.win_rate_pct, b.win_rate_pct + "% · n=" + b.n, b.win_rate_pct < b.bucket ? "dn" : ""]; });
      el.innerHTML = '<p class="small dim">Stated confidence against realised win rate. A bar shorter than its label means the engine is overconfident there.</p>' +
        (rows.length ? C.bars(rows, { max: 100 }) : C.empty("Needs resolved outcomes."));
    }
  });
  UI.widget("acc-moves", {
    title: "MOVE SIZE vs THE COST FLOOR", uses: ["accuracy"],
    render: function (el, D) {
      var a = D.accuracy; if (!a) { el.innerHTML = C.state(D, "accuracy"); return; }
      var min = a.min_target_pct || 0;
      el.innerHTML = (a.move_buckets || []).length ? C.hist(a.move_buckets.map(function (b) { return { label: b.upper_pct + "%", n: b.n, cls: b.upper_pct < min ? "dn" : "" }; }), { unit: "signals" }) +
        '<p class="mono tiny dim">Lilac bars sat under the ' + min.toFixed(3) + "% the engine requires: they could not have paid for the round trip.</p>" : C.empty("Needs archived signals.");
    }
  });
  UI.widget("acc-setups", {
    title: "ACCURACY BY SETUP", uses: ["accuracy"],
    render: function (el, D) {
      var a = D.accuracy; if (!a) { el.innerHTML = C.state(D, "accuracy"); return; }
      el.innerHTML = (a.by_setup || []).length ? C.bars(a.by_setup.map(function (b) { return [U.name(b.signal_type), b.win_rate_pct, b.win_rate_pct + "% · n=" + b.n, ""]; }), { max: 100 }) : C.empty("Needs resolved outcomes.");
    }
  });
  UI.view("evidence/accuracy", [["acc-tiles"], [{ w: "acc-calibration", size: "grow" }, { w: "acc-setups", size: "side" }], ["acc-moves"]]);

  // ------------------------------------------------------------- history (was Historic Data)
  UI.widget("hist-tiles", {
    bare: true, uses: ["archive"],
    render: function (el, D) {
      var d = D.archive; if (!d) { el.innerHTML = C.state(D, "archive"); return; }
      var c = d.counts || {};
      el.innerHTML = C.tiles([["ARCHIVED SIGNALS", c.signals || 0, "older than 7 days"], ["CLOSED TRADES", c.trades || 0, "across all cycles"], ["SNAPSHOTS", c.snapshots || 0, "price + indicator"], ["CYCLES", c.cycles || 0, "completed"]]);
    }
  });
  UI.widget("hist-table", {
    title: "ARCHIVE · 7 TO 365 DAYS AGO", uses: ["archive"],
    mount: function (el, ctx) { ctx.s.f = { sym: "", out: "", q: "" }; ctx.s.t = { sort: null }; el.innerHTML = '<div class="toolbar"></div><div data-slot="t"></div>'; },
    on: {
      sym: function (el, ev, t, ctx) { ctx.s.f.sym = t.value; ctx.render(); },
      out: function (el, ev, t, ctx) { ctx.s.f.out = t.value; ctx.render(); },
      q: function (el, ev, t, ctx) { ctx.s.f.q = t.value.toLowerCase(); ctx.render(); },
      sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); }
    },
    render: function (el, D, ctx) {
      var d = D.archive; if (!d) { el.querySelector('[data-slot="t"]').innerHTML = C.state(D, "archive"); return; }
      var all = d.signals || [], f = ctx.s.f;
      if (!ctx.s.tb) {
        ctx.s.tb = 1;
        var syms = Array.from(new Set(all.map(function (s) { return s.symbol; }))).sort();
        var outs = Array.from(new Set(all.map(function (s) { return s.outcome || "pending"; }))).sort();
        el.querySelector(".toolbar").innerHTML = C.select("sym", "Coin", [["", "All"]].concat(syms.map(function (s) { return [s, U.base(s)]; })), f.sym) +
          C.select("out", "Outcome", [["", "All"]].concat(outs), f.out) + C.search("q", "Search setup…", f.q);
      }
      var rows = all.filter(function (s) { return (!f.sym || s.symbol === f.sym) && (!f.out || (s.outcome || "pending") === f.out) && (!f.q || String(s.signal_type).toLowerCase().indexOf(f.q) >= 0); });
      ctx.right(rows.length + " of " + all.length);
      el.querySelector('[data-slot="t"]').innerHTML = C.table([
        { label: "DATE", w: "110px", cell: function (s) { return '<span class="dim">' + e(U.stamp(s.timestamp)) + "</span>"; }, sort: function (s) { return s.timestamp; } },
        { label: "COIN", w: "70px", cell: function (s) { return "<b>" + e(U.base(s.symbol)) + "</b>"; }, sort: function (s) { return s.symbol; } },
        { label: "SETUP", w: "minmax(0,1.3fr)", cell: function (s) { return e(U.name(s.signal_type)); } },
        { label: "SIDE", w: "70px", cell: function (s) { return C.side(s.direction); } },
        { label: "ENTRY", w: "100px", cell: function (s) { return e(U.px(s.current_price)); } },
        { label: "CONF", w: "60px", cell: function (s) { return e(s.confidence + "%"); }, sort: function (s) { return s.confidence; } },
        { label: "OUTCOME", w: "90px", cell: function (s) { var o = s.outcome || "pending"; return C.tag(o, o === "won" ? "" : o === "lost" ? "skip" : "neutral"); } },
        { label: "P&L", w: "80px", cell: function (s) { return s.outcome && s.outcome !== "pending" ? C.signed(s.pnl_pct) : '<span class="dim">—</span>'; }, sort: function (s) { return s.pnl_pct; } }
      ], rows, { state: ctx.s.t, minWidth: 780, empty: all.length ? "No signals match these filters." : "Nothing older than 7 days yet." });
    }
  });
  UI.view("evidence/history", [["hist-tiles"], ["hist-table"]]);

  // ------------------------------------------------------------- research
  UI.widget("research-report", {
    title: "RESEARCH REPORT", uses: ["research"],
    render: function (el, D, ctx) {
      var r = D.research; if (!r) { el.innerHTML = C.state(D, "research"); return; }
      ctx.right(r.generated ? "generated " + r.generated : "");
      el.innerHTML = r.report ? C.pre(r.report) : C.empty("No research report yet: the research job writes one on its schedule.");
    }
  });
  UI.widget("research-v2", {
    title: "v2 SETUPS · SHADOW", uses: ["v2"],
    render: function (el, D) {
      var v = D.v2; if (!v) { el.innerHTML = C.state(D, "v2"); return; }
      var c = v.counts || {}, by = v.by_setup || {};
      el.innerHTML = C.tiles([["PENDING", c.pending || 0], ["OPEN", c.open || 0], ["CLOSED", c.closed || 0], ["EXPIRED", c.expired || 0]]) +
        (Object.keys(by).length ? C.bars(Object.keys(by).map(function (k) {
          var s = (by[k] && by[k].stats) || {};
          return [k, s.trades || 0, (s.trades || 0) + " closed" + (s.expectancy_r != null ? " · " + U.rr(s.expectancy_r) + "/trade" : ""), s.expectancy_r < 0 ? "dn" : ""];
        })) : "") + '<p class="small"><a href="/v2">Open the full v2 shadow page ↗</a></p>';
    }
  });
  UI.widget("research-tools", {
    title: "DEEP TOOLS",
    render: function (el) {
      el.innerHTML = C.links([
        ["/audit", "Audit", "every signal, its gates and its outcome"], ["/classic#mirror", "Mirror", "mirror review and live trade check"],
        ["/pipeline", "Pipeline", "multi-year backtest monitor"], ["/v2", "v2 shadow", "next strategy set, shadow only"], ["/chart", "Chart", "candles with signal overlays"]
      ]);
    }
  });
  UI.view("evidence/research", [[{ w: "research-report", size: "grow" }, { stack: ["research-v2", "research-tools"], size: "side" }]]);
})();
