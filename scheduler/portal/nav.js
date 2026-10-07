/* The one navigation map for every page: the portal shell (core.js) and every server-rendered
   tool page (chrome.js) both draw their header from this. Spec: docs/PORTAL_REVAMP.md §3.

   A tab is either native (rendered by the portal at /<hub>/<tab>) or a tool (its own page at `url`,
   shown under the same header). `aliases` are older addresses that now belong to the tab: the
   classic dashboard's #anchors and retired routes. Native aliases are redirected; tool aliases
   just highlight the right tab. */
(function () {
  "use strict";
  var NAV = [
    { id: "command", label: "Command", tabs: [
      { id: "overview", label: "Overview", native: true },
      { id: "signals", label: "Signals", native: true, aliases: ["/classic#dashboard", "/classic#crypto"] },
      { id: "watchlist", label: "Watchlist", native: true, aliases: ["/classic#watchlist"] },
      { id: "outlook", label: "Price outlook", url: "/predict" },
      { id: "moves", label: "Market moves", url: "/moves" },
      { id: "chart", label: "Chart", url: "/chart" }
    ] },
    { id: "book", label: "Book", tabs: [
      { id: "swing", label: "Swing book", native: true },
      { id: "paper", label: "Paper cycle", native: true, aliases: ["/classic#paper"] },
      { id: "guard", label: "Session guard", native: true, aliases: ["/classic#guard"] },
      { id: "simulator", label: "Simulator", url: "/classic#simulator" },
      { id: "journal", label: "Journal", url: "/journal" }
    ] },
    { id: "evidence", label: "Evidence", tabs: [
      { id: "strategies", label: "Strategies", native: true },
      { id: "accuracy", label: "Accuracy", native: true, aliases: ["/classic#accuracy"] },
      { id: "history", label: "History", native: true, aliases: ["/classic#historic"] },
      { id: "research", label: "Research", native: true },
      { id: "audit", label: "Audit", url: "/audit" },
      { id: "mirror", label: "Mirror", url: "/classic#mirror" },
      { id: "pipeline", label: "Pipeline", url: "/pipeline" },
      { id: "v2", label: "v2 shadow", url: "/v2" }
    ] },
    { id: "system", label: "System", tabs: [
      { id: "health", label: "Health", native: true },
      { id: "data", label: "Data", native: true, aliases: ["/data"] },
      { id: "diagnostics", label: "Diagnostics", native: true },
      { id: "settings", label: "Settings", url: "/settings", aliases: ["/settings/classic"] },
      { id: "keys", label: "Keys", url: "/keys" },
      { id: "api", label: "API list", url: "/api-docs" }
    ] }
  ];

  function hub(id) { for (var i = 0; i < NAV.length; i++) if (NAV[i].id === id) return NAV[i]; return null; }
  function tab(h, id) { if (!h) return null; for (var i = 0; i < h.tabs.length; i++) if (h.tabs[i].id === id) return h.tabs[i]; return null; }
  function href(h, t) {
    if (t.url) return t.url;
    return t === h.tabs[0] ? "/" + h.id : "/" + h.id + "/" + t.id;
  }
  // Which hub/tab an address belongs to: native paths, tool urls, then aliases.
  function locate(path, hash) {
    path = (path || "/").replace(/\/+$/, "") || "/";
    var full = path + (hash || "");
    var parts = path.split("/").filter(Boolean);
    var h = hub(parts[0]);
    if (h) {
      var t = parts[1] ? tab(h, parts[1]) : h.tabs[0];
      return { hub: h, tab: t || h.tabs[0], native: !!(t || h.tabs[0]).native, known: !!t || !parts[1] };
    }
    for (var i = 0; i < NAV.length; i++) {
      for (var j = 0; j < NAV[i].tabs.length; j++) {
        var x = NAV[i].tabs[j];
        if (x.url && (x.url === full || (x.url === path && x.url.indexOf("#") < 0))) return { hub: NAV[i], tab: x, native: false, known: true };
        if ((x.aliases || []).indexOf(full) >= 0 || (x.aliases || []).indexOf(path) >= 0) return { hub: NAV[i], tab: x, native: !!x.native, known: true, alias: true };
      }
    }
    return null;
  }

  window.PortalNav = { NAV: NAV, hub: hub, tab: tab, href: href, locate: locate };
})();
