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
        ["/audit", "Audit", "every signal, its gates and its outcome"], ["/evidence/mirror", "Mirror", "mirror review and live trade check"],
        ["/pipeline", "Pipeline", "multi-year backtest monitor"], ["/v2", "v2 shadow", "next strategy set, shadow only"], ["/chart", "Chart", "candles with signal overlays"]
      ]);
    }
  });
  UI.view("evidence/research", [[{ w: "research-report", size: "grow" }, { stack: ["research-v2", "research-tools"], size: "side" }]]);

  UI.widget("news-study", {
    title: "NEWS EVENT STUDY",
    uses: ["news"],
    mount: function (el) { el.innerHTML = '<div class="panel-body" data-r="body"></div>'; },
    render: function (el, D) {
      var n = D.news || {}, box = el.querySelector("[data-r=body]");
      if (!n.enabled) { box.innerHTML = C.empty("Enable news engine + portal widgets. Mac backfill: scripts/news_backfill.py"); return; }
      box.innerHTML = "<p class=\"dim\">Event-study runs log to <code>audit/trial_ledger.jsonl</code> (family <code>news_study</code>). "
        + "Run <code>scripts/news_event_study.py</code> on your Mac — not on the server.</p>"
        + "<p>Events loaded: <b>" + ((n.events || []).length) + "</b></p>";
    }
  });
  UI.view("evidence/news-study", [["news-study"]]);

  // ------------------------------------------------------------- mirror (was /classic#mirror)
  // Every intraday signal is logged with its mirror (same setup, opposite side) as a control. This page shows how
  // the mirrors of the last 48 h did. Every number comes from /api/crypto/signals?mirror=1 (scorecard, trade check,
  // distances, AI read); the page only filters and renders.
  var MIRROR_STATUS = [["ALL", "All"], ["WORKED", "Worked (full + partial)"], ["won", "Full target"], ["partial", "Partial"], ["running", "Running"], ["stopped", "Stopped"]];
  function mirrorCount(sum, k) {
    if (!sum) return 0;
    return { ALL: sum.total_tested, WORKED: sum.worked_count, won: sum.full_win_count, partial: sum.partial_win_count,
      running: sum.running_count, stopped: sum.stopped_count }[k] || 0;
  }
  function mirrorBadge(s) {
    var tc = s.trade_check || {}, st = tc.status || s.outcome || "pending";
    if (st === "won") return ["FULL TARGET " + U.pct(tc.target_dist_pct || s.target_move_pct), ""];
    if (st === "partial") return ["PARTIAL · " + Math.round(tc.target_pct_reached || 0) + "% OF TARGET", "neutral"];
    if (st === "running") return ["RUNNING " + U.pct(tc.current_pnl_pct), "neutral"];
    if (st === "stopped" || st === "lost") return ["STOPPED −" + Number(tc.risk_dist_pct || s.stop_risk_pct || 0).toFixed(2) + "%", "skip"];
    return [String(st).toUpperCase(), "neutral"];
  }
  function mirrorCard(s) {
    var tc = s.trade_check || {}, ai = s.ai_reasoning || {}, b = mirrorBadge(s), reached = Math.max(0, Math.min(100, tc.target_pct_reached || 0));
    var src = !ai.source_model || ai.source_model === "pending" ? "not explained yet" : ai.source_model === "algorithmic_synthesis" ? "template text (no AI was available)" : ai.source_model;
    return '<article class="pos mirror-card"><div class="pos-top"><b>' + e(U.base(s.symbol)) + "</b>" + C.side(s.direction) + C.tag(b[0], b[1]) + "</div>" +
      '<div class="mono small dim">' + e(U.name(s.signal_type)) + " · " + e(s.confidence) + "% confidence · " + e(s.timeframe || "") + " · " + e(U.ago(s.timestamp)) + "</div>" +
      C.tiles([["STOP", U.px(s.stop_loss), s.stop_risk_pct != null ? "−" + s.stop_risk_pct.toFixed(2) + "%" : ""], ["ENTRY", U.px(s.current_price), "price at the signal"],
        ["TARGET", U.px(s.target_price), s.target_move_pct != null ? "+" + s.target_move_pct.toFixed(2) + "%" : ""],
        ["PEAK GAIN", U.pct(tc.peak_gain_pct), tc.peak_r != null ? "+" + tc.peak_r + "R at best" : "", "up"]]) +
      '<div class="mono tiny dim">HOW FAR TOWARDS THE TARGET · peak ' + e(U.px(tc.peak_price)) + "</div>" + '<div class="meter" role="img" aria-label="' + reached + '% of the way to the target"><i style="width:' + reached + '%"></i></div>' +
      '<div class="pos-scale"><span>entry</span><span>half way</span><span>target</span></div>' +
      (tc.worked_desc ? '<p class="small">' + e(tc.worked_desc) + "</p>" : "") +
      '<div class="mirror-ai"><div><b class="up">Why it worked</b><p class="small">' + e(ai.why_it_worked || "Not explained yet: only finished trades are explained, a few at a time.") + "</p></div>" +
      '<div><b class="dn">Why it failed or gave back</b><p class="small">' + e(ai.why_it_failed || "—") + "</p></div></div>" +
      '<p class="mono tiny dim">Takeaway: ' + e(ai.key_takeaway || "—") + " · read by " + e(src) + "</p></article>";
  }
  UI.widget("mirror-score", {
    bare: true, uses: ["mirror"],
    render: function (el, D) {
      var m = D.mirror; if (!m || !m.summary) { el.innerHTML = C.state(D, "mirror"); return; }
      var x = m.summary;
      el.innerHTML = C.tiles([
        ["TESTED", x.total_tested, "mirrors in the last 48 h"], ["WORKED", x.worked_rate_pct + "%", x.worked_count + " full or partial", x.worked_rate_pct >= 50 ? "up" : ""],
        ["FULL TARGET", x.full_win_count, "hit the whole target", "up"], ["PARTIAL", x.partial_win_count, "40%+ of the way"],
        ["STOPPED", x.stopped_count, "hit the stop first", "dn"], ["RUNNING", x.running_count, "still open"],
        ["AVG PEAK GAIN", U.pct(x.avg_peak_gain_pct), "best move, on average"], ["AVG OF TARGET", (x.avg_target_reached_pct || 0) + "%", "reached, on average"]]);
    }
  });
  UI.widget("mirror-list", {
    title: "MIRROR SIGNALS · LAST 48 H", uses: ["mirror"],
    mount: function (el, ctx) {
      ctx.s.f = { st: "ALL", sym: "" }; ctx.s.page = 0;
      el.innerHTML = '<p class="small dim">A mirror is the same setup taken the other way, logged as a control for every intraday signal. If mirrors do as well as the picks, the picks have no edge.</p>' +
        '<div class="toolbar" data-slot="tb"></div><div data-slot="cards"></div><div class="toolbar" data-slot="pg"></div>';
    },
    on: {
      st: function (el, ev, t, ctx) { ctx.s.f.st = t.dataset.v; ctx.s.page = 0; ctx.render(); },
      sym: function (el, ev, t, ctx) { ctx.s.f.sym = t.value; ctx.s.page = 0; ctx.render(); },
      pg: function (el, ev, t, ctx) { ctx.s.page += +t.dataset.d; ctx.render(); },
      check: function (el, ev, t) {
        t.disabled = true; t.textContent = "Checking live trades…";
        UI.Net.post("/api/crypto/signals/live-check?mirror=1", {})
          .then(function () { return UI.Store.fetch("mirror"); })
          .catch(function (err) { window.alert("Live check failed: " + err.message); })
          .then(function () { t.disabled = false; t.textContent = "Re-check live trades now"; });
      }
    },
    render: function (el, D, ctx) {
      var m = D.mirror, slot = el.querySelector('[data-slot="cards"]');
      if (!m || !Array.isArray(m.signals)) { slot.innerHTML = C.state(D, "mirror"); return; }
      var f = ctx.s.f, sum = m.summary || {}, syms = Object.keys(sum.by_symbol || {});
      el.querySelector('[data-slot="tb"]').innerHTML =
        '<span class="chipset" role="group" aria-label="Result">' + MIRROR_STATUS.map(function (x) {
          return '<button class="chipbtn' + (f.st === x[0] ? " on" : "") + '" data-act="st" data-v="' + x[0] + '" aria-pressed="' + (f.st === x[0]) + '">' + e(x[1]) + " " + mirrorCount(sum, x[0]) + "</button>";
        }).join("") + "</span>" +
        C.select("sym", "Coin", [["", "All coins"]].concat(syms.map(function (s) { return [s, U.base(s) + " (" + sum.by_symbol[s] + ")"]; })), f.sym) +
        '<button class="mini" data-act="check">Re-check live trades now</button>';
      var rows = m.signals.filter(function (s) {
        var tc = s.trade_check || {};
        var ok = f.st === "ALL" || (f.st === "WORKED" ? !!tc.worked : tc.status === f.st);
        return ok && (!f.sym || s.symbol === f.sym);
      });
      var per = 10, pages = Math.max(1, Math.ceil(rows.length / per));
      ctx.s.page = Math.max(0, Math.min(ctx.s.page, pages - 1));
      ctx.right(rows.length + " of " + m.signals.length);
      slot.innerHTML = rows.length ? '<div class="cards">' + rows.slice(ctx.s.page * per, ctx.s.page * per + per).map(mirrorCard).join("") + "</div>"
        : C.empty("No mirror signals match these filters in the last 48 hours.");
      el.querySelector('[data-slot="pg"]').innerHTML = pages > 1 ? '<button class="mini" data-act="pg" data-d="-1"' + (ctx.s.page ? "" : " disabled") + '>‹ Prev</button><span class="mono small">Page ' +
        (ctx.s.page + 1) + " of " + pages + '</span><button class="mini" data-act="pg" data-d="1"' + (ctx.s.page < pages - 1 ? "" : " disabled") + ">Next ›</button>" : "";
    }
  });
  UI.view("evidence/mirror", [["mirror-score"], ["mirror-list"]]);
})();
