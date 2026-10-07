/* Portal chrome for the server-rendered tool pages (/predict, /moves, /chart, /audit, /settings,
   /keys, /journal, /v2, /pipeline, /api-docs, /classic#mirror|#simulator).
   Draws the same header as the portal from nav.js, so every page has the full navigation and none
   is a dead end with one "← back" link. Also forwards retired addresses to their new home.
   Spec: docs/PORTAL_REVAMP.md §3. */
(function () {
  "use strict";
  var Nav = window.PortalNav;
  if (!Nav) return;
  var path = location.pathname.replace(/\/+$/, "") || "/";
  var root = document.documentElement;
  root.setAttribute("data-theme", "brutal");
  try { root.setAttribute("data-palette", localStorage.getItem("portal-palette") || "sage"); } catch (e) { root.setAttribute("data-palette", "sage"); }

  // Retired addresses: the classic dashboard's tabs that the portal now renders natively.
  if (path === "/classic") {
    var loc0 = Nav.locate(path, location.hash);
    if (!location.hash || (loc0 && loc0.native)) { location.replace(loc0 && loc0.native ? Nav.href(loc0.hub, loc0.tab) : "/command"); return; }
  }

  function esc(v) { return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  var PALETTES = [["sage", "#A9D3B6", "#B9AAF2", "Sage & Lilac"], ["champagne", "#E3C58D", "#6CC9AE", "Champagne & Teal"], ["ice", "#94C5F5", "#EDA982", "Ice & Peach"], ["lime", "#C6F432", "#9B8CFF", "Classic lime"]];

  function draw() {
    var loc = Nav.locate(path, location.hash) || { hub: Nav.NAV[0], tab: null };
    var h = loc.hub, t = loc.tab;
    var bar = document.getElementById("pc-bar");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "pc-bar"; bar.className = "pc-bar"; bar.setAttribute("role", "banner");
      document.body.insertBefore(bar, document.body.firstChild);
    }
    var pal = document.documentElement.getAttribute("data-palette") || "sage";
    bar.innerHTML =
      '<div class="pc-row">' +
        '<a class="pc-logo" href="/command"><span class="pc-logo-a">SIG/</span><span class="pc-logo-b">' + esc(h.label.toUpperCase()) + "</span></a>" +
        '<nav class="pc-tabs" aria-label="Hubs">' + Nav.NAV.map(function (x, i) {
          return '<a class="pc-tab' + (x === h ? " on" : "") + '"' + (x === h ? ' aria-current="true"' : "") + ' href="' + Nav.href(x, x.tabs[0]) + '">0' + (i + 1) + " " + esc(x.label.toUpperCase()) + "</a>";
        }).join("") + "</nav>" +
        '<div class="pc-right"><span class="pc-mode" id="pc-mode">PAPER ONLY</span><div class="pc-sw" role="group" aria-label="Colour theme">' + PALETTES.map(function (p) {
          return '<button type="button" class="pc-swb' + (p[0] === pal ? " on" : "") + '" data-p="' + p[0] + '" title="' + esc(p[3]) + '" aria-label="' + esc(p[3]) + ' theme" aria-pressed="' + (p[0] === pal) + '"><i style="background:' + p[1] + '"></i><i style="background:' + p[2] + '"></i></button>';
        }).join("") + "</div></div>" +
      "</div>" +
      '<nav class="pc-subs" aria-label="Pages in ' + esc(h.label) + '">' + h.tabs.map(function (x) {
        var on = x === t;
        return '<a class="pc-sub' + (on ? " on" : "") + (x.native ? "" : " tool") + '"' + (on ? ' aria-current="page"' : "") + ' href="' + esc(Nav.href(h, x)) + '">' + esc(x.label) + "</a>";
      }).join("") + "</nav>";
  }

  function setPalette(p) {
    document.documentElement.setAttribute("data-palette", p);
    try { localStorage.setItem("portal-palette", p); } catch (e) { /* private mode */ }
    draw();
  }

  document.addEventListener("click", function (ev) {
    var b = ev.target.closest && ev.target.closest(".pc-swb");
    if (b) { setPalette(b.dataset.p); return; }
    // Two tools live on the classic page (#mirror, #simulator): switch in place instead of reloading.
    var a = ev.target.closest && ev.target.closest(".pc-bar a");
    if (a && typeof window.switchTab === "function") {
      var u = new URL(a.href, location.href);
      if (u.pathname === location.pathname && u.hash) { ev.preventDefault(); window.switchTab(u.hash.slice(1)); draw(); }
    }
  });
  window.addEventListener("hashchange", function () {
    if (path === "/classic") {
      var l = Nav.locate(path, location.hash);
      if (l && l.native) { location.replace(Nav.href(l.hub, l.tab)); return; }
    }
    draw();
  });

  function start() {
    document.documentElement.classList.add("pc-on");
    draw();
    fetch("/api/portal/meta", { credentials: "same-origin" }).then(function (r) { return r.ok ? r.json() : null; }).then(function (m) {
      var el = document.getElementById("pc-mode"); if (!el || !m) return;
      var live = m.live_trading_mode && m.live_trading_mode !== "off";
      el.textContent = live ? "LIVE: " + String(m.live_trading_mode).toUpperCase() : "PAPER ONLY";
      el.classList.toggle("live", !!live);
    }).catch(function () { /* the badge stays at its default */ });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
})();
