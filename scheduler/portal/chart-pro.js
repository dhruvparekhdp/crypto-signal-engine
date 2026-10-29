/* Chart Pro orchestrator — strategies, indicator catalog, drawings */
(function () {
  const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BCHUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "ADAUSDT"];
  const TFS = ["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"];
  const LS_IND = "chart-pro-active-indicators-v1";
  const DEFAULT_INDS = ["ema20", "ema50", "bb_20"];

  let chart, candleSeries, volSeries;
  let klines = [], historySignals = [], scanMarkers = [], strategies = [];
  let activeInd = new Set();
  let seriesByKey = {};
  let oscCharts = {};
  let drawCtl = null;

  const state = {
    symbol: "BTCUSDT", tf: "1h", strategy: "all", minConf: 0.6,
    dirFilter: "all", showHistory: true, showScan: true, showTpSl: true,
  };

  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[<>&"]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c]));
  const Lib = () => window.ChartIndicatorLib;

  function loadActiveInd() {
    try {
      const raw = localStorage.getItem(LS_IND);
      activeInd = new Set(raw ? JSON.parse(raw) : DEFAULT_INDS);
    } catch (e) { activeInd = new Set(DEFAULT_INDS); }
  }

  function saveActiveInd() {
    localStorage.setItem(LS_IND, JSON.stringify([...activeInd]));
  }

  function toLwcBars(bars) {
    return bars.map(b => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close }));
  }

  function buildMarkerList() {
    const out = [];
    const add = (m, prefix) => {
      const isLong = m.direction === "LONG" || m.direction === "long";
      if (state.dirFilter === "long" && !isLong) return;
      if (state.dirFilter === "short" && isLong) return;
      const conf = m.confidence != null ? m.confidence : m.conf;
      if (conf != null && conf < state.minConf) return;
      const strat = (m.strategy || m.signal_type || "signal").slice(0, 12);
      out.push({
        time: m.time || Math.floor(new Date(m.entry_time || m.timestamp).getTime() / 1000),
        position: isLong ? "belowBar" : "aboveBar",
        color: isLong ? "#34d399" : "#fb7185",
        shape: isLong ? "arrowUp" : "arrowDown",
        text: `${prefix}${strat}${conf != null ? " " + (conf * 100).toFixed(0) + "%" : ""}`,
        meta: m,
      });
    };
    if (state.showHistory) historySignals.forEach(m => add(m, "H:"));
    if (state.showScan) scanMarkers.forEach(m => add(m, "S:"));
    return out.sort((a, b) => a.time - b.time);
  }

  function applyPriceLines() {
    if (!candleSeries?.applyOptions) return;
    candleSeries.applyOptions({ priceLines: [] });
    if (!state.showTpSl) return;
    const markers = buildMarkerList();
    const last = markers[markers.length - 1];
    if (!last?.meta?.tp) return;
    [{ price: last.meta.tp, color: "#34d399", title: "TP" }, { price: last.meta.sl, color: "#fb7185", title: "SL" }].forEach(pl => {
      candleSeries.createPriceLine({ ...pl, lineWidth: 1, lineStyle: 2 });
    });
  }

  function ensureOscPane(paneId) {
    if (oscCharts[paneId]) return oscCharts[paneId];
    const stack = $("chart-stack");
    const el = document.createElement("div");
    el.id = "pane-" + paneId;
    el.className = "sub-pane";
    stack.appendChild(el);
    const oc = LightweightCharts.createChart(el, {
      layout: { background: { color: "#080d18" }, textColor: "#64748b" },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      width: el.clientWidth,
      height: 100,
      rightPriceScale: { borderVisible: false },
      timeScale: { visible: false },
    });
    oscCharts[paneId] = { el, chart: oc, series: {} };
    chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (range) oc.timeScale().setVisibleLogicalRange(range);
    });
    return oscCharts[paneId];
  }

  function hideUnusedOscPanes(needed) {
    Object.keys(oscCharts).forEach(pid => {
      const visible = needed.has(pid);
      oscCharts[pid].el.style.display = visible ? "block" : "none";
      if (!visible) Object.values(oscCharts[pid].series).forEach(s => s.setData([]));
    });
  }

  function getOrCreateSeries(indId, outKey, outMeta, pane) {
    const key = indId + "::" + outKey;
    if (seriesByKey[key]) return seriesByKey[key];

    let series;
    if (pane === "main") {
      if (outMeta.type === "hist") {
        series = chart.addHistogramSeries({ priceFormat: { type: "price", precision: 4 }, priceLineVisible: false, lastValueVisible: false });
      } else {
        series = chart.addLineSeries({
          color: outMeta.color, lineWidth: outMeta.width || 1, priceLineVisible: false, lastValueVisible: false,
          lineStyle: outMeta.dash ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
        });
      }
    } else {
      const pid = pane === "osc_macd" ? "osc_macd" : "osc";
      const pan = ensureOscPane(pid);
      if (outMeta.type === "hist") {
        series = pan.chart.addHistogramSeries({ priceFormat: { type: "price", precision: 4 } });
      } else {
        series = pan.chart.addLineSeries({ color: outMeta.color, lineWidth: outMeta.width || 1 });
      }
      pan.series[key] = series;
    }
    seriesByKey[key] = series;
    return series;
  }

  function seriesData(values) {
    return klines.map((b, i) => ({ time: b.time, value: values[i] }))
      .filter(d => d.value != null && !Number.isNaN(d.value));
  }

  function refreshIndicators() {
    if (!klines.length || !candleSeries) return;
    const lib = Lib();
    if (!lib) return;

    candleSeries.setData(toLwcBars(klines));
    volSeries.setData(klines.map(b => ({
      time: b.time, value: b.volume,
      color: b.close >= b.open ? "rgba(16,185,129,0.45)" : "rgba(244,63,94,0.45)",
    })));

    const neededOsc = new Set();
    const usedKeys = new Set();

    activeInd.forEach(indId => {
      const ind = lib.BY_ID[indId];
      if (!ind) return;
      const computed = lib.compute(indId, klines);
      if (!computed) return;
      if (ind.pane !== "main") neededOsc.add(ind.pane === "osc_macd" ? "osc_macd" : "osc");

      ind.outputs.forEach(out => {
        const vals = computed[out.key];
        if (!vals) return;
        const key = indId + "::" + out.key;
        usedKeys.add(key);
        const series = getOrCreateSeries(indId, out.key, out, ind.pane);
        if (out.type === "hist") {
          series.setData(klines.map((b, i) => ({
            time: b.time, value: vals[i],
            color: (vals[i] || 0) >= 0 ? "#10b981" : "#f43f5e",
          })).filter(d => d.value != null));
        } else {
          series.setData(seriesData(vals));
        }
      });
    });

    Object.entries(seriesByKey).forEach(([key, series]) => {
      if (!usedKeys.has(key)) series.setData([]);
    });
    hideUnusedOscPanes(neededOsc);

    candleSeries.setMarkers(buildMarkerList());
    applyPriceLines();
    renderActiveChips();
    updateLegend();
    drawCtl?.redraw();
  }

  function renderActiveChips() {
    const el = $("active-indicators");
    if (!el) return;
    const lib = Lib();
    el.innerHTML = [...activeInd].map(id => {
      const name = lib?.BY_ID[id]?.name || id;
      return `<span class="ind-chip">${esc(name)} <button type="button" data-rm="${esc(id)}">×</button></span>`;
    }).join("");
    el.querySelectorAll("button[data-rm]").forEach(btn => {
      btn.onclick = () => { activeInd.delete(btn.getAttribute("data-rm")); saveActiveInd(); refreshIndicators(); };
    });
  }

  function updateLegend() {
    const el = $("chart-legend");
    if (!el || !klines.length) return;
    const last = klines[klines.length - 1];
    const chg = ((last.close - last.open) / last.open) * 100;
    const lib = Lib();
    el.innerHTML = `<strong style="color:#38bdf8">${esc(state.symbol)}</strong> · ${esc(state.tf)} · ` +
      `${(lib?.count() || 0)} indicators in catalog · <strong>${activeInd.size} active</strong> · ` +
      `<span style="color:${chg >= 0 ? "#10b981" : "#f43f5e"}">${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</span>`;
  }

  function openIndicatorModal() {
    const lib = Lib();
    if (!lib) return;
    const modal = $("ind-modal");
    const list = $("ind-list");
    const q = ($("ind-search")?.value || "").toLowerCase();
    const cat = $("ind-cat")?.value || "all";
    list.innerHTML = lib.REGISTRY.filter(ind => {
      if (cat !== "all" && ind.category !== cat) return false;
      if (q && !ind.name.toLowerCase().includes(q) && !ind.id.includes(q)) return false;
      return true;
    }).map(ind => {
      const on = activeInd.has(ind.id);
      const badge = ind.tv === "partial" ? '<span class="tv-partial">partial</span>' : "";
      return `<label class="ind-row"><input type="checkbox" data-id="${esc(ind.id)}" ${on ? "checked" : ""}> ` +
        `<span>${esc(ind.name)}</span> <span class="ind-cat">${esc(ind.category)}</span>${badge}</label>`;
    }).join("");
    list.querySelectorAll("input[data-id]").forEach(inp => {
      inp.onchange = () => {
        const id = inp.getAttribute("data-id");
        if (inp.checked) activeInd.add(id); else activeInd.delete(id);
        saveActiveInd();
        refreshIndicators();
      };
    });
    modal?.classList.add("open");
  }

  async function loadStrategies() {
    const res = await fetch("/api/chart/strategies");
    strategies = await res.json();
    const sel = $("sel-strategy");
    if (sel) sel.innerHTML = strategies.map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
  }

  async function loadKlines() {
    const res = await fetch(`/api/chart/klines?symbol=${encodeURIComponent(state.symbol)}&tf=${encodeURIComponent(state.tf)}&limit=800`);
    klines = await res.json();
    if (!Array.isArray(klines)) klines = [];
  }

  async function loadHistorySignals() {
    const q = new URLSearchParams({ symbol: state.symbol, days: "60", strategy: state.strategy === "all" ? "" : state.strategy });
    const data = await (await fetch(`/api/chart/history-signals?${q}`)).json();
    historySignals = data.signals || [];
  }

  async function runStrategyScan() {
    $("scan-status").textContent = "Scanning…";
    const q = new URLSearchParams({ symbol: state.symbol, tf: state.tf, strategy: state.strategy, min_confidence: String(state.minConf), limit: "800" });
    const data = await (await fetch(`/api/chart/strategy-scan?${q}`)).json();
    scanMarkers = data.markers || [];
    $("scan-status").textContent = `${scanMarkers.length} markers · ${data.elapsed_ms || 0}ms`;
    refreshIndicators();
  }

  async function reloadAll() {
    $("load-status").textContent = "Loading…";
    window.ChartDrawings?.setStorageKey(`chart-draw-${state.symbol}-${state.tf}`);
    await Promise.all([loadKlines(), loadHistorySignals()]);
    await runStrategyScan();
    refreshIndicators();
    $("load-status").textContent = `${klines.length} bars`;
  }

  function initChart() {
    const container = $("tv-chart-root");
    const stack = $("chart-stack");
    if (!container || !stack) return;

    chart = LightweightCharts.createChart(container, {
      layout: { background: { color: "#0b1120" }, textColor: "#94a3b8" },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#334155" },
      timeScale: { borderColor: "#334155", timeVisible: true, secondsVisible: false },
      width: container.clientWidth,
      height: container.clientHeight,
    });
    candleSeries = chart.addCandlestickSeries({
      upColor: "#10b981", downColor: "#f43f5e", borderVisible: false, wickUpColor: "#10b981", wickDownColor: "#f43f5e",
    });
    volSeries = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol" });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    drawCtl = window.ChartDrawings?.init(stack);
    window.ChartDrawings?.setApi({ chart, candleSeries });
    window.ChartDrawings?.setStorageKey(`chart-draw-${state.symbol}-${state.tf}`);

    const drawBar = $("draw-tools");
    if (drawBar && drawCtl) {
      drawBar.innerHTML = window.ChartDrawings.TOOLS.map(t =>
        `<button type="button" class="pill-btn draw-tool" data-tool="${t.id}" title="${esc(t.label)}">${t.icon}</button>`).join("");
      drawBar.querySelectorAll(".draw-tool").forEach(btn => {
        btn.onclick = () => {
          drawBar.querySelectorAll(".draw-tool").forEach(b => b.classList.remove("active"));
          btn.classList.add("active");
          drawCtl.setTool(btn.getAttribute("data-tool"));
        };
      });
    }

    window.addEventListener("resize", () => {
      chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
      Object.values(oscCharts).forEach(p => p.chart.applyOptions({ width: p.el.clientWidth }));
    });
  }

  function bindUI() {
    SYMBOLS.forEach(sym => {
      const btn = document.createElement("button");
      btn.className = "pill-btn" + (sym === state.symbol ? " active" : "");
      btn.textContent = sym.replace("USDT", "");
      btn.onclick = () => { state.symbol = sym; document.querySelectorAll("#sym-row .pill-btn").forEach(b => b.classList.remove("active")); btn.classList.add("active"); reloadAll(); };
      $("sym-row")?.appendChild(btn);
    });
    TFS.forEach(tf => {
      const btn = document.createElement("button");
      btn.className = "pill-btn" + (tf === state.tf ? " active" : "");
      btn.textContent = tf;
      btn.onclick = () => { state.tf = tf; document.querySelectorAll("#tf-row .pill-btn").forEach(b => b.classList.remove("active")); btn.classList.add("active"); reloadAll(); };
      $("tf-row")?.appendChild(btn);
    });

    const lib = Lib();
    const catSel = $("ind-cat");
    if (catSel && lib) {
      catSel.innerHTML = `<option value="all">All categories</option>` +
        lib.CATEGORIES.map(c => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
    }

    $("btn-indicators")?.addEventListener("click", openIndicatorModal);
    $("ind-search")?.addEventListener("input", openIndicatorModal);
    $("ind-cat")?.addEventListener("change", openIndicatorModal);
    $("ind-modal-close")?.addEventListener("click", () => $("ind-modal")?.classList.remove("open"));
    $("ind-modal")?.addEventListener("click", e => { if (e.target.id === "ind-modal") e.currentTarget.classList.remove("open"); });

    $("sel-strategy")?.addEventListener("change", e => { state.strategy = e.target.value; reloadAll(); });
    $("sel-dir")?.addEventListener("change", e => { state.dirFilter = e.target.value; refreshIndicators(); });
    $("rng-conf")?.addEventListener("input", e => { state.minConf = parseFloat(e.target.value); $("lbl-conf").textContent = (state.minConf * 100).toFixed(0) + "%"; refreshIndicators(); });
    ["ov-history", "ov-scan", "ov-tpsl"].forEach(id => {
      $(id)?.addEventListener("change", e => {
        if (id === "ov-history") state.showHistory = e.target.checked;
        if (id === "ov-scan") state.showScan = e.target.checked;
        if (id === "ov-tpsl") state.showTpSl = e.target.checked;
        refreshIndicators();
      });
    });
    $("btn-rescan")?.addEventListener("click", runStrategyScan);
    $("btn-fit")?.addEventListener("click", () => chart?.timeScale().fitContent());
    $("btn-log")?.addEventListener("click", () => {
      const ps = chart.priceScale("right");
      const on = ps.options().mode !== LightweightCharts.PriceScaleMode.Logarithmic;
      ps.applyOptions({ mode: on ? LightweightCharts.PriceScaleMode.Logarithmic : LightweightCharts.PriceScaleMode.Normal });
    });
    $("btn-clear-draw")?.addEventListener("click", () => drawCtl?.clear());
  }

  async function boot() {
    loadActiveInd();
    bindUI();
    await loadStrategies();
    initChart();
    await reloadAll();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
