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
  // channel-style bracket, paper research only (owner, 21 Oct 2026): does a +1% first target / -5% stop make money on our signals?
  UI.widget("paper-bracket", {
    title: "CHANNEL-STYLE BRACKET · PAPER RESEARCH", uses: ["bracket"],
    render: function (el, D, ctx) {
      var d = D.bracket; if (!d) { el.innerHTML = C.state(D, "bracket"); return; }
      var S = d.summary || {}, A = S.all, pl = d.plan || {};
      ctx.right((d.started ? "since " + U.stamp(d.started) + " · " : "") + d.open + " open");
      var intro = '<p class="mono small dim" style="margin:0 0 10px">Every signal gets a virtual trade with a Telegram channel\'s bracket: stop −' + pl.stop_pct + "%, targets " +
        (pl.targets_pct || []).map(function (t) { return "+" + t + "%"; }).join(" / ") + " (a quarter at each), stop to entry after the first, closed after " + pl.max_hold_hours +
        " h. No orders, no real wallet. Fees " + pl.cost_pct + "% round trip. Our pick vs its mirror (same setup, other side) shows whether the entries add anything to the bracket's built-in ~81% first-target rate. " +
        "S/R setups: every hour, every watchlist coin is checked for a retest or rejection at a support / resistance level; each one is traded with its mirror and labelled with news, AI briefing, funding, Fear &amp; Greed and trend, so the labels can be compared afterwards.</p>";
      if (!A) { el.innerHTML = intro + C.empty("No closed bracket trades yet: the first ones close as targets, stops or the 48 h limit are reached."); return; }
      var tiles = C.tiles([
        ["CLOSED", String(A.n), d.open + " still open"],
        ["FIRST TARGET HIT", A.tp1_pct + "%", "random entries: ~81%"],
        ["ENDED IN PROFIT", A.profit_pct + "%", A.full_stops + " full −5% stops"],
        ["AVG AFTER FEES", (A.avg_net_pct > 0 ? "+" : "") + A.avg_net_pct + "%", "of price per trade", A.avg_net_pct >= 0 ? "up" : "dn"],
        ["AVG ROI AT " + pl.roi_leverage + "×", (A.avg_roi_5x > 0 ? "+" : "") + A.avg_roi_5x + "%", "on margin, per trade", A.avg_roi_5x >= 0 ? "up" : "dn"]]);
      var M = d.marked || {};
      var groups = [["S/R setups: picks", "source_role:levels|primary"], ["S/R setups: mirrors (control)", "source_role:levels|mirror"],
        ["Intraday: our picks", "source_role:intraday|primary"], ["Intraday: counter-trend shorts (held back from paper)", "source_role:intraday|counter_trend"],
        ["Intraday: mirrors (control)", "source_role:intraday|mirror"], ["Swing signals", "source:swing"], ["All longs", "side:long"], ["All shorts", "side:short"]];
      // wallet limit, no risk rules (owner, 21 Oct 2026): fixed margin per trade at fixed leverage, skipped only when the wallet has no free margin
      var W = d.wallets || {}, wl = [["S/R PICKS", "levels"], ["INTRADAY PICKS", "intraday"], ["SWING", "swing"]].filter(function (x) { return W[x[1]]; });
      var wtiles = wl.length ? '<h4 class="mono small" style="margin:14px 0 6px">PAPER WALLET · ₹' + Math.round(W[wl[0][1]].start).toLocaleString("en-IN") + " START · ₹" + W[wl[0][1]].margin + " MARGIN PER TRADE AT " + W[wl[0][1]].leverage + "× · NO RISK RULES</h4>" +
        C.tiles(wl.map(function (x) { var w = W[x[1]]; return [x[0], rs(w.wallet), w.taken + " taken · " + w.won + " won · " + w.lost + " lost · " + w.open + " open" + (w.skipped_no_margin ? " · " + w.skipped_no_margin + " skipped (no margin)" : "") + (w.skipped_same_coin ? " · " + w.skipped_same_coin + " skipped (coin already open)" : "") + " · worst drop " + w.max_dd_pct + "%", w.wallet >= w.start ? "up" : "dn"]; })) : "";
      // each pick next to its own mirror: a market-wide move lifts every short (or long) together, this comparison it cannot fake
      var P = d.pairs || {}, pl2 = [["S/R setups", "levels|primary"], ["Intraday picks", "intraday|primary"], ["Counter-trend shorts", "intraday|counter_trend"]].filter(function (x) { return P[x[1]]; });
      var ptable = pl2.length ? '<h4 class="mono small" style="margin:14px 0 6px">EACH PICK VS ITS OWN MIRROR · INCL. OPEN TRADES AT THE CURRENT PRICE</h4>' + C.table([
        { label: "PICKS", w: "minmax(0,1fr)", cell: function (x) { return "<b>" + e(x[0]) + "</b>"; } },
        { label: "PAIRS", w: "70px", cell: function (x) { return e(P[x[1]].n); } },
        { label: "PICK AVG", w: "100px", cell: function (x) { return C.signed(P[x[1]].pick_avg, function (y) { return (y > 0 ? "+" : "") + y.toFixed(2) + "%"; }); } },
        { label: "ITS MIRROR AVG", w: "120px", cell: function (x) { return C.signed(P[x[1]].mirror_avg, function (y) { return (y > 0 ? "+" : "") + y.toFixed(2) + "%"; }); } },
        { label: "PICK DID BETTER", w: "130px", cell: function (x) { return e(P[x[1]].pick_better + " of " + P[x[1]].n); } }
      ], pl2, { minWidth: 560 }) : "";
      var coins = Object.keys(S).filter(function (k) { return k.indexOf("symbol:") === 0; }).sort(function (a, b) { return S[b].n - S[a].n; });
      var rows = groups.concat(coins.map(function (k) { return [k.replace("symbol:", "").replace("USDT", ""), k]; })).filter(function (g) { return S[g[1]] || M[g[1]]; });
      function cs(g, k) { return S[g[1]] ? S[g[1]][k] : null; }
      var table = C.table([
        { label: "GROUP", w: "minmax(0,1.4fr)", cell: function (g) { return "<b>" + e(g[0]) + "</b>"; } },
        { label: "CLOSED", w: "70px", cell: function (g) { return e(cs(g, "n") || 0); } },
        { label: "1ST TARGET", w: "90px", cell: function (g) { return cs(g, "n") ? e(cs(g, "tp1_pct") + "%") : "—"; } },
        { label: "IN PROFIT", w: "90px", cell: function (g) { return cs(g, "n") ? e(cs(g, "profit_pct") + "%") : "—"; } },
        { label: "FULL STOPS", w: "90px", cell: function (g) { return e(cs(g, "full_stops") || 0); } },
        { label: "AVG AFTER FEES", w: "120px", cell: function (g) { return cs(g, "n") ? C.signed(cs(g, "avg_net_pct"), function (x) { return (x > 0 ? "+" : "") + x.toFixed(3) + "%"; }) : "—"; } },
        { label: "ROI AT 5×", w: "100px", cell: function (g) { return cs(g, "n") ? C.signed(cs(g, "avg_roi_5x"), function (x) { return (x > 0 ? "+" : "") + x.toFixed(2) + "%"; }) : "—"; } },
        { label: "INCL. OPEN NOW", w: "130px", cell: function (g) { var m = M[g[1]]; return m ? C.signed(m.avg_net_pct, function (x) { return (x > 0 ? "+" : "") + x.toFixed(2) + "%"; }) + ' <span class="dim tiny">n ' + m.n + "</span>" : "—"; } }
      ], rows, { minWidth: 810 });
      var recent = C.table([
        { label: "CLOSED", w: "110px", cell: function (r) { return '<span class="dim">' + e(U.stamp(r.closed_ts)) + "</span>"; } },
        { label: "COIN", w: "70px", cell: function (r) { return "<b>" + e(String(r.symbol).replace("USDT", "")) + "</b>"; } },
        { label: "SIDE", w: "76px", cell: function (r) { return C.side(r.side > 0 ? "long" : "short"); } },
        { label: "KIND", w: "130px", cell: function (r) { return '<span class="dim">' + e(r.source === "swing" ? "swing" : r.source === "levels" ? "S/R " + (r.role === "mirror" ? "mirror" : "pick") + (r.labels && r.labels.setup ? " · " + r.labels.setup : "") : r.role === "counter_trend" ? "counter-trend short" : r.role === "primary" ? "pick" : r.role) + "</span>"; } },
        { label: "EXIT", w: "minmax(0,1fr)", cell: function (r) { return e(r.exit + (r.legs ? " · " + r.legs + "/4 targets" : "")); } },
        { label: "AFTER FEES", w: "100px", cell: function (r) { return C.signed(r.net_pct, function (x) { return (x > 0 ? "+" : "") + x.toFixed(2) + "%"; }); } },
        { label: "ROI 5×", w: "80px", cell: function (r) { return C.signed(r.roi_5x_pct, function (x) { return (x > 0 ? "+" : "") + x.toFixed(1) + "%"; }); } }
      ], d.recent || [], { minWidth: 640 });
      // S/R labels: each label value, picks next to their mirrors (a label only matters if the picks beat the mirrors under it)
      var lab = {};
      Object.keys(M).forEach(function (k) {                      // incl. open trades: S/R trades run up to 48 h
        if (k.indexOf("label:") !== 0) return;
        var p = k.slice(6).split("|"), id = p[1] + "|" + p.slice(2).join("|");
        (lab[id] = lab[id] || { k: p[1], v: p.slice(2).join("|") })[p[0]] = M[k];
      });
      var lrows = Object.keys(lab).sort().map(function (id) { return lab[id]; });
      var nice = { setup: "setup", level: "level", touches: "touches", move_24h: "coin 24 h move", trend_1h_ema200: "1h trend (EMA200)", atr_1h: "1h volatility",
        coin_news: "coin news", market_news_6h: "market news 6 h", btc: "BTC 24 h vs the trade", ai_briefing: "AI briefing", btc_24h: "BTC 24 h", funding: "funding", fear_greed: "Fear & Greed", event_window: "event window" };
      function av(x) { return x ? C.signed(x.avg_net_pct, function (y) { return (y > 0 ? "+" : "") + y.toFixed(2) + "%"; }) + ' <span class="dim tiny">n ' + x.n + " · 1st " + x.tp1_pct + "%</span>" : '<span class="dim">—</span>'; }
      var ltable = lrows.length ? '<h4 class="mono small" style="margin:14px 0 6px">S/R SETUPS BY LABEL · PICKS VS MIRRORS · INCL. OPEN TRADES AT THE CURRENT PRICE</h4>' + C.table([
        { label: "LABEL", w: "minmax(0,1fr)", cell: function (r) { return '<span class="dim">' + e(nice[r.k] || r.k) + ":</span> <b>" + e(r.v) + "</b>"; } },
        { label: "PICKS · AVG AFTER FEES", w: "190px", cell: function (r) { return av(r.primary); } },
        { label: "MIRRORS · AVG AFTER FEES", w: "190px", cell: function (r) { return av(r.mirror); } },
        { label: "PICK − MIRROR", w: "110px", cell: function (r) { return r.primary && r.mirror ? C.signed(r.primary.avg_net_pct - r.mirror.avg_net_pct, function (y) { return (y > 0 ? "+" : "") + y.toFixed(2) + "%"; }) : "—"; } }
      ], lrows, { minWidth: 700 }) + '<p class="mono tiny dim" style="margin:6px 0 0">With / against = the label points the same way as the trade, or the other way. Small groups move a lot: wait for 30+ trades before reading one.</p>' : "";
      // variations on the same signals (owner, 26 Oct 2026: "keep the bracket and try 2-3 new variation")
      var VG = [["Intraday picks", "intraday|primary"], ["Intraday mirrors (control)", "intraday|mirror"], ["Counter-trend shorts", "intraday|counter_trend"],
        ["S/R picks", "levels|primary"], ["S/R mirrors (control)", "levels|mirror"], ["Swing signals", "swing|primary"]];
      function vc(key) { var a = S[key], b = M[key]; if (!a && !b) return '<span class="dim">—</span>';
        return (a ? C.signed(a.avg_net_pct, function (y) { return (y > 0 ? "+" : "") + y.toFixed(2) + "%"; }) + ' <span class="dim tiny">n ' + a.n + "</span>" : '<span class="dim">no closed</span>') +
          (b ? '<br><span class="dim tiny">incl. open ' + (b.avg_net_pct > 0 ? "+" : "") + b.avg_net_pct.toFixed(2) + "% · n " + b.n + "</span>" : ""); }
      var vrows = VG.filter(function (g) { return S["variant:base|" + g[1]] || M["variant:base|" + g[1]]; });
      var vtable = vrows.length ? '<h4 class="mono small" style="margin:14px 0 6px">VARIATIONS ON THE SAME SIGNALS · AVG AFTER FEES</h4>' + C.table([
        { label: "SIGNALS", w: "minmax(0,1fr)", cell: function (g) { return "<b>" + e(g[0]) + "</b>"; } },
        { label: "BASE (5% STOP)", w: "140px", cell: function (g) { return vc("variant:base|" + g[1]); } },
        { label: "TIGHT (2.5% STOP)", w: "140px", cell: function (g) { return vc("variant:tight|" + g[1]); } },
        { label: "TRAIL AFTER 1ST TARGET", w: "150px", cell: function (g) { return vc("variant:trail|" + g[1]); } },
        { label: "BASE, WITH BTC ONLY", w: "150px", cell: function (g) { return vc("btc:" + g[1] + "|with"); } },
        { label: "BASE, AGAINST BTC", w: "140px", cell: function (g) { return vc("btc:" + g[1] + "|against"); } }
      ], vrows, { minWidth: 900 }) + '<p class="mono tiny dim" style="margin:6px 0 0">Started 26 Oct: trades from before then are base only. A variation helps only if it beats base for the picks AND the gap to their mirrors grows.</p>' : "";
      el.innerHTML = intro + tiles + wtiles + '<h4 class="mono small" style="margin:14px 0 6px">BY GROUP · CLOSED TRADES, AND INCL. OPEN ONES AT THE CURRENT PRICE</h4>' + table + vtable + ptable + ltable +
        '<h4 class="mono small" style="margin:14px 0 6px">LATEST CLOSED</h4>' + recent;
    }
  });
  // delta-hedged straddles (owner, 22 Oct 2026: "delta trading and hedging ... paper trade"; chose sell vs buy volatility)
  UI.widget("paper-vol", {
    title: "DELTA-HEDGED OPTIONS · SELL VS BUY VOLATILITY · PAPER RESEARCH", uses: ["vol"],
    render: function (el, D, ctx) {
      var d = D.vol; if (!d) { el.innerHTML = C.state(D, "vol"); return; }
      var pl = d.plan || {}, S = d.summary || {}, open = d.open || [];
      ctx.right((d.started ? "since " + U.stamp(d.started) : "") + (d.checked ? " · checked " + U.stamp(d.checked) : ""));
      function usd(x) { return C.signed(x, function (y) { return (y < 0 ? "−$" : "+$") + Math.abs(y).toFixed(2); }); }
      function pct(x, k) { return x == null ? "—" : C.signed(x, function (y) { return (y > 0 ? "+" : "") + y.toFixed(k == null ? 2 : k) + "%"; }); }
      function vol(x) { return x == null ? "—" : (x * 100).toFixed(1) + "%"; }
      var intro = '<p class="mono small dim" style="margin:0 0 10px">Delta hedging, as options desks do it: buy or sell an at-the-money call + put (a straddle) on Delta India, then keep the position\'s delta near zero by trading the perpetual future whenever it drifts more than ' + pl.hedge_band + ' per coin. With direction hedged away, a straddle is one bet: will the coin move <b>more</b> (buyer wins) or <b>less</b> (seller wins) than the options\' implied volatility priced in? ' +
        "Two mirror books for BTC and ETH, on the next-day and the weekly expiry: SELL (at the bid) and BUY (at the ask). $" + pl.notional_usd + " of coin per straddle, closed " + pl.close_before_min + " min before expiry; fees " + pl.option_fee_pct + "% per option leg and " + pl.perp_fee_pct + "% per hedge, + GST. No orders, paper only.</p>";
      var openT = C.table([
        { label: "BOOK", w: "minmax(0,1.2fr)", cell: function (r) { return "<b>" + e(r.underlying) + " " + e(r.tenor) + "</b> · " + (r.side < 0 ? '<b class="dn">SELL</b>' : '<b class="up">BUY</b>') + '<br><span class="dim tiny">strike ' + e(r.strike) + " · expires " + e(U.stamp(r.expiry)) + "</span>"; } },
        { label: "VOL PRICED IN → NOW", w: "130px", cell: function (r) { return e(vol(r.entry_iv)) + " → " + e(vol(r.iv_now)) + '<br><span class="dim tiny">moved so far ' + e(vol(r.realized_vol)) + "</span>"; } },
        { label: "OPTIONS", w: "90px", cell: function (r) { return usd(r.opt_pnl); } },
        { label: "HEDGE", w: "90px", cell: function (r) { return usd(r.hedge_pnl) + '<br><span class="dim tiny">' + e(r.hedges) + " trades</span>"; } },
        { label: "FEES", w: "70px", cell: function (r) { return '<span class="dim">$' + (r.fees || 0).toFixed(2) + "</span>"; } },
        { label: "TOTAL NOW", w: "100px", cell: function (r) { return usd(r.total_usd) + '<br><span class="dim tiny">' + pct(r.total_pct) + " of $" + e(r.notional) + "</span>"; } },
        { label: "DELTA", w: "70px", cell: function (r) { return e(r.delta_now == null ? "—" : r.delta_now.toFixed(2)); } }
      ], open, { minWidth: 760, empty: "Waiting for the first check: positions open within 5 minutes." });
      var groups = [["Selling volatility", "side:sell"], ["Buying volatility", "side:buy"], ["Sell · daily", "side_tenor:sell|daily"], ["Buy · daily", "side_tenor:buy|daily"], ["Sell · weekly", "side_tenor:sell|weekly"], ["Buy · weekly", "side_tenor:buy|weekly"]]
        .concat(Object.keys(S).filter(function (k) { return k.indexOf("book:") === 0; }).sort().map(function (k) { return [k.slice(5).replace(/\|/g, " · "), k]; }))
        .filter(function (g) { return S[g[1]]; });
      var res = groups.length ? C.table([
        { label: "GROUP", w: "minmax(0,1fr)", cell: function (g) { return "<b>" + e(g[0]) + "</b>"; } },
        { label: "CLOSED", w: "70px", cell: function (g) { return e(S[g[1]].n); } },
        { label: "IN PROFIT", w: "80px", cell: function (g) { return e(S[g[1]].win_pct + "%"); } },
        { label: "AVG", w: "80px", cell: function (g) { return pct(S[g[1]].avg_pct, 3); } },
        { label: "TOTAL", w: "90px", cell: function (g) { return usd(S[g[1]].total_usd); } },
        { label: "PRICED IN VS MOVED", w: "140px", cell: function (g) { return e(S[g[1]].avg_entry_iv + "% vs " + (S[g[1]].avg_realized_vol == null ? "—" : S[g[1]].avg_realized_vol + "%")); } },
        { label: "HEDGES / FEES", w: "110px", cell: function (g) { return '<span class="dim">' + e(S[g[1]].avg_hedges) + " · $" + e(S[g[1]].avg_fees_usd) + "</span>"; } }
      ], groups, { minWidth: 680 }) : C.empty("No closed straddles yet: the first next-day ones close 20 minutes before 12:00 UTC.");
      el.innerHTML = intro + '<h4 class="mono small" style="margin:4px 0 6px">OPEN NOW · MARKED AT DELTA\'S OPTION PRICES</h4>' + openT +
        '<h4 class="mono small" style="margin:14px 0 6px">CLOSED · BY BOOK</h4>' + res +
        '<p class="mono tiny dim" style="margin:6px 0 0">Seller wins when "moved" ends below "priced in". One day is one draw: read it after 2-3 weeks of daily expiries.</p>';
    }
  });
  // candidates (owner, 26 Oct 2026: "only we get some confidence from paper, so we can pick those strategies in live")
  UI.widget("paper-candidates", {
    title: "CANDIDATES · PAPER ONLY · WHICH STRATEGIES COULD GO LIVE", uses: ["candidates"],
    render: function (el, D, ctx) {
      var d = D.candidates; if (!d) { el.innerHTML = C.state(D, "candidates"); return; }
      var T = d.tracks || {}, g = d.gate || {};
      ctx.right((d.started ? "since " + U.stamp(d.started) + " · " : "") + d.days + " days · " + d.open + " open");
      function r(x) { return x == null ? "—" : C.signed(x, function (y) { return (y > 0 ? "+" : "") + y.toFixed(2) + "R"; }); }
      function pair(p) { return p && p[1] ? r(p[0]) + ' <span class="dim tiny">n ' + p[1] + "</span>" : '<span class="dim">—</span>'; }
      var intro = '<p class="mono small dim" style="margin:0 0 10px">Every strategy that is not live gets its own virtual trades with live-style exits: no slot limit, no one-per-coin rule, never sent to Delta, never in the paper wallet. ' +
        "One can qualify for live only when it has run " + g.days + "+ days, closed " + g.n + "+ trades, is profitable after fees, beats its control (the same signal on the other side), and is profitable in both rising and falling BTC weeks. You decide what goes live.</p>";
      var order = ["swing", "swing_ratchet", "swing_mirror", "intraday_pick", "intraday_mirror", "counter_trend", "counter_trend_mirror"];
      var rows = order.filter(function (k) { return T[k]; }).map(function (k) { return [k, T[k]]; });
      var chip = function (s) { var c = s.indexOf("qualifies") === 0 ? "up" : s === "not qualifying" ? "dn" : "dim"; return '<b class="' + c + '">' + e(s) + "</b>"; };
      var table = rows.length ? C.table([
        { label: "STRATEGY", w: "minmax(0,1.4fr)", cell: function (x) { return "<b>" + e(x[1].name) + "</b><br>" + chip(x[1].status); } },
        { label: "CLOSED", w: "70px", cell: function (x) { return e(x[1].closed ? x[1].closed.n : 0); } },
        { label: "AVG AFTER FEES", w: "110px", cell: function (x) { return x[1].closed ? r(x[1].closed.avg_r) + '<br><span class="dim tiny">win ' + x[1].closed.win_pct + "%</span>" : "—"; } },
        { label: "LONGS / SHORTS", w: "150px", cell: function (x) { return x[1].closed ? pair(x[1].closed.long) + "<br>" + pair(x[1].closed.short) : "—"; } },
        { label: "BTC UP / DOWN WEEKS", w: "150px", cell: function (x) { return x[1].closed ? pair(x[1].closed.rising_weeks) + "<br>" + pair(x[1].closed.falling_weeks) : "—"; } },
        { label: "OPEN NOW", w: "110px", cell: function (x) { return x[1].open_now ? r(x[1].open_now.avg_r) + ' <span class="dim tiny">n ' + x[1].open_now.n + "</span>" : "—"; } },
        { label: "GATE", w: "minmax(0,1fr)", cell: function (x) { return (x[1].gate || []).map(function (c) { return (c.ok ? "✅ " : "⬜ ") + e(c.check) + ' <span class="dim tiny">' + e(c.detail) + "</span>"; }).join("<br>"); } }
      ], rows, { minWidth: 980 }) : C.empty("Collecting: the first trades open a few minutes after the next signals.");
      var L = (T.swing && T.swing.labels) || {};
      var labs = Object.keys(L).filter(function (k) { return L[k]; });
      var why = labs.length ? '<h4 class="mono small" style="margin:14px 0 6px">SWING SIGNALS BY WHAT THE REAL PAPER BOOK DID WITH THEM</h4>' + C.table([
        { label: "PAPER BOOK", w: "minmax(0,1fr)", cell: function (k) { return "<b>" + e(k === "taken" ? "taken" : "skipped: " + k.replace(/_/g, " ")) + "</b>"; } },
        { label: "CLOSED", w: "70px", cell: function (k) { return e(L[k].n); } },
        { label: "AVG AFTER FEES", w: "120px", cell: function (k) { return r(L[k].avg_r); } },
        { label: "WIN", w: "70px", cell: function (k) { return e(L[k].win_pct + "%"); } }
      ], labs, { minWidth: 420 }) + '<p class="mono tiny dim" style="margin:6px 0 0">If the skipped ones do as well as the taken ones, that skip rule is costing trades.</p>' : "";
      el.innerHTML = intro + table + why;
    }
  });
  // the demo (testnet) panel was removed: owner 26 Oct 2026, "no use, demo doesn't run with proper live market rate"
  UI.view("book/paper", [["paper-strip"], ["paper-open"], ["paper-candidates"], ["paper-bracket"], ["paper-vol"], ["paper-history"]]);

  // ------------------------------------------------------------- live-book simulator (owner, 27 Oct 2026)
  // Replays the LIVE book (8 live swing specs, live exit, live sizing and limits, Delta fees) from a date with a
  // wallet: /api/sim/live-book. Play steps through the closed trades one by one.
  var SIM_SPECS = ["4h vol breakout", "4h Keltner", "4h Donchian", "4h Ichimoku", "8h Keltner", "8h vol breakout", "8h Donchian", "8h Ichimoku"];
  var SIM_COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX", "LINK", "SUI", "TIA", "XAUT"];
  function simDate(ms) { return ms ? new Date(ms).toISOString().slice(0, 10) : "—"; }
  function simChart(eq, start, n) {
    var pts = [[null, start]].concat(eq.slice(0, n).map(function (p) { return [p[0], p[1]]; }));
    if (pts.length < 2) return '<p class="mono small dim">No closed trades yet.</p>';
    var t0 = eq[0][0], t1 = eq[eq.length - 1][0], all = eq.map(function (p) { return p[1]; }).concat([start, start * 0.5]);
    var lo = Math.min.apply(null, all), hi = Math.max.apply(null, all), W = 1000, H = 260, P = 8;
    var logY = lo > 0 && hi / lo > 4;                        // compounding: a straight scale flattens the first year
    function f(v) { return logY ? Math.log(v) : v; }
    function x(t) { return t == null ? P : P + (t - t0) / Math.max(1, t1 - t0) * (W - 2 * P); }
    function y(v) { return H - P - (f(v) - f(lo)) / Math.max(1e-9, f(hi) - f(lo)) * (H - 2 * P); }
    var line = pts.map(function (q) { return x(q[0]).toFixed(1) + "," + y(q[1]).toFixed(1); }).join(" ");
    var last = pts[pts.length - 1][1], up = last >= start;
    return '<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" style="width:100%;height:220px;display:block" role="img" aria-label="wallet over time">' +
      '<line x1="0" x2="' + W + '" y1="' + y(start).toFixed(1) + '" y2="' + y(start).toFixed(1) + '" stroke="currentColor" stroke-opacity=".35" stroke-dasharray="6 6"/>' +
      '<line x1="0" x2="' + W + '" y1="' + y(start * 0.5).toFixed(1) + '" y2="' + y(start * 0.5).toFixed(1) + '" stroke="#e5484d" stroke-opacity=".6" stroke-dasharray="3 7"/>' +
      '<polyline fill="none" stroke="' + (up ? "#12a87b" : "#e5484d") + '" stroke-width="2.5" vector-effect="non-scaling-stroke" points="' + line + '"/></svg>' +
      '<p class="mono tiny dim" style="margin:4px 0 0">Dashed: the starting wallet · red dashed: the floor (half of it), where the live book stops opening trades' +
      (logY ? " · log scale (equal steps = equal % change)" : "") + ".</p>";
  }
  function simUrl(p) {
    var q = ["wallet=" + encodeURIComponent(p.wallet), "risk=" + encodeURIComponent(p.risk), "max_open=" + encodeURIComponent(p.max_open),
      "round_up=" + encodeURIComponent(p.round_up)];
    if (p.start) q.push("start=" + p.start); if (p.end) q.push("end=" + p.end);
    if (p.specs.length < SIM_SPECS.length) q.push("specs=" + p.specs.join(","));
    if (p.coins.length < SIM_COINS.length) q.push("coins=" + p.coins.join(","));
    return "/api/sim/live-book?" + q.join("&");
  }
  function simForm(el, ctx) {
    var p = ctx.s.p;
    function fld(k, label, type, extra) { return '<label class="fld"><span>' + e(label) + '</span><input type="' + type + '" data-act="f" data-k="' + k + '" value="' + e(p[k]) + '"' + (extra || "") + "></label>"; }
    el.querySelector(".sim-form").innerHTML =
      fld("start", "From", "date") + fld("end", "To (empty = latest)", "date") + fld("wallet", "Wallet ₹", "number", ' min="100" step="100" style="width:100px"') +
      fld("risk", "Risk % a trade", "number", ' min="0.1" max="10" step="0.5" style="width:80px"') + fld("max_open", "Max open (0 = no limit)", "number", ' min="0" max="20" step="1" style="width:80px"') +
      fld("round_up", "One-contract round-up %", "number", ' min="0" max="25" step="1" style="width:80px"') +
      '<button class="mini" data-act="run">' + (ctx.s.busy ? "Running…" : "Run") + '</button><button class="mini" data-act="play">' + (ctx.s.timer ? "Stop" : "Play") + "</button>" +
      '<div style="flex-basis:100%;margin-top:6px"><span class="mono tiny dim">Strategies </span>' + SIM_SPECS.map(function (n, i) { return C.chip("spec", n, p.specs.indexOf(i) >= 0).replace("data-act", 'data-i="' + i + '" data-act'); }).join("") + "</div>" +
      '<div style="flex-basis:100%;margin-top:4px"><span class="mono tiny dim">Coins </span>' + SIM_COINS.map(function (c) { return C.chip("coin", c, p.coins.indexOf(c) >= 0).replace("data-act", 'data-c="' + c + '" data-act'); }).join("") + "</div>";
  }
  function simStop(ctx) { if (ctx.s.timer) { clearInterval(ctx.s.timer); ctx.s.timer = null; } }
  UI.widget("sim-live", {
    title: "LIVE-BOOK SIMULATOR · WHAT THE LIVE BOOK WOULD HAVE DONE",
    mount: function (el, ctx) {
      ctx.s.p = { start: "2025-01-01", end: "", wallet: 800, risk: 3, max_open: 4, round_up: 10,
        specs: SIM_SPECS.map(function (_, i) { return i; }), coins: SIM_COINS.slice() };
      ctx.s.t = { sort: null };
      el.innerHTML = '<p class="mono small dim" style="margin:0 0 8px">The live book replayed: the 8 live swing strategies on the live coins, the live exit (3×ATR stop, 3R target, profit-lock ladder, 7 days), Delta fees and funding, and the live rules — whole Delta contracts, the one-contract round-up, the BTC volatility filter, one trade per coin, max open, 10% daily loss limit, floor at half the wallet, no news blackout. Past results, not a promise.</p>' +
        '<div class="toolbar sim-form" style="flex-wrap:wrap"></div><div data-slot="data" class="mono tiny dim" style="margin:6px 0"></div><div data-slot="out"></div>';
      simForm(el, ctx);
      UI.Net.get("/api/sim/live-book/meta").then(function (m) { ctx.s.meta = m; ctx.render(); }).catch(function () { /* shown on run */ });
    },
    unmount: function (el, ctx) { simStop(ctx); },
    on: {
      f: function (el, ev, t, ctx) { ctx.s.p[t.dataset.k] = t.value; },
      spec: function (el, ev, t, ctx) { var i = +t.dataset.i, a = ctx.s.p.specs, k = a.indexOf(i); if (k >= 0) { if (a.length > 1) a.splice(k, 1); } else a.push(i); simForm(el, ctx); },
      coin: function (el, ev, t, ctx) { var c = t.dataset.c, a = ctx.s.p.coins, k = a.indexOf(c); if (k >= 0) { if (a.length > 1) a.splice(k, 1); } else a.push(c); simForm(el, ctx); },
      run: function (el, ev, t, ctx) {
        simStop(ctx); ctx.s.busy = true; ctx.s.err = null; simForm(el, ctx);
        UI.Net.get(simUrl(ctx.s.p)).then(function (r) { ctx.s.res = r; ctx.s.step = null; })
          .catch(function (err) { ctx.s.err = err.message; })
          .then(function () { ctx.s.busy = false; simForm(el, ctx); ctx.render(); });
      },
      play: function (el, ev, t, ctx) {
        var r = ctx.s.res; if (!r || !r.equity.length) return;
        if (ctx.s.timer) { simStop(ctx); simForm(el, ctx); return; }
        var n = r.equity.length, per = Math.max(1, Math.ceil(n / 160));
        ctx.s.step = 0;
        ctx.s.timer = setInterval(function () {
          ctx.s.step = Math.min(n, ctx.s.step + per); ctx.render();
          if (ctx.s.step >= n) { simStop(ctx); simForm(el, ctx); }
        }, 70);
        simForm(el, ctx);
      },
      sort: function (el, ev, t, ctx) { C.sortState(ctx.s.t, t.dataset.k); ctx.render(); }
    },
    render: function (el, D, ctx) {
      var m = ctx.s.meta, out = el.querySelector('[data-slot="out"]');
      el.querySelector('[data-slot="data"]').textContent = m && m.trades ? "Data: " + m.trades + " trades, signals " + simDate(m.first_entry_ms) + " → " + simDate(m.last_entry_ms) + " · refreshed " + (m.refreshed_at || "—") : "";
      if (ctx.s.err) { out.innerHTML = C.empty("Could not run: " + ctx.s.err); return; }
      var r = ctx.s.res;
      if (!r) { out.innerHTML = C.empty(ctx.s.busy ? "Running…" : "Pick a start date and a wallet, then Run."); return; }
      var S = r.summary, n = ctx.s.step == null ? r.equity.length : ctx.s.step;
      var shown = r.trades.slice(0, n), w = n ? shown[n - 1].wallet_after : S.start_inr;
      var won = shown.filter(function (x) { return x.pnl_inr > 0; }).length;
      var sk = S.skips || {}, why = [["filter", "BTC too wild"], ["full", "book full"], ["coin_open", "coin already open"], ["too_small", "below one contract"],
        ["too_wide", "stop too wide for the leverage rule"], ["daily_loss", "daily loss limit"], ["floor", "below the floor"]]
        .filter(function (k) { return sk[k[0]]; }).map(function (k) { return sk[k[0]] + " " + k[1]; }).join(" · ");
      out.innerHTML = C.tiles([
          ["WALLET", rs(S.start_inr) + " → " + rs(w), (w / S.start_inr).toFixed(2) + "×" + (ctx.s.step != null && n < r.equity.length ? " so far" : ""), w >= S.start_inr ? "up" : "dn"],
          ["TRADES", String(n), won + " won · " + (n - won) + " lost" + (n ? " · " + Math.round(100 * won / n) + "% win" : "")],
          ["AVG R A TRADE", S.avg_r == null ? "—" : (S.avg_r > 0 ? "+" : "") + S.avg_r.toFixed(2) + "R", "after fees and funding, whole run"],
          ["DEEPEST DROP", S.max_dd_pct + "%", "from a high, closed trades"],
          ["LONGEST LOSING RUN", String(S.losing_streak), "trades in a row"],
          ["TRADES A MONTH", String(S.per_month), S.signals + " signals in the window"]]) +
        simChart(r.equity, S.start_inr, n) +
        (why ? '<p class="mono tiny dim" style="margin:6px 0">Signals not taken: ' + e(why) + ".</p>" : "") +
        '<h4 class="mono small" style="margin:14px 0 6px">BY MONTH</h4>' + C.table([
          { label: "MONTH", w: "90px", cell: function (x) { return "<b>" + e(x.month) + "</b>"; } },
          { label: "TRADES", w: "70px", cell: function (x) { return e(x.trades) + ' <span class="dim tiny">' + e(x.won) + " won</span>"; } },
          { label: "P&L", w: "110px", cell: function (x) { return C.signed(x.pnl_inr, function (y) { return rs(y, true); }); } },
          { label: "WALLET AT MONTH END", w: "minmax(0,1fr)", cell: function (x) { return e(rs(x.wallet_end)); } }
        ], r.months.slice().reverse(), { minWidth: 440 }) +
        '<h4 class="mono small" style="margin:14px 0 6px">EVERY TRADE · NEWEST FIRST</h4>' + C.table([
          { label: "OPENED", w: "92px", cell: function (x) { return e(simDate(x.entry_t)); }, sort: function (x) { return x.entry_t; } },
          { label: "CLOSED", w: "92px", cell: function (x) { return e(simDate(x.exit_t)); }, sort: function (x) { return x.exit_t; } },
          { label: "COIN", w: "64px", cell: function (x) { return "<b>" + e(x.coin) + "</b>"; }, sort: function (x) { return x.coin; } },
          { label: "SIDE", w: "70px", cell: function (x) { return C.side(x.side); } },
          { label: "SETUP", w: "minmax(0,1fr)", cell: function (x) { return e(x.setup); } },
          { label: "RISK", w: "80px", cell: function (x) { return e(rs(x.risk_inr)) + ' <span class="dim tiny">' + e(x.contracts) + "c</span>"; } },
          { label: "R", w: "64px", cell: function (x) { return C.signed(x.r, function (y) { return (y > 0 ? "+" : "") + y.toFixed(2); }); }, sort: function (x) { return x.r; } },
          { label: "P&L", w: "90px", cell: function (x) { return C.signed(x.pnl_inr, function (y) { return rs(y, true); }); }, sort: function (x) { return x.pnl_inr; } },
          { label: "EXIT", w: "70px", cell: function (x) { return '<span class="dim">' + e(x.reason) + "</span>"; } },
          { label: "WALLET", w: "90px", cell: function (x) { return e(rs(x.wallet_after)); } }
        ], shown.slice().reverse(), { minWidth: 900, state: ctx.s.t, limit: 300 });
      ctx.right(r.params.start ? "from " + r.params.start : "");
    }
  });
  UI.view("book/simulator", [["sim-live"]]);

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
        d.live ? ["ok", "LIVE: real orders are ON", "Swing signals are copied to Delta India with real money. Risk " + Math.round(d.risk_pct * 1000) / 10 + "% per trade, max " + d.max_open + " open, leverage ≥ " + d.min_leverage + "x" +
          (d.round_up_pct ? "; when 3% buys less than one contract, one contract is taken if it risks at most " + Math.round(d.round_up_pct * 100) + "% of the wallet" : "") + "."] :
        ["amber", "Not live: no real orders (mode " + String(d.mode).toUpperCase() + (d.live_orders ? "" : ", live orders OFF") + ")", "Turn on 'Delta book mode = live' and 'Allow live Delta orders' on Settings to start."];
      var dayLeft = day.limit_inr != null && day.pnl_inr != null ? day.limit_inr + Math.min(0, day.pnl_inr) : null;
      el.innerHTML = C.flag(state[0], state[1], state[2], d.error || "") +
        // the same numbers as the Delta app (19 Oct 2026): balance, P&L at the mark price, available after open losses
        C.tiles([["WALLET ON DELTA", rs(w.total_inr), "balance, before open trades"],
                 ["OPEN P&L", rs(w.upnl_inr, true), "equity " + rs(w.equity_inr), w.upnl_inr < 0 ? "dn" : w.upnl_inr > 0 ? "up" : ""],
                 ["AVAILABLE MARGIN", rs(w.free_inr), "free for a new trade, after open losses"],
                 ["TODAY · CLOSED TRADES", rs(day.pnl_inr, true), day.start_inr != null ? "from " + rs(day.start_inr) + " at 05:30 IST" : "day starts at first check", day.pnl_inr < 0 ? "dn" : ""],
                 ["DAILY LIMIT LEFT", dayLeft != null ? rs(dayLeft) : "—", "stops new orders at −" + Math.round(d.daily_loss_pct * 100) + "%"],
                 ["FLOOR", rs(d.floor_inr), d.deposited_inr ? Math.round(d.floor_frac * 100) + "% of " + rs(d.deposited_inr) + " put in · no new orders below" : "no new orders below"],
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
