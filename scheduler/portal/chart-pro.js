/* Institutional chart — Lightweight Charts + engine strategies (chart-pro.js) */
(function () {
  const SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BCHUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "ADAUSDT"];
  const TFS = ["1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d"];

  let chart, candleSeries, volSeries;
  let emaLines = {};
  let bbUpper, bbLower, vwapLine;
  let rsiChart, rsiSeries, macdChart, macdLine, macdSignal, macdHist;
  let klines = [];
  let historySignals = [];
  let scanMarkers = [];
  let strategies = [];

  const state = {
    symbol: "BTCUSDT",
    tf: "1h",
    strategy: "all",
    minConf: 0.6,
    dirFilter: "all",
    showHistory: true,
    showScan: true,
    showTpSl: true,
  };

  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s ?? "").replace(/[<>&"]/g, c => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c])); }

  function indicatorFlags() {
    const ids = ["ind-ema20", "ind-ema50", "ind-ema200", "ind-bb", "ind-vwap", "ind-vol", "ind-rsi", "ind-macd"];
    const o = {};
    ids.forEach(id => { o[id] = $(id)?.checked ?? false; });
    return o;
  }

  function calcEMA(values, period) {
    const k = 2 / (period + 1);
    const out = [];
    let prev = values[0] || 0;
    for (let i = 0; i < values.length; i++) {
      prev = values[i] * k + prev * (1 - k);
      out.push(prev);
    }
    return out;
  }

  function calcBB(closes, period, mult) {
    const upper = [], lower = [], mid = [];
    for (let i = 0; i < closes.length; i++) {
      if (i < period - 1) { upper.push(null); lower.push(null); mid.push(null); continue; }
      const slice = closes.slice(i - period + 1, i + 1);
      const mean = slice.reduce((a, b) => a + b, 0) / period;
      const variance = slice.reduce((a, b) => a + (b - mean) ** 2, 0) / period;
      const std = Math.sqrt(variance);
      mid.push(mean);
      upper.push(mean + mult * std);
      lower.push(mean - mult * std);
    }
    return { upper, lower, mid };
  }

  function calcVWAP(bars) {
    let cumPV = 0, cumV = 0;
    return bars.map(b => {
      const tp = (b.high + b.low + b.close) / 3;
      cumPV += tp * b.volume;
      cumV += b.volume;
      return cumV > 0 ? cumPV / cumV : b.close;
    });
  }

  function calcRSI(closes, period) {
    const rsi = [];
    let avgGain = 0, avgLoss = 0;
    for (let i = 0; i < closes.length; i++) {
      if (i === 0) { rsi.push(null); continue; }
      const ch = closes[i] - closes[i - 1];
      const gain = Math.max(0, ch);
      const loss = Math.max(0, -ch);
      if (i <= period) {
        avgGain += gain;
        avgLoss += loss;
        rsi.push(null);
        if (i === period) {
          avgGain /= period;
          avgLoss /= period;
          const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
          rsi[i] = 100 - 100 / (1 + rs);
        }
        continue;
      }
      avgGain = (avgGain * (period - 1) + gain) / period;
      avgLoss = (avgLoss * (period - 1) + loss) / period;
      const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
      rsi.push(100 - 100 / (1 + rs));
    }
    return rsi;
  }

  function calcMACD(closes) {
    const ema12 = calcEMA(closes, 12);
    const ema26 = calcEMA(closes, 26);
    const macd = ema12.map((v, i) => v - ema26[i]);
    const signal = calcEMA(macd.map(v => v ?? 0), 9);
    const hist = macd.map((v, i) => (v != null && signal[i] != null) ? v - signal[i] : null);
    return { macd, signal, hist };
  }

  function toLwcBars(bars) {
    return bars.map(b => ({
      time: b.time,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }));
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
        color: isLong ? "#10b981" : "#f43f5e",
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
    candleSeries.applyOptions({ priceLines: [] });
    if (!state.showTpSl) return;
    const markers = buildMarkerList();
    const last = markers[markers.length - 1];
    if (!last?.meta?.tp) return;
    const lines = [
      { price: last.meta.tp, color: "#34d399", lineWidth: 1, lineStyle: 2, title: "TP" },
      { price: last.meta.sl, color: "#fb7185", lineWidth: 1, lineStyle: 2, title: "SL" },
    ];
    lines.forEach(pl => candleSeries.createPriceLine(pl));
  }

  function refreshIndicators() {
    if (!klines.length || !candleSeries) return;
    const flags = indicatorFlags();
    const closes = klines.map(b => b.close);
    const lwc = toLwcBars(klines);

    candleSeries.setData(lwc);
    volSeries.setData(klines.map(b => ({
      time: b.time,
      value: b.volume,
      color: b.close >= b.open ? "rgba(16,185,129,0.45)" : "rgba(244,63,94,0.45)",
    })));

    const setLine = (key, series, data, visible) => {
      if (!series) return;
      if (!visible) { series.setData([]); return; }
      series.setData(klines.map((b, i) => ({ time: b.time, value: data[i] })).filter(d => d.value != null && !Number.isNaN(d.value)));
    };

    if (flags["ind-ema20"]) setLine("20", emaLines[20], calcEMA(closes, 20), true);
    else emaLines[20]?.setData([]);
    if (flags["ind-ema50"]) setLine("50", emaLines[50], calcEMA(closes, 50), true);
    else emaLines[50]?.setData([]);
    if (flags["ind-ema200"]) setLine("200", emaLines[200], calcEMA(closes, 200), true);
    else emaLines[200]?.setData([]);

    if (flags["ind-bb"]) {
      const bb = calcBB(closes, 20, 2);
      setLine("bbu", bbUpper, bb.upper, true);
      setLine("bbl", bbLower, bb.lower, true);
    } else {
      bbUpper?.setData([]);
      bbLower?.setData([]);
    }

    if (flags["ind-vwap"]) setLine("vwap", vwapLine, calcVWAP(klines), true);
    else vwapLine?.setData([]);

    $("pane-rsi").style.display = flags["ind-rsi"] ? "block" : "none";
    $("pane-macd").style.display = flags["ind-macd"] ? "block" : "none";
    if (flags["ind-rsi"]) {
      const rsi = calcRSI(closes, 14);
      rsiSeries.setData(klines.map((b, i) => ({ time: b.time, value: rsi[i] })).filter(d => d.value != null));
    }
    if (flags["ind-macd"]) {
      const { macd, signal, hist } = calcMACD(closes);
      macdLine.setData(klines.map((b, i) => ({ time: b.time, value: macd[i] })).filter(d => d.value != null));
      macdSignal.setData(klines.map((b, i) => ({ time: b.time, value: signal[i] })).filter(d => d.value != null));
      macdHist.setData(klines.map((b, i) => ({ time: b.time, value: hist[i], color: (hist[i] || 0) >= 0 ? "#10b981" : "#f43f5e" })).filter(d => d.value != null));
    }

    candleSeries.setMarkers(buildMarkerList());
    applyPriceLines();
    chart.timeScale().fitContent();
    updateLegend();
  }

  function updateLegend() {
    const el = $("chart-legend");
    if (!el || !klines.length) return;
    const last = klines[klines.length - 1];
    const chg = ((last.close - last.open) / last.open) * 100;
    el.innerHTML = `<strong style="color:#38bdf8">${esc(state.symbol)}</strong> · ${esc(state.tf)} · ` +
      `O ${last.open.toFixed(2)} H ${last.high.toFixed(2)} L ${last.low.toFixed(2)} C ${last.close.toFixed(2)} · ` +
      `<span style="color:${chg >= 0 ? "#10b981" : "#f43f5e"}">${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</span> · ` +
      `${historySignals.length} hist · ${scanMarkers.length} scanned`;
  }

  async function loadStrategies() {
    try {
      const res = await fetch("/api/chart/strategies");
      strategies = await res.json();
      const sel = $("sel-strategy");
      if (!sel) return;
      sel.innerHTML = strategies.map(s => `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join("");
      sel.value = state.strategy;
    } catch (e) { console.warn("strategies", e); }
  }

  async function loadKlines() {
    const limit = 800;
    const res = await fetch(`/api/chart/klines?symbol=${encodeURIComponent(state.symbol)}&tf=${encodeURIComponent(state.tf)}&limit=${limit}`);
    klines = await res.json();
    if (!Array.isArray(klines)) klines = [];
  }

  async function loadHistorySignals() {
    const q = new URLSearchParams({
      symbol: state.symbol,
      days: "60",
      strategy: state.strategy === "all" ? "" : state.strategy,
    });
    const res = await fetch(`/api/chart/history-signals?${q}`);
    const data = await res.json();
    historySignals = data.signals || [];
  }

  async function runStrategyScan() {
    $("scan-status").textContent = "Scanning…";
    const q = new URLSearchParams({
      symbol: state.symbol,
      tf: state.tf,
      strategy: state.strategy,
      min_confidence: String(state.minConf),
      limit: "800",
    });
    const res = await fetch(`/api/chart/strategy-scan?${q}`);
    const data = await res.json();
    scanMarkers = data.markers || [];
    $("scan-status").textContent = `${scanMarkers.length} markers · ${data.elapsed_ms || 0}ms`;
    refreshIndicators();
  }

  async function reloadAll() {
    $("load-status").textContent = "Loading…";
    await Promise.all([loadKlines(), loadHistorySignals()]);
    await runStrategyScan();
    refreshIndicators();
    $("load-status").textContent = `${klines.length} bars`;
  }

  function initChart() {
    const container = $("tv-chart-root");
    if (!container || typeof LightweightCharts === "undefined") return;

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
      upColor: "#10b981", downColor: "#f43f5e", borderVisible: false,
      wickUpColor: "#10b981", wickDownColor: "#f43f5e",
    });
    volSeries = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    const line = (color, width) => chart.addLineSeries({ color, lineWidth: width, priceLineVisible: false, lastValueVisible: false });
    emaLines[20] = line("#38bdf8", 1);
    emaLines[50] = line("#fb923c", 1);
    emaLines[200] = line("#c084fc", 1);
    bbUpper = line("rgba(148,163,184,0.5)", 1);
    bbLower = line("rgba(148,163,184,0.5)", 1);
    vwapLine = line("#eab308", 1);

    const rsiEl = $("pane-rsi");
    rsiChart = LightweightCharts.createChart(rsiEl, {
      layout: { background: { color: "#080d18" }, textColor: "#64748b" },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      width: rsiEl.clientWidth,
      height: 100,
      rightPriceScale: { borderVisible: false },
      timeScale: { visible: false },
    });
    rsiSeries = rsiChart.addLineSeries({ color: "#a78bfa", lineWidth: 1.5 });

    const macdEl = $("pane-macd");
    macdChart = LightweightCharts.createChart(macdEl, {
      layout: { background: { color: "#080d18" }, textColor: "#64748b" },
      grid: { vertLines: { color: "#1e293b" }, horzLines: { color: "#1e293b" } },
      width: macdEl.clientWidth,
      height: 100,
      rightPriceScale: { borderVisible: false },
      timeScale: { visible: false },
    });
    macdLine = macdChart.addLineSeries({ color: "#38bdf8", lineWidth: 1 });
    macdSignal = macdChart.addLineSeries({ color: "#fb923c", lineWidth: 1 });
    macdHist = macdChart.addHistogramSeries({ priceFormat: { type: "price", precision: 4 } });

    chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (!range) return;
      rsiChart.timeScale().setVisibleLogicalRange(range);
      macdChart.timeScale().setVisibleLogicalRange(range);
    });

    window.addEventListener("resize", () => {
      chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
      rsiChart.applyOptions({ width: rsiEl.clientWidth });
      macdChart.applyOptions({ width: macdEl.clientWidth });
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

    $("sel-strategy")?.addEventListener("change", e => { state.strategy = e.target.value; reloadAll(); });
    $("sel-dir")?.addEventListener("change", e => { state.dirFilter = e.target.value; refreshIndicators(); });
    $("rng-conf")?.addEventListener("input", e => {
      state.minConf = parseFloat(e.target.value);
      $("lbl-conf").textContent = (state.minConf * 100).toFixed(0) + "%";
      refreshIndicators();
    });
    ["ov-history", "ov-scan", "ov-tpsl"].forEach(id => {
      $(id)?.addEventListener("change", e => {
        if (id === "ov-history") state.showHistory = e.target.checked;
        if (id === "ov-scan") state.showScan = e.target.checked;
        if (id === "ov-tpsl") state.showTpSl = e.target.checked;
        refreshIndicators();
      });
    });
    document.querySelectorAll(".ind-toggle").forEach(el => el.addEventListener("change", refreshIndicators));
    $("btn-rescan")?.addEventListener("click", runStrategyScan);
    $("btn-fit")?.addEventListener("click", () => chart?.timeScale().fitContent());
    $("btn-log")?.addEventListener("click", () => {
      const on = chart.priceScale("right").options().mode !== LightweightCharts.PriceScaleMode.Logarithmic;
      chart.priceScale("right").applyOptions({ mode: on ? LightweightCharts.PriceScaleMode.Logarithmic : LightweightCharts.PriceScaleMode.Normal });
    });
  }

  async function boot() {
    bindUI();
    await loadStrategies();
    initChart();
    await reloadAll();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
