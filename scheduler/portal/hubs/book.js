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
          lockBlock(p, o) +
          (p.note ? '<div class="small dim" style="margin:6px 0">🗒 ' + e(p.note) + "</div>" : "") +
          '<div class="pos-foot"><span>in ' + e(U.px(p.entry)) + "</span><span>now " + e(U.px(o.mark)) + "</span><b>" +
          (left == null ? "—" : left >= 1 ? left.toFixed(1) + "d left" : left > 0 ? (left * 24).toFixed(1) + "h left" : "expiring") +
          "</b></div></div>";
      }).join("") + "</div>";
    }
  });
  // Swing / Intra / Delivery tabs (owner, 14 Oct 2026): every list in the paper cycle can be cut by book.
  var MODES = [["all", "All"], ["swing", "Swing"], ["intraday", "Intra"], ["delivery", "Delivery"]];
  function modeTabs(cur, rows) {
    return '<span class="chipset modetabs">' + MODES.map(function (m) {
      var n = m[0] === "all" ? rows.length : rows.filter(function (r) { return (r.trade_mode || "intraday") === m[0]; }).length;
      return '<button class="chipbtn' + (cur === m[0] ? " on" : "") + '" data-act="mode" data-v="' + m[0] + '">' +
        e(m[1]) + " · " + n + "</button>";
    }).join("") + "</span>";
  }
  function byMode(rows, cur) {
    return cur === "all" ? rows : rows.filter(function (r) { return (r.trade_mode || "intraday") === cur; });
  }

  // Profit lock (ladder) panel: what is protected now, the best the trade reached, and the next step.
  function lockBlock(p, o) {
    var k = o.lock || p.lock || {};
    if (k.state == null) return "";
    var locked = k.locked_r != null && k.locked_r > -0.999;
    var cls = k.locked_r > 0.02 ? "up" : locked ? "acc" : "dim";
    var rows = [
      ["Stop now", U.px(k.stop) + (locked ? "  (was " + U.px(k.initial_stop) + ")" : "")],
      ["Best so far", U.px(k.best_price) + " · " + U.rr(k.best_r)],
      ["If stopped now", (k.worst_case_inr <= 0 ? "+" : "−") + U.inr(Math.abs(k.worst_case_inr))],
      ["Next step", k.next_trigger_r == null ? "—" : "at " + U.px(k.next_trigger_price) + " (" + U.rr(k.next_trigger_r) +
        ") → lock " + (k.next_lock_r <= 0 ? "break-even" : U.rr(k.next_lock_r)) + " at " + U.px(k.next_lock_price)]
    ];
    return '<div class="lockbox"><div class="lockbox-head"><span class="lock-badge ' + cls + '">' +
      (locked ? "🔒 " : "") + e(String(k.state).toUpperCase()) + "</span><span class='dim tiny'>PROFIT LADDER</span></div>" +
      rows.map(function (r) { return '<div class="lockbox-row"><span>' + e(r[0]) + "</span><b>" + e(r[1]) + "</b></div>"; }).join("") +
      '<div class="lockbox-note">' + e(k.confirm || "") + "</div></div>";
  }

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
        { label: "STRATEGY", w: "minmax(0,1.4fr)", cell: function (t) { return e(U.name(t.strategy)) + (t.note ? ' <span class="tiny dim">🗒</span>' : ""); } },
        { label: "R", w: "90px", cell: function (t) { return C.signed(t.r, U.rr); }, sort: function (t) { return t.r; } },
        { label: "NET", w: "100px", cell: function (t) { return C.signed(t.net_pnl, function (x) { return U.inr(x, true); }); }, sort: function (t) { return t.net_pnl; } },
        { label: "EXIT", w: "minmax(0,1fr)", cell: function (t) { return e(t.exit_reason || ""); } },
        { label: "CLOSED", w: "100px", cell: function (t) { return '<span class="dim">' + e(U.ago(t.closed_at)) + "</span>"; }, sort: function (t) { return t.closed_at || ""; } }
      ], (cl.last || []).slice().reverse(), { state: ctx.s.t, empty: "No closed swing trades yet." }) +
        (function () {                                          // AI trade notes (plan step 7), full width
          var withNotes = (cl.last || []).filter(function (t) { return t.note; }).slice(-5).reverse();
          return withNotes.length ? '<div class="small" style="margin-top:10px"><b class="tiny dim">TRADE NOTES</b>' +
            withNotes.map(function (t) {
              return '<div style="margin-top:6px"><b>' + e(U.base(t.symbol)) + '</b> <span class="dim tiny">' + e(U.ago(t.closed_at)) +
                "</span> — " + e(t.note) + "</div>";
            }).join("") + "</div>" : "";
        })();
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
      var tiles = [
        ["TIMEFRAMES", r.timeframes || "—"], ["STOP", r.stop || "—"], ["TARGET", r.target || "—"],
        ["TIME LIMIT", r.time_limit_minutes ? Math.round(r.time_limit_minutes / 1440) + " days" : "—"],
        ["RISK / TRADE", F.riskPct(D) + "%" + (r.mirrors_live ? " fixed" : r.adaptive_risk ? " (adaptive)" : "")]
      ];
      if (r.mirrors_live) {
        tiles.push(["LEVERAGE", (r.min_leverage || 5) + "–" + (r.max_leverage || 10) + "x", "liquidation ≥ 2× the stop away"],
          ["MAX OPEN", (r.max_open || "no limit") + " (live coins)"],
          ["DAILY LOSS LIMIT", r.daily_loss_limit ? Math.round(r.daily_loss_limit * 100) + "%" : "off"],
          ["ROUND UP TO 1 CONTRACT", r.round_up_risk_pct ? "if risk ≤ " + Math.round(r.round_up_risk_pct * 100) + "%" : "off"],
          ["PAPER-ONLY COINS", r.paper_only_coins ? String(r.paper_only_coins) : "none"]);
      } else {
        tiles.push(["MAX LEVERAGE", (r.max_leverage || "—") + "x"], ["MAX OPEN", r.max_open ? r.max_open : "no limit"]);
      }
      tiles.push(["EXCLUDED", String(r.excluded_coins || "—").toUpperCase()]);
      el.innerHTML = (r.mirrors_live ? '<p class="small dim">Paper uses the same rules as the live ₹ book (Settings → Paper uses the live rules).</p>' : "") + C.tiles(tiles);
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
        ["COSTS", U.inr((s.trading_fees || 0) + (s.funding_paid || 0) + (s.open_entry_fees || 0) + (s.open_funding || 0)),
          "closed " + U.inr((s.trading_fees || 0) + (s.funding_paid || 0)) + " · open: fees " + U.inr(s.open_entry_fees || 0) +
          ", funding " + U.inr(s.open_funding || 0, true), "dn"],
        ["CYCLE", "#" + c.id, "started " + U.ago(c.started_at)]
      ]);
    }
  });
  UI.widget("paper-open", {
    title: "OPEN POSITIONS", uses: ["paper"],
    mount: function (el, ctx) { ctx.s.t = { sort: null }; ctx.s.mode = "all"; },
    on: { sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); },
          mode: function (el, ev, t, ctx) { ctx.s.mode = t.dataset.v; ctx.render(); } },
    render: function (el, D, ctx) {
      var p = D.paper; if (!p) { el.innerHTML = C.state(D, "paper"); return; }
      var allRows = p.positions || [], rows = byMode(allRows, ctx.s.mode || "all");
      ctx.right(rows.length + " of " + allRows.length + " open");
      el.innerHTML = '<div class="toolbar">' + modeTabs(ctx.s.mode || "all", allRows) + "</div>" + C.table([
        { label: "COIN", w: "80px", cell: function (r) { return "<b>" + e(U.base(r.symbol)) + "</b>"; }, sort: function (r) { return r.symbol; } },
        { label: "SIDE", w: "80px", cell: function (r) { return C.side(r.side); } },
        { label: "MODE", w: "80px", cell: function (r) { return '<span class="dim">' + e(r.trade_mode) + "</span>"; } },
        { label: "STRATEGY", w: "130px", cell: function (r) { return e(String(r.signal_type || "").replace(/^swing_/, "").replace(/_/g, " ")) + (r.confidence != null ? '<br><span class="dim tiny">confidence ' + e(Math.round(r.confidence)) + "</span>" : ""); }, sort: function (r) { return r.signal_type || ""; } },
        { label: "ENTRY", w: "100px", cell: function (r) { return e(U.px(r.entry)); } },
        { label: "MARK", w: "100px", cell: function (r) { return e(U.px(r.mark)); } },
        { label: "STOP", w: "100px", cell: function (r) { return e(U.px(r.stop)) + (r.trailing ? ' <span class="acc tiny">trail</span>' : ""); } },
        { label: "LOCK", w: "170px", cell: function (r) {
            var k = r.lock || {};
            if (k.state == null) return '<span class="dim">—</span>';
            var cls = k.locked_r >= 0.05 ? "up" : k.locked_r > -0.999 ? "acc" : "dim";
            var txt = k.locked_r >= 0.05 ? "LOCKED " + U.rr(k.locked_r) : k.locked_r > -0.05 ? "BREAK-EVEN"
                    : k.locked_r > -0.999 ? "RISK " + U.rr(k.locked_r) : "ORIGINAL STOP";
            return '<span class="lock-badge ' + cls + '" title="' + e(k.confirm || "") + '">' + (k.locked_r > -0.999 ? "🔒 " : "") + e(txt) + "</span>";
          } },
        { label: "R NOW / BEST", w: "120px", cell: function (r) { var k = r.lock || {};
            return k.r_now == null ? "—" : C.signed(k.r_now, U.rr) + ' <span class="dim">/ ' + e(U.rr(k.best_r)) + "</span>"; },
          sort: function (r) { return (r.lock || {}).r_now; } },
        { label: "NEXT LOCK", w: "150px", cell: function (r) { var k = r.lock || {};
            return k.next_trigger_r == null ? "—" : '<span class="dim">at ' + e(U.px(k.next_trigger_price)) + " → " +
              e(k.next_lock_r <= 0 ? "BE" : U.rr(k.next_lock_r)) + "</span>"; } },
        { label: "TARGET", w: "100px", cell: function (r) { return e(U.px(r.target)); } },
        { label: "MARGIN", w: "90px", cell: function (r) { return e(U.inr(r.margin)); }, sort: function (r) { return r.margin; } },
        { label: "LEVERAGE", w: "80px", cell: function (r) { return r.leverage ? "<b>" + e(r.leverage.toFixed(1)) + "x</b>" : "—"; }, sort: function (r) { return r.leverage || 0; } },
        { label: "SIZE", w: "130px", cell: function (r) { return e((r.qty != null ? r.qty : "—") + " " + U.base(r.symbol)) + '<br><span class="dim tiny">' + e(U.inr(r.notional)) + " notional</span>"; }, sort: function (r) { return r.notional || 0; } },
        { label: "LIQUIDATION", w: "120px", cell: function (r) { if (!r.liq) return "—"; var d = r.mark ? Math.abs(r.liq / r.mark - 1) * 100 : null;
            return e(U.px(r.liq)) + (d != null ? '<br><span class="' + (d < 10 ? "dn" : "dim") + ' tiny">' + d.toFixed(1) + "% away</span>" : ""); }, sort: function (r) { return r.liq && r.mark ? Math.abs(r.liq / r.mark - 1) : 9; } },
        { label: "AT STOP", w: "110px", cell: function (r) { if (r.qty == null || !r.stop || !r.entry) return "—";
            var sgn = String(r.side).toLowerCase() === "short" ? -1 : 1, v = sgn * (r.stop - r.entry) * r.qty * (r.usdt_inr || 1);
            return C.signed(v, function (x) { return U.inr(x, true); }) + '<br><span class="dim tiny">' + (v >= 0 ? "locked" : "at risk") + "</span>"; }, sort: function (r) { var sgn = String(r.side).toLowerCase() === "short" ? -1 : 1; return r.qty ? sgn * (r.stop - r.entry) * r.qty : 0; } },
        { label: "NET", w: "100px", cell: function (r) { return C.signed(r.unrealised, function (x) { return U.inr(x, true); }); }, sort: function (r) { return r.unrealised; } },
        { label: "ROE", w: "80px", cell: function (r) { return C.signed(r.roe_pct); }, sort: function (r) { return r.roe_pct; } },
        { label: "COSTS", w: "110px", cell: function (r) {
            var c = -(r.entry_fee || 0) - (r.exit_fee_est || 0) - (r.funding_so_far || 0);
            return '<span title="entry fee ' + e(U.inr(r.entry_fee || 0)) + ' · exit fee est ' + e(U.inr(r.exit_fee_est || 0)) +
              ' · funding ' + e(U.inr(-(r.funding_so_far || 0), true)) + '">' + C.signed(c, function (x) { return U.inr(x, true); }) + "</span>"; } },
        { label: "OPENED", w: "90px", cell: function (r) { return '<span class="dim">' + e(U.ago(r.opened_at)) + "</span>"; }, sort: function (r) { return r.opened_at || ""; } },
        { label: "EXPIRES", w: "90px", cell: function (r) {
            if (!r.expires_at) return "—";
            var h = (new Date(r.expires_at).getTime() - Date.now()) / 3600000;
            return '<span class="' + (h < 12 ? "acc" : "dim") + '">' + (h <= 0 ? "now" : h < 48 ? h.toFixed(1) + "h" : (h / 24).toFixed(1) + "d") + "</span>";
          }, sort: function (r) { return r.expires_at || ""; } }
      ], rows, { state: ctx.s.t, minWidth: 2100, empty: p.running ? "No open positions." : "No cycle running." });
    }
  });
  UI.widget("paper-history", {
    title: "HISTORY", uses: ["paper"],
    mount: function (el, ctx) {
      ctx.s.f = { sym: "", side: "", setup: "", exit: "", res: "", q: "", mode: "all" }; ctx.s.t = { sort: null };
      el.innerHTML = '<div class="toolbar"></div><div data-slot="t"></div><div data-slot="sum"></div>';
    },
    on: {
      sym: function (el, ev, t, ctx) { ctx.s.f.sym = t.value; ctx.render(); },
      side: function (el, ev, t, ctx) { ctx.s.f.side = t.value; ctx.render(); },
      setup: function (el, ev, t, ctx) { ctx.s.f.setup = t.value; ctx.render(); },
      exit: function (el, ev, t, ctx) { ctx.s.f.exit = t.value; ctx.render(); },
      res: function (el, ev, t, ctx) { ctx.s.f.res = ctx.s.f.res === t.dataset.v ? "" : t.dataset.v; ctx.s.tb = null; ctx.render(); },
      q: function (el, ev, t, ctx) { ctx.s.f.q = t.value.toLowerCase(); ctx.render(); },
      reset: function (el, ev, t, ctx) { ctx.s.f = { sym: "", side: "", setup: "", exit: "", res: "", q: "", mode: "all" }; ctx.s.tb = null; ctx.render(); },
      mode: function (el, ev, t, ctx) { ctx.s.f.mode = t.dataset.v; ctx.s.tb = null; ctx.render(); },
      sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); }
    },
    render: function (el, D, ctx) {
      var p = D.paper; if (!p) { el.querySelector('[data-slot="t"]').innerHTML = C.state(D, "paper"); return; }
      var all = p.trades || [], f = ctx.s.f;
      function uniq(k) { return Array.from(new Set(all.map(function (t) { return t[k]; }).filter(Boolean))).sort(); }
      var key = [all.length, f.res, f.mode].join();
      if (ctx.s.tb !== key) {
        ctx.s.tb = key;
        el.querySelector(".toolbar").innerHTML = modeTabs(f.mode || "all", all) +
          C.select("sym", "Coin", [["", "All"]].concat(uniq("symbol").map(function (s) { return [s, U.base(s)]; })), f.sym) +
          C.select("side", "Side", [["", "Both"], ["long", "Long"], ["short", "Short"]], f.side) +
          C.select("setup", "Setup", [["", "All"]].concat(uniq("signal_type").map(function (s) { return [s, U.name(s)]; })), f.setup) +
          C.select("exit", "Exit", [["", "All"]].concat(uniq("reason")), f.exit) +
          '<span class="chipset">' + '<button class="chipbtn' + (f.res === "win" ? " on" : "") + '" data-act="res" data-v="win">Wins</button>' +
          '<button class="chipbtn' + (f.res === "loss" ? " on" : "") + '" data-act="res" data-v="loss">Losses</button></span>' +
          C.search("q", "Search…", f.q) + '<button class="mini" data-act="reset">Reset</button>';
      }
      var rows = byMode(all, f.mode || "all").filter(function (t) {
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
  // Delta demo (testnet) positions with the real-market gap (owner, 15 Oct 2026)
  UI.widget("paper-demo", {
    title: "DELTA DEMO · FAKE MONEY", uses: ["delta_demo", "paper"],
    render: function (el, D) {
      var d = D.delta_demo; if (!d) { el.innerHTML = C.state(D, "delta_demo"); return; }
      var rows = d.positions || [];
      if (!rows.length) { el.innerHTML = C.empty(d.mode === "testnet" ? "No open demo positions." : "Demo mode is off (mode: " + e(d.mode) + ")."); return; }
      var paper = {}; ((D.paper && D.paper.positions) || []).forEach(function (p) { paper[String(p.symbol).toUpperCase()] = p; });
      el.innerHTML = '<p class="mono small dim" style="margin:0 0 10px">The demo exchange has its own price, which drifts away from the real market. ' +
        "Demo profit and loss shows that orders, stops and targets work, not how the strategy is doing; the paper column is the real-price result.</p>" +
        C.table([
          { label: "COIN", w: "80px", cell: function (r) { return "<b>" + e(r.symbol.replace(/USD$/, "")) + "</b>"; } },
          { label: "SIDE", w: "80px", cell: function (r) { return C.side(r.side); } },
          { label: "CONTRACTS", w: "90px", cell: function (r) { return e(r.contracts); } },
          { label: "DEMO ENTRY", w: "110px", cell: function (r) { return e(U.px(r.entry)); } },
          { label: "DEMO PRICE", w: "110px", cell: function (r) { return e(U.px(r.demo_mark)); } },
          { label: "REAL PRICE", w: "110px", cell: function (r) { return "<b>" + e(U.px(r.real_mark)) + "</b>"; } },
          { label: "GAP", w: "80px", cell: function (r) { var g = r.gap_pct; return g == null ? "—" : '<span class="' + (Math.abs(g) >= 1 ? "dn" : "dim") + '">' + (g > 0 ? "+" : "") + g.toFixed(2) + "%</span>"; } },
          { label: "DEMO P&L", w: "100px", cell: function (r) { return r.demo_pnl_usd == null ? "—" : C.signed(r.demo_pnl_usd, function (x) { return (x >= 0 ? "+$" : "−$") + Math.abs(x).toFixed(2); }); } },
          { label: "PAPER (REAL PRICE)", w: "130px", cell: function (r) { var p = paper[String(r.binance_symbol || "").toUpperCase()];
              return p ? C.signed(p.unrealised, function (x) { return U.inr(x, true); }) : '<span class="dim">closed in paper</span>'; } },
          { label: "EXCHANGE STOP", w: "120px", cell: function (r) { return e(U.px(r.exchange_stop)); } }
        ], rows, { minWidth: 1100 });
    }
  });
  UI.view("book/paper", [["paper-strip"], ["paper-open"], ["paper-demo"], ["paper-history"]]);

  // ------------------------------------------------------------- live, real money (owner, 18 Oct 2026)
  // paise, like the Delta app: a ₹500 wallet rounded to ₹500 hid the ₹0.21 that was really gone (19 Oct 2026)
  function rs(x, signed) {
    if (!U.num(x)) return "—";
    return (x < 0 ? "−" : signed ? "+" : "") + "₹" + Math.abs(x).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  UI.widget("live-strip", {
    title: "LIVE · REAL MONEY · DELTA INDIA", uses: ["delta_live"],
    render: function (el, D, ctx) {
      var d = D.delta_live; if (!d) { el.innerHTML = C.state(D, "delta_live"); return; }
      ctx.right("updated " + U.hms(new Date(d.generated_ms)) + " UTC · refreshes every 5 s");
      var w = d.wallet || {}, day = d.day || {};
      var state = d.kill_switch ? ["red", "KILL SWITCH ON: no new orders", "Open positions keep their exchange stops."] :
        d.live ? ["ok", "LIVE: real orders are ON", "Swing signals are copied to Delta India with real money. Risk " + Math.round(d.risk_pct * 1000) / 10 + "% per trade, max " + d.max_open + " open, leverage ≥ " + d.min_leverage + "x."] :
        ["amber", "Not live: no real orders (mode " + String(d.mode).toUpperCase() + (d.live_orders ? "" : ", live orders OFF") + ")", "Turn on 'Delta book mode = live' and 'Allow live Delta orders' on Settings to start."];
      var dayLeft = day.limit_inr != null && day.pnl_inr != null ? day.limit_inr + Math.min(0, day.pnl_inr) : null;
      el.innerHTML = C.flag(state[0], state[1], state[2], d.error || "") +
        // the same numbers as the Delta app (19 Oct 2026): balance, P&L at the mark price, available after open losses
        C.tiles([["WALLET ON DELTA", rs(w.total_inr), "balance, before open trades"],
                 ["OPEN P&L", rs(w.upnl_inr, true), "equity " + rs(w.equity_inr), w.upnl_inr < 0 ? "dn" : w.upnl_inr > 0 ? "up" : ""],
                 ["AVAILABLE MARGIN", rs(w.free_inr), "free for a new trade, after open losses"],
                 ["TODAY · CLOSED TRADES", rs(day.pnl_inr, true), day.start_inr != null ? "from " + rs(day.start_inr) + " at 05:30 IST" : "day starts at first check", day.pnl_inr < 0 ? "dn" : ""],
                 ["DAILY LIMIT LEFT", dayLeft != null ? rs(dayLeft) : "—", "stops new orders at −" + Math.round(d.daily_loss_pct * 100) + "%"],
                 ["FLOOR", rs(d.floor_inr), "no new orders below"],
                 ["OPEN", (d.positions || []).length + " / " + d.max_open, ""]]);
    }
  });
  UI.widget("live-positions", {
    title: "LIVE POSITIONS · ON DELTA NOW", uses: ["delta_live"],
    render: function (el, D) {
      var d = D.delta_live; if (!d) { el.innerHTML = C.state(D, "delta_live"); return; }
      var rows = d.positions || [];
      el.innerHTML = C.table([
        { label: "COIN", w: "80px", cell: function (r) { return "<b>" + e(String(r.symbol).replace(/USD$/, "")) + "</b>" + (r.flags && r.flags.length ? '<br><span class="dn tiny">' + e(r.flags.join(" · ")) + "</span>" : ""); } },
        { label: "SIDE", w: "76px", cell: function (r) { return C.side(r.side); } },
        { label: "CONTRACTS", w: "86px", cell: function (r) { return e(r.contracts) + (r.leverage ? '<br><span class="dim tiny">' + e(r.leverage) + "x</span>" : ""); } },
        { label: "ENTRY", w: "100px", cell: function (r) { return e(U.px(r.entry)); } },
        { label: "MARK PRICE", w: "100px", cell: function (r) { return "<b>" + e(U.px(r.mark)) + "</b>"; } },
        { label: "P&L @ MARK", w: "110px", cell: function (r) { return r.upnl_inr == null ? "—" : C.signed(r.upnl_inr, function (x) { return rs(x, true); }) + (r.roe_pct != null ? '<br><span class="dim tiny">' + (r.roe_pct > 0 ? "+" : "") + r.roe_pct.toFixed(2) + "% of margin</span>" : ""); } },
        { label: "R NOW", w: "70px", cell: function (r) { return r.r_now == null ? "—" : C.signed(r.r_now, function (x) { return (x > 0 ? "+" : "") + x.toFixed(2) + "R"; }); } },
        { label: "STOP ON DELTA", w: "110px", cell: function (r) { return r.exchange_stop ? e(U.px(r.exchange_stop)) + (r.engine_stop && Math.abs(r.engine_stop - r.exchange_stop) > (r.tick || 1e-9) / 2 ? '<br><span class="dim tiny">engine lock ' + e(U.px(r.engine_stop)) + "</span>" : "") : '<b class="dn">NONE</b>'; } },
        { label: "TARGET", w: "100px", cell: function (r) { return e(U.px(r.exchange_target)); } },
        { label: "LIQUIDATION", w: "110px", cell: function (r) { return r.liquidation ? e(U.px(r.liquidation)) + (r.mark ? '<br><span class="dim tiny">' + (Math.abs(r.liquidation / r.mark - 1) * 100).toFixed(1) + "% away</span>" : "") : "—"; } },
        { label: "MARGIN", w: "80px", cell: function (r) { return e(rs(r.margin_inr)); } }
      ], rows, { minWidth: 1100, empty: d.live ? "No open live positions. The next swing signal that fits ₹" + Math.round((d.wallet || {}).total_inr || 0) + " will appear here." : "No live positions." });
    }
  });
  UI.widget("live-orders", {
    title: "ORDERS WAITING ON DELTA", uses: ["delta_live"],
    render: function (el, D) {
      var d = D.delta_live; if (!d) { el.innerHTML = C.state(D, "delta_live"); return; }
      el.innerHTML = C.table([
        { label: "COIN", w: "80px", cell: function (o) { return "<b>" + e(String(o.symbol || "").replace(/USD$/, "")) + "</b>"; } },
        { label: "TYPE", w: "minmax(0,1fr)", cell: function (o) { return e(String(o.type || "").replace(/_order$/, "").replace(/_/g, " ")); } },
        { label: "SIDE", w: "70px", cell: function (o) { return e(o.side); } },
        { label: "SIZE", w: "70px", cell: function (o) { return e(o.size); } },
        { label: "TRIGGER", w: "100px", cell: function (o) { return e(U.px(o.stop_price || o.limit_price)); } },
        { label: "REDUCE-ONLY", w: "100px", cell: function (o) { return o.reduce_only ? "yes" : '<b class="dn">NO</b>'; } }
      ], d.orders || [], { minWidth: 560, empty: "No orders waiting." });
    }
  });
  UI.widget("live-journal", {
    title: "LIVE ORDER LOG", uses: ["delta_live"],
    render: function (el, D) {
      var d = D.delta_live; if (!d) { el.innerHTML = C.state(D, "delta_live"); return; }
      el.innerHTML = C.table([
        { label: "TIME (UTC)", w: "96px", cell: function (r) { return '<span class="dim">' + (r.t ? U.hms(new Date(r.t)) : "—") + "</span>"; } },
        { label: "EVENT", w: "minmax(0,1fr)", cell: function (r) { return "<b>" + e(r.event || (r.side ? "open " + (r.side === "sell" ? "short" : "long") : "signal")) + "</b>"; } },
        { label: "CONTRACT", w: "90px", cell: function (r) { return e(r.delta_symbol || "—"); } },
        { label: "RESULT", w: "minmax(0,1.6fr)", cell: function (r) { return r.error ? '<span class="dn small">' + e(String(r.error).slice(0, 90)) + "</span>" : '<span class="small">' + e((r.placed ? "placed" : (r.reason || "not placed")) + (r.size ? " · " + r.size + " ct" : "") + (r.leverage ? " · " + r.leverage + "x" : "")) + "</span>"; } }
      ], (d.journal || []).slice().reverse(), { minWidth: 520, empty: "No live orders yet." });
    }
  });
  // backtest = live check (19 Oct 2026): analysis/parity.py, run daily at 00:40 UTC and on demand
  UI.widget("live-parity", {
    title: "BACKTEST = LIVE CHECK", uses: ["parity"],
    on: { run: function (el, ev, t) { t.disabled = true; t.textContent = "Running…"; UI.Net.get("/api/parity?run=1").then(function () { UI.Store.fetch("parity"); }); } },
    render: function (el, D, ctx) {
      var d = D.parity; if (!d) { el.innerHTML = C.state(D, "parity"); return; }
      var rep = d.report, btn = '<button class="mini" data-act="run"' + (d.running ? " disabled>Running…" : ">Run now") + "</button>";
      if (!rep) { el.innerHTML = '<div class="toolbar">' + btn + "</div>" + C.empty("No check has run yet. It runs every day at 00:40 UTC."); return; }
      var sm = rep.summary || {};
      ctx.right("last run " + U.stamp(rep.ran_at) + " · last " + rep.days + " days");
      var why = '<p class="mono small dim" style="margin:0 0 10px">Every 4h / 8h bar: what live logged vs the same computation re-run on the same Delta bars ' +
        "(a mismatch is a fault), vs a longer warm-up, and vs the backtest's Binance bars. Every live position vs its paper copy; size is compared as risk in % of each book's own wallet.</p>";
      var tiles = C.tiles([
        ["RESULT", sm.ok ? "ALL MATCH" : "CHECK", sm.ok ? "live did what the backtest code says" : "see the list below", sm.ok ? "up" : "dn"],
        ["BARS COMPARED", String(sm.signals_compared), (sm.live_fired || 0) + " live signals, " + (sm.replay_fired || 0) + " in the replay"],
        ["SIGNAL MISMATCHES", String(sm.signal_faults), (sm.expected_skips || 0) + " skipped on purpose", sm.signal_faults ? "dn" : "up"],
        ["WARM-UP GAPS", String(sm.warmup_diffs), "500 bars vs full history", sm.warmup_diffs ? "dn" : "up"],
        ["SAME AS BACKTEST", sm.venue_agreement_pct == null ? "—" : sm.venue_agreement_pct + "%", "Delta vs Binance bars"],
        ["LIVE POSITIONS", String(sm.positions_checked), (sm.execution_problems || 0) + " problems", sm.execution_problems ? "dn" : "up"]]);
      var faults = (rep.signal_faults || []).length ? C.table([
        { label: "COIN", w: "70px", cell: function (f) { return "<b>" + e(String(f.symbol).replace(/USDT$/, "")) + "</b>"; } },
        { label: "TF", w: "50px", cell: function (f) { return e(f.tf); } },
        { label: "BAR CLOSE", w: "110px", cell: function (f) { return e(f.bar_close); } },
        { label: "LIVE LOGGED", w: "minmax(0,1fr)", cell: function (f) { return e(f.live); } },
        { label: "REPLAY SAYS", w: "minmax(0,1fr)", cell: function (f) { return e(f.replay); } }
      ], rep.signal_faults, { minWidth: 560 }) : "";
      var ex = (rep.execution || []).length ? C.table([
        { label: "COIN", w: "80px", cell: function (r) { return "<b>" + e(String(r.symbol).replace(/USD$/, "")) + "</b> " + C.side(r.side); } },
        { label: "ENTRY VS PAPER", w: "110px", cell: function (r) { return r.entry_gap_bps == null ? "—" : e((r.entry_gap_bps > 0 ? "+" : "") + r.entry_gap_bps + " bps"); } },
        { label: "STOP / TARGET", w: "120px", cell: function (r) { return e("stop " + (r["initial stop gap R"] == null ? "—" : r["initial stop gap R"] + "R") + " · tgt " + (r["target gap R"] == null ? "—" : r["target gap R"] + "R")); } },
        { label: "SIZE · RISK OF WALLET", w: "170px", cell: function (r) { var c = r.contracts || {}, k = r.risk_pct || {};
            return e((c.live != null ? c.live + " ct" : "—") + (k.live != null ? " (" + k.live + "%)" : "") + " live") + '<br><span class="dim tiny">' +
              e((c.paper != null ? c.paper + " ct" : "—") + (k.paper != null ? " (" + k.paper + "%)" : "") + " paper, own wallet") + "</span>"; } },
        { label: "R NOW", w: "120px", cell: function (r) { return r.r_now ? e(r.r_now.live.toFixed(2) + " live / " + r.r_now.paper.toFixed(2) + " paper") : "—"; } },
        { label: "PROBLEMS", w: "minmax(0,1.4fr)", cell: function (r) { return r.problems.length ? '<span class="dn small">' + e(r.problems.join(" · ")) + "</span>" : '<span class="up small">none</span>'; } }
      ], rep.execution, { minWidth: 820 }) : C.empty("No live positions to compare.");
      el.innerHTML = '<div class="toolbar">' + btn + "</div>" + why + tiles + (faults ? '<h4 class="mono small" style="margin:14px 0 6px">SIGNAL MISMATCHES</h4>' + faults : "") +
        '<h4 class="mono small" style="margin:14px 0 6px">LIVE POSITIONS VS PAPER</h4>' + ex;
    }
  });
  UI.view("book/live", [["live-strip"], ["live-positions"], [{ w: "live-orders", size: "grow" }], ["live-parity"], ["live-journal"]]);

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
