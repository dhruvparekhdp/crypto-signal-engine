/* Portal core: shared store, component kit, widget runtime and the app shell.
   Spec: docs/PORTAL_REVAMP.md §4.

   Micro UI model
   - Store: one entry per endpoint. Widgets subscribe; an endpoint polls only while something is
     subscribed, every subscriber shares one request, and polling pauses while the tab is hidden.
   - Widgets: self-contained units registered by the hub modules (hubs/*.js), each declaring the
     store keys it uses. The shell mounts the widgets of the open tab and unmounts the rest.
   - Components (UI.C): pure functions returning HTML for the pieces every page repeats
     (panel, tiles, table, bars, tags, meters, flags, link grids…). All text goes through esc().
   - Hub modules load on first visit to their hub, never before. */
(function () {
  "use strict";
  var Nav = window.PortalNav;
  var ASSETS = window.PORTAL_ASSETS || {};

  // ---------------------------------------------------------------- utilities
  var U = {};
  U.$ = function (sel, root) { return (root || document).querySelector(sel); };
  U.esc = function (v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  U.pad = function (n) { return String(n).padStart(2, "0"); };
  U.hms = function (d) { return U.pad(d.getUTCHours()) + ":" + U.pad(d.getUTCMinutes()) + ":" + U.pad(d.getUTCSeconds()); };
  U.dur = function (ms) { var s = Math.max(0, Math.floor(ms / 1000)); return U.pad(Math.floor(s / 3600)) + ":" + U.pad(Math.floor(s / 60) % 60) + ":" + U.pad(s % 60); };
  U.num = function (x) { return x != null && isFinite(x); };
  U.px = function (p) {
    if (!U.num(p)) return "—";
    var a = Math.abs(p), d = a >= 10 ? 2 : a >= 1 ? 3 : a >= 0.01 ? 5 : 7;
    return Number(p).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  };
  U.pct = function (x, d) { return U.num(x) ? (x >= 0 ? "+" : "") + Number(x).toFixed(d == null ? 2 : d) + "%" : "—"; };
  U.rr = function (x) { return U.num(x) ? (x >= 0 ? "+" : "") + Number(x).toFixed(2) + "R" : "—"; };
  U.inr = function (x, signed) {
    if (!U.num(x)) return "—";
    var s = "₹" + Math.abs(x).toLocaleString("en-IN", { maximumFractionDigits: Math.abs(x) < 100 ? 2 : 0 });
    return (x < 0 ? "−" : signed ? "+" : "") + s;
  };
  U.compact = function (n) {
    if (!U.num(n)) return "—";
    var a = Math.abs(n);
    return a >= 1e9 ? (n / 1e9).toFixed(2) + "B" : a >= 1e6 ? (n / 1e6).toFixed(1) + "M" : a >= 1e3 ? (n / 1e3).toFixed(0) + "K" : String(Math.round(n));
  };
  U.base = function (sym) { return String(sym || "").toUpperCase().replace(/USDT$|INR$/, ""); };
  U.ago = function (iso) {
    if (!iso) return "—";
    var t = typeof iso === "number" ? iso : new Date(iso).getTime();
    var m = Math.round((Date.now() - t) / 60000);
    return m < 1 ? "just now" : m < 60 ? m + "m ago" : m < 2880 ? Math.round(m / 60) + "h ago" : Math.round(m / 1440) + "d ago";
  };
  U.stamp = function (iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short" }) + " " + U.pad(d.getHours()) + ":" + U.pad(d.getMinutes());
  };
  U.hours = function (h) { return !U.num(h) ? "—" : h < 1 / 60 ? Math.round(h * 3600) + "s" : h < 1 ? Math.round(h * 60) + "m" : h.toFixed(1) + "h"; };
  U.name = function (s) { return String(s || "").replace(/^swing_/, "").replace(/[_@:]/g, " "); };
  U.median = function (xs) { var v = xs.filter(U.num).sort(function (a, b) { return a - b; }); return v.length ? v[Math.floor(v.length / 2)] : null; };

  // ---------------------------------------------------------------- network
  var Net = {};
  Net.get = function (url) {
    return fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          var e = new Error(b.error || b.reason || ("HTTP " + r.status)); e.status = r.status; throw e;
        });
      }
      return r.json();
    });
  };
  // Same token flow as the classic pages (localStorage 'api_token', asked once on a 401).
  Net.post = function (url, body) {
    function go(token) {
      var h = { "Content-Type": "application/json" };
      if (token) h.Authorization = "Bearer " + token;
      return fetch(url, { method: "POST", headers: h, body: JSON.stringify(body || {}), credentials: "same-origin" });
    }
    var tok = null; try { tok = localStorage.getItem("api_token"); } catch (e) { /* private mode */ }
    return go(tok).then(function (r) {
      if (r.status !== 401) return r;
      var entered = window.prompt("API token needed for this action (API_AUTH_TOKEN on the server):");
      if (!entered) return r;
      try { localStorage.setItem("api_token", entered); } catch (e) { /* ignore */ }
      return go(entered);
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (b) {
        if (!r.ok) throw new Error(b.error || ("HTTP " + r.status));
        return b;
      });
    });
  };

  // ---------------------------------------------------------------- store
  var Store = { src: {} };
  Store.def = function (key, url, every) { Store.src[key] = Store.src[key] || { key: key, url: url, every: every || 0, subs: [], data: undefined, err: "", at: 0 }; return Store.src[key]; };
  Store.get = function (key) { var s = Store.src[key]; return s ? s.data : undefined; };
  Store.fetch = function (key) {
    var s = Store.src[key]; if (!s || s.inflight) return s && s.inflight;
    s.inflight = Net.get(s.url).then(function (d) { s.data = d; s.err = ""; }).catch(function (e) { s.err = e.message || "failed"; s.status = e.status; })
      .then(function () { s.at = Date.now(); s.inflight = null; s.subs.slice().forEach(function (fn) { fn(key); }); });
    return s.inflight;
  };
  Store.use = function (key, fn) {
    var s = Store.src[key]; if (!s) throw new Error("unknown store key " + key);
    s.subs.push(fn);
    if (s.subs.length === 1) {
      if (!s.at || (s.every && Date.now() - s.at > s.every)) Store.fetch(key); else setTimeout(function () { fn(key); }, 0);
      if (s.every) s.timer = setInterval(function () { if (!document.hidden) Store.fetch(key); }, s.every);
    } else if (s.at) setTimeout(function () { fn(key); }, 0);
    return function () {
      var i = s.subs.indexOf(fn); if (i >= 0) s.subs.splice(i, 1);
      if (!s.subs.length && s.timer) { clearInterval(s.timer); s.timer = null; }
    };
  };
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) return;
    Object.keys(Store.src).forEach(function (k) { var s = Store.src[k]; if (s.subs.length && s.every && Date.now() - s.at > s.every) Store.fetch(k); });
  });
  // The shared endpoints. Hub modules may add their own with Store.def.
  Store.def("coins", "/api/crypto/coins", 5000);
  Store.def("swing", "/api/swing", 30000);
  Store.def("signals", "/api/portal/signals?days=14&limit=40", 30000);
  Store.def("meta", "/api/portal/meta", 300000);
  Store.def("budget", "/api/llm/budget", 60000);
  Store.def("status", "/api/status", 30000);
  Store.def("paper", "/api/paper", 20000);
  Store.def("history7", "/api/signals/history?days=7", 60000);
  Store.def("archive", "/api/signals/history?days=365&before=7", 300000);
  Store.def("accuracy", "/api/signals/accuracy", 300000);
  Store.def("research", "/api/research", 300000);
  Store.def("v2", "/api/v2/shadow", 300000);
  Store.def("events", "/api/events", 120000);
  Store.def("tables", "/api/tables?limit=50", 0);
  Store.def("debug", "/api/debug", 30000);
  Store.def("perf", "/api/debug/perf", 60000);

  // ---------------------------------------------------------------- components
  var e = U.esc, C = {};
  C.empty = function (msg) { return '<div class="empty">' + e(msg) + "</div>"; };
  C.loading = function () { return '<div class="empty">Loading…</div>'; };
  C.state = function (D, key, emptyMsg) {   // standard loading / error line for one store key
    var s = Store.src[key];
    if (s && s.err && D[key] === undefined) return '<div class="empty err">' + (s.status === 401 || s.status === 403 ? "Admin login needed: sign in on Settings." : "Unavailable: " + e(s.err)) + "</div>";
    if (D[key] === undefined) return C.loading();
    return emptyMsg ? C.empty(emptyMsg) : "";
  };
  C.tiles = function (items) {     // [[label, value, sub, cls]]
    return '<div class="kv">' + items.map(function (t) {
      return "<div" + (t[3] ? ' class="' + e(t[3]) + '"' : "") + "><small>" + e(t[0]) + "</small><b>" + e(t[1]) + "</b>" + (t[2] ? "<span>" + e(t[2]) + "</span>" : "") + "</div>";
    }).join("") + "</div>";
  };
  C.tag = function (text, kind) { return '<span class="st-tag' + (kind ? " " + kind : "") + '">' + e(text) + "</span>"; };
  C.side = function (dir) { var s = String(dir).toLowerCase() === "short"; return '<span class="side-tag' + (s ? " short" : "") + '">' + (s ? "SHORT" : "LONG") + "</span>"; };
  C.signed = function (x, fmt) { return '<b class="' + (x >= 0 ? "up" : "dn") + '">' + e((fmt || U.pct)(x)) + "</b>"; };
  C.meter = function (share, hot) {
    var w = U.num(share) ? Math.max(0, Math.min(100, share * 100)) : 0;
    return '<div class="meter"><i class="' + (hot || share >= 0.75 ? "hot" : "") + '" style="width:' + w.toFixed(1) + '%"></i></div>';
  };
  C.rbar = function (R) {
    var prog = U.num(R) ? (Math.max(-1, Math.min(3, R)) + 1) / 4 * 100 : 25;
    return '<div class="rbar"><div class="rbar-fill' + (R < 0 ? " dn" : "") + '" style="width:' + prog.toFixed(1) + '%"></div><div class="rbar-zero"></div></div>';
  };
  C.bars = function (rows, opts) {   // [[label, value, note, cls]] horizontal bars, scaled to the max
    opts = opts || {};
    var mx = Math.max.apply(null, rows.map(function (r) { return r[1] || 0; }).concat([opts.max || 1]));
    return '<div class="bars">' + rows.map(function (r) {
      return '<div class="bar-row"><div class="bar-top"><span>' + e(r[0]) + "</span><em>" + e(r[2]) + '</em></div><div class="bar-track"><i class="' + (r[3] || "") + '" style="width:' + ((r[1] || 0) / mx * 100).toFixed(1) + '%"></i></div></div>';
    }).join("") + "</div>";
  };
  C.hist = function (buckets, opts) {   // [{label, n, cls}] vertical histogram
    opts = opts || {};
    var mx = Math.max.apply(null, buckets.map(function (b) { return b.n; }).concat([1]));
    return '<div class="hist">' + buckets.map(function (b) {
      return '<div class="hist-col" title="' + e(b.n + " " + (opts.unit || "")) + '"><i class="' + (b.cls || "") + '" style="height:' + (b.n / mx * 100).toFixed(1) + '%"></i><span>' + e(b.label) + "</span></div>";
    }).join("") + "</div>";
  };
  C.flag = function (level, title, desc, extra) {
    return '<div class="flag ' + e(level) + '"><b>' + e(title) + "</b><p>" + e(desc) + "</p>" + (extra ? '<span class="mono tiny">' + e(extra) + "</span>" : "") + "</div>";
  };
  C.links = function (list) {        // [[href, title, sub]]
    return '<div class="links">' + list.map(function (l) { return '<a href="' + e(l[0]) + '"><b>' + e(l[1]) + "</b><span>" + e(l[2]) + "</span></a>"; }).join("") + "</div>";
  };
  C.pre = function (text) { return '<pre class="pre">' + e(text) + "</pre>"; };
  /* Data table. cols: [{label, w, cell(row) -> html, sort(row) -> value, cls}]. Sorting is driven by
     the widget's own state ({sort, dir}); headers carry data-act="sort" data-k=<index>. */
  C.table = function (cols, rows, opts) {
    opts = opts || {};
    if (!rows.length) return C.empty(opts.empty || "Nothing to show.");
    var st = opts.state || {};
    if (st.sort != null && cols[st.sort] && cols[st.sort].sort) {
      var f = cols[st.sort].sort, dir = st.dir || -1;
      rows = rows.slice().sort(function (a, b) { var x = f(a), y = f(b); return (x > y ? 1 : x < y ? -1 : 0) * dir; });
    }
    var tpl = "grid-template-columns:" + cols.map(function (c) { return c.w || "minmax(0,1fr)"; }).join(" ");
    var max = opts.limit || 400;
    return '<div class="scroll-x"><div class="table" style="min-width:' + (opts.minWidth || 620) + 'px"><div class="trow head" style="' + tpl + '">' +
      cols.map(function (c, i) {
        var on = st.sort === i ? (st.dir > 0 ? " ▲" : " ▼") : "";
        return c.sort ? '<button class="th" data-act="sort" data-k="' + i + '">' + e(c.label) + on + "</button>" : "<span>" + e(c.label) + "</span>";
      }).join("") + "</div>" +
      rows.slice(0, max).map(function (r) {
        return '<div class="trow' + (opts.rowCls ? " " + opts.rowCls(r) : "") + '" style="' + tpl + '">' + cols.map(function (c) { return "<span" + (c.cls ? ' class="' + c.cls + '"' : "") + ">" + c.cell(r) + "</span>"; }).join("") + "</div>";
      }).join("") + "</div></div>" +
      (rows.length > max ? '<p class="mono tiny dim">showing ' + max + " of " + rows.length + "</p>" : "");
  };
  C.sortState = function (st, k) {   // toggle helper for data-act="sort"
    k = +k; if (st.sort === k) st.dir = -(st.dir || -1); else { st.sort = k; st.dir = -1; }
  };
  C.select = function (act, label, options, value) {
    return '<label class="fld"><span>' + e(label) + '</span><select data-act="' + e(act) + '">' + options.map(function (o) {
      var v = Array.isArray(o) ? o[0] : o, t = Array.isArray(o) ? o[1] : o;
      return '<option value="' + e(v) + '"' + (String(v) === String(value) ? " selected" : "") + ">" + e(t) + "</option>";
    }).join("") + "</select></label>";
  };
  C.search = function (act, placeholder, value) {
    return '<label class="fld grow"><span class="sr">' + e(placeholder) + '</span><input type="search" data-act="' + e(act) + '" placeholder="' + e(placeholder) + '" value="' + e(value || "") + '" autocomplete="off"></label>';
  };
  C.chip = function (act, label, on) { return '<button class="chipbtn' + (on ? " on" : "") + '" data-act="' + e(act) + '" aria-pressed="' + !!on + '">' + e(label) + "</button>"; };

  // ---------------------------------------------------------------- derived facts (shared by hubs)
  var F = {};
  F.coin = function (D, sym) {
    var u = String(sym || "").toUpperCase(), c = D.coins || [];
    for (var i = 0; i < c.length; i++) if (c[i].symbol === u) return c[i];
    return null;
  };
  F.regime = function (D) {
    var rf = (D.swing && D.swing.regime_filter) || {}, now = rf.now || {};
    var max = rf.vol_rank_max != null ? rf.vol_rank_max : (D.meta ? D.meta.vol_rank_max : 0.67);
    var mode = rf.mode || (D.meta && D.meta.regime_filter) || "";
    var rank = now.btc_vol_rank;
    return { rank: rank, max: max, mode: mode, closed: mode === "on" && rank != null && rank >= max, known: rank != null };
  };
  F.openSwing = function (D) {
    return ((D.swing && D.swing.open) || []).map(function (p) {
      var c = F.coin(D, p.symbol), mark = c ? c.price : null, dir = String(p.side).toLowerCase() === "short" ? -1 : 1;
      var risk = Math.abs(p.entry - p.stop);
      return { p: p, mark: mark, dir: dir, R: mark != null && risk > 0 ? dir * (mark - p.entry) / risk : null };
    });
  };
  F.riskPct = function (D) { var r = D.swing && D.swing.rules && D.swing.rules.risk_per_trade; return r == null ? null : (r <= 1 ? +(r * 100).toFixed(2) : r); };
  F.budgetShare = function (m) { var l = m.limits || {}; return l.tpd ? m.today_tokens / l.tpd : l.rpd ? m.today_calls / l.rpd : null; };
  F.live = function (D) { return !!(D.meta && D.meta.live_trading_mode && D.meta.live_trading_mode !== "off"); };

  // ---------------------------------------------------------------- widget runtime
  var W = { defs: {}, views: {}, mounted: [] };
  W.widget = function (name, def) { W.defs[name] = def; };
  W.view = function (key, rows) { W.views[key] = rows; };
  function snapshot() { var D = {}; Object.keys(Store.src).forEach(function (k) { D[k] = Store.src[k].data; }); return D; }

  function mountWidget(name, host) {
    var def = W.defs[name]; if (!def) { host.innerHTML = C.empty("Missing widget " + name); return null; }
    var el = document.createElement("div");
    el.className = def.bare ? "w-bare" : "panel" + (def.shadow === false ? "" : " shadow") + (def.cls ? " " + def.cls : "");
    el.dataset.w = name;
    var body = el;
    if (def.title && !def.bare) {
      el.innerHTML = '<div class="ph"><h2>' + e(def.title) + '</h2><span class="mono dim small ph-r"></span></div><div class="w-body"></div>';
      body = el.querySelector(".w-body");
    }
    host.appendChild(el);
    var inst = { name: name, def: def, el: el, body: body, s: {}, timers: [], unsub: [], pending: false };
    inst.ctx = {
      s: inst.s, el: el, body: body,
      right: function (t) { var r = el.querySelector(".ph-r"); if (r) r.textContent = t || ""; },
      every: function (fn, ms) { inst.timers.push(setInterval(fn, ms)); },
      render: function () { schedule(inst); },
      D: snapshot
    };
    if (def.mount) def.mount(body, inst.ctx);
    (def.uses || []).forEach(function (k) { inst.unsub.push(Store.use(k, function () { schedule(inst); })); });
    if (!(def.uses || []).length) schedule(inst);
    return inst;
  }
  function schedule(inst) {
    if (inst.pending || inst.dead) return;
    inst.pending = true;
    requestAnimationFrame(function () {
      inst.pending = false; if (inst.dead) return;
      try { inst.def.render(inst.body, snapshot(), inst.ctx); }
      catch (err) { inst.body.innerHTML = C.empty("This panel failed to draw: " + err.message); if (window.console) console.error(err); }
    });
  }
  function unmountAll() {
    W.mounted.forEach(function (inst) {
      inst.dead = true; inst.unsub.forEach(function (u) { u(); }); inst.timers.forEach(clearInterval);
      if (inst.def.unmount) inst.def.unmount(inst.body, inst.ctx);
    });
    W.mounted = [];
  }
  /* A view is rows of cells. A cell is "name", {w: name, size: "grow"|"side"|"full"} or
     {stack: [cells], size}. */
  function buildView(rows, host) {
    rows.forEach(function (row) {
      var r = document.createElement("div"); r.className = "row"; host.appendChild(r);
      row.forEach(function (cell) { buildCell(cell, r, row.length === 1); });
    });
  }
  function buildCell(cell, parent, alone) {
    if (typeof cell === "string") cell = { w: cell };
    var box = document.createElement("div");
    box.className = "cell " + (cell.size || (alone ? "full" : "grow"));
    parent.appendChild(box);
    if (cell.stack) cell.stack.forEach(function (c) { buildCell(c, box, true); });
    else { var inst = mountWidget(cell.w, box); if (inst) W.mounted.push(inst); }
  }
  // Delegated events: any [data-act] inside a widget calls def.on[act](body, event, target, ctx).
  function delegate(type) {
    document.addEventListener(type, function (ev) {
      var t = ev.target.closest && ev.target.closest("[data-act]"); if (!t) return;
      if (type === "click" && (t.tagName === "SELECT" || t.tagName === "INPUT")) return;
      var host = t.closest("[data-w]"); if (!host) return;
      var inst = W.mounted.filter(function (i) { return i.el === host; })[0];
      var fn = inst && inst.def.on && inst.def.on[t.dataset.act];
      if (fn) { if (type === "click") ev.preventDefault(); fn(inst.body, ev, t, inst.ctx); }
    });
  }
  ["click", "input", "change"].forEach(delegate);

  // ---------------------------------------------------------------- shell
  var Shell = { hub: null, tab: null, loaded: {} };
  function loadHub(id) {
    if (Shell.loaded[id]) return Shell.loaded[id];
    Shell.loaded[id] = new Promise(function (ok, fail) {
      var s = document.createElement("script");
      s.src = ASSETS["hubs/" + id + ".js"] || "/portal/static/hubs/" + id + ".js";
      s.onload = ok; s.onerror = function () { Shell.loaded[id] = null; fail(new Error("could not load " + id)); };
      document.head.appendChild(s);
    });
    return Shell.loaded[id];
  }
  function drawNav(h, t) {
    U.$("#hubTabs").innerHTML = Nav.NAV.map(function (x, i) {
      return '<a class="tab' + (x === h ? " on" : "") + '"' + (x === h ? ' aria-current="page"' : "") + ' href="' + Nav.href(x, x.tabs[0]) + '" data-nav="1">0' + (i + 1) + " " + e(x.label.toUpperCase()) + "</a>";
    }).join("");
    U.$("#subTabs").innerHTML = h.tabs.map(function (x) {
      return '<a class="sub' + (x === t ? " on" : "") + (x.native ? "" : " tool") + '"' + (x === t ? ' aria-current="page"' : "") + ' href="' + e(Nav.href(h, x)) + '"' + (x.native ? ' data-nav="1"' : "") + ">" + e(x.label) + (x.native ? "" : " ↗") + "</a>";
    }).join("");
    var word = h.label.toUpperCase();
    U.$("#logoWord").textContent = word; U.$("#logoWord").setAttribute("data-text", word);
    document.title = "Signal Engine · " + h.label + (t !== h.tabs[0] ? " · " + t.label : "");
  }
  function route() {
    var loc = Nav.locate(location.pathname, "");
    if (!loc) { history.replaceState({}, "", "/command"); loc = Nav.locate("/command", ""); }
    if (!loc.native) { location.replace(loc.tab.url); return; }
    if (!loc.known) history.replaceState({}, "", Nav.href(loc.hub, loc.tab));
    var h = loc.hub, t = loc.tab;
    drawNav(h, t);
    if (Shell.hub === h.id && Shell.tab === t.id) return;
    Shell.hub = h.id; Shell.tab = t.id;
    unmountAll();
    var host = U.$("#view"); host.innerHTML = C.loading();
    loadHub(h.id).then(function () {
      if (Shell.hub !== h.id || Shell.tab !== t.id) return;
      host.innerHTML = "";
      var rows = W.views[h.id + "/" + t.id];
      if (!rows) { host.innerHTML = C.empty("This view is not built yet."); return; }
      buildView(rows, host);
      window.scrollTo(0, 0);
    }).catch(function (err) { host.innerHTML = C.empty("Could not load this hub: " + err.message); });
  }
  document.addEventListener("click", function (ev) {
    var a = ev.target.closest && ev.target.closest("a[data-nav]");
    if (!a || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button) return;
    ev.preventDefault();
    if (a.getAttribute("href") !== location.pathname) history.pushState({}, "", a.getAttribute("href"));
    route();
  });
  window.addEventListener("popstate", route);

  // status strip + ticker + toast: shell-level, always subscribed
  function drawStrip() {
    var D = snapshot(), g = F.regime(D), open = F.openSwing(D), risk = F.riskPct(D);
    var gate = U.$("#stripGate");
    gate.textContent = !g.known ? "FILTER · NO READING YET" : g.mode !== "on" ? "FILTER " + String(g.mode || "OFF").toUpperCase() : g.closed ? "FILTER CLOSED" : "FILTER OPEN";
    gate.classList.toggle("closed", g.closed);
    U.$("#stripOpen").textContent = "OPEN SWING TRADES " + (D.swing ? open.length : "…");
    U.$("#stripRisk").textContent = risk == null ? "WORST CASE …" : "WORST CASE −" + (open.length * risk).toFixed(1) + "% WALLET";
    var coins = D.coins || [], newest = coins.reduce(function (m, c) { return c.timestamp && c.timestamp > m ? c.timestamp : m; }, "");
    U.$("#stripFeed").textContent = coins.length ? "FEED " + coins.length + " COINS · " + U.ago(newest) : "FEED …";
    U.$("#stripMode").textContent = F.live(D) ? "LIVE TRADING: " + String(D.meta.live_trading_mode).toUpperCase() : "LIVE TRADING: OFF · PAPER ONLY";
  }
  var tapeKey = "";
  function drawTape() {
    var coins = Store.get("coins") || [], key = coins.map(function (c) { return c.symbol; }).join();
    var track = U.$("#tape");
    if (key !== tapeKey) {
      tapeKey = key;
      var html = coins.map(function (c) { return '<span data-t="' + e(c.symbol) + '"><b>' + e(U.base(c.symbol)) + "</b><em></em><b></b></span>"; }).join("");
      track.innerHTML = html + html;
    }
    track.querySelectorAll("[data-t]").forEach(function (s) {
      var c = F.coin({ coins: coins }, s.dataset.t); if (!c) return;
      s.children[1].textContent = U.px(c.price);
      s.children[2].textContent = (c.change_24h_pct >= 0 ? "▲ " : "▼ ") + U.pct(c.change_24h_pct);
    });
  }
  var seen = null;
  function watchSignals() {
    var list = (Store.get("signals") || {}).signals || [];
    if (seen && list.length && !seen[list[0].id]) toast(list[0]);
    seen = {}; list.forEach(function (x) { seen[x.id] = 1; });
  }
  function toast(s) {
    var t = U.$("#toast"), traded = s.status === "TRADED";
    t.className = "toast" + (traded ? "" : s.status === "FILTER BLOCK" ? " skip" : " neutral");
    t.innerHTML = "<small>" + (traded ? "SIGNAL LOCKED" : e(s.status)) + "</small><b>" + e(U.base(s.symbol) + " " + String(s.direction).toUpperCase() + " · " + U.name(s.strategy)) + "</b>";
    t.hidden = false; t.style.animation = "none"; void t.offsetWidth; t.style.animation = "";
    clearTimeout(toast.h); toast.h = setTimeout(function () { t.hidden = true; }, 3200);
  }
  function setPalette(p) {
    document.documentElement.setAttribute("data-palette", p);
    try { localStorage.setItem("portal-palette", p); } catch (err) { /* private mode */ }
    document.querySelectorAll(".sw").forEach(function (b) { var on = b.dataset.p === p; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); });
  }
  document.addEventListener("click", function (ev) { var sw = ev.target.closest && ev.target.closest(".sw"); if (sw) setPalette(sw.dataset.p); });
  function tickClock() {
    var now = Date.now();
    U.$("#clock").textContent = U.hms(new Date(now));
    U.$("#next4").textContent = U.dur(4 * 3600e3 - (now % (4 * 3600e3)));
  }

  window.UI = { U: U, C: C, F: F, Net: Net, Store: Store, widget: W.widget, view: W.view, toast: toast };

  // start
  setPalette(document.documentElement.getAttribute("data-palette") || "sage");
  tickClock(); setInterval(tickClock, 1000);
  Store.use("coins", function () { drawStrip(); drawTape(); });
  Store.use("swing", drawStrip);
  Store.use("meta", drawStrip);
  Store.use("signals", watchSignals);
  route();
})();
