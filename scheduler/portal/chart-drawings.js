/* Drawing tools overlay (TradingView-style subset) for Chart Pro. */
window.ChartDrawings = (function () {
  const TOOLS = [
    { id: "cursor", label: "Select", icon: "↖" },
    { id: "trendline", label: "Trend line", icon: "╱" },
    { id: "hline", label: "Horizontal", icon: "─" },
    { id: "vline", label: "Vertical", icon: "│" },
    { id: "ray", label: "Ray", icon: "↗" },
    { id: "rect", label: "Rectangle", icon: "▭" },
    { id: "fib", label: "Fib retracement", icon: "Fib" },
    { id: "text", label: "Text", icon: "T" },
  ];

  let canvas, ctx, tool = "cursor", drawings = [], draft = null, api = null;
  let storageKey = "chart-drawings-default";

  function setApi(a) { api = a; }
  function setStorageKey(k) { storageKey = k; load(); }

  function load() {
    try {
      const raw = localStorage.getItem(storageKey);
      drawings = raw ? JSON.parse(raw) : [];
    } catch (e) { drawings = []; }
  }

  function save() {
    try { localStorage.setItem(storageKey, JSON.stringify(drawings)); } catch (e) { /* ignore */ }
  }

  function init(containerEl) {
    canvas = document.createElement("canvas");
    canvas.style.cssText = "position:absolute;inset:0;pointer-events:none;z-index:5";
    containerEl.style.position = "relative";
    containerEl.appendChild(canvas);
    ctx = canvas.getContext("2d");
    resize(containerEl);
    window.addEventListener("resize", () => resize(containerEl));

    canvas.addEventListener("mousedown", onDown);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    canvas.addEventListener("dblclick", onDbl);

    return {
      setTool(t) {
        tool = t;
        canvas.style.pointerEvents = t === "cursor" ? "none" : "auto";
        canvas.style.cursor = t === "cursor" ? "default" : "crosshair";
      },
      clear() { drawings = []; save(); redraw(); },
      redraw,
      tools: TOOLS,
    };
  }

  function resize(containerEl) {
    if (!canvas) return;
    const r = containerEl.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = r.width * dpr;
    canvas.height = r.height * dpr;
    canvas.style.width = r.width + "px";
    canvas.style.height = r.height + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    redraw();
  }

  function ptFromEvent(e) {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top, px: e.clientX, py: e.clientY };
  }

  function snapPoint(p) {
    if (!api?.chart || !api?.candleSeries) return p;
    const time = api.chart.timeScale().coordinateToTime(p.x);
    const price = api.candleSeries.coordinateToPrice(p.y);
    const x = time != null ? api.chart.timeScale().timeToCoordinate(time) : p.x;
    const y = price != null ? api.candleSeries.priceToCoordinate(price) : p.y;
    return { x, y, time, price };
  }

  function onDown(e) {
    if (tool === "cursor") return;
    const p = snapPoint(ptFromEvent(e));
    if (tool === "text") {
      const label = prompt("Label", "note") || "";
      if (label) { drawings.push({ type: "text", ...p, label }); save(); redraw(); }
      return;
    }
    draft = { type: tool, p1: p, p2: p };
  }

  function onMove(e) {
    if (!draft) return;
    draft.p2 = snapPoint(ptFromEvent(e));
    redraw();
  }

  function onUp() {
    if (!draft) return;
    if (draft.p1 && draft.p2 && (Math.abs(draft.p1.x - draft.p2.x) > 3 || Math.abs(draft.p1.y - draft.p2.y) > 3)) {
      drawings.push({ ...draft });
      save();
    }
    draft = null;
    redraw();
  }

  function onDbl() {
    if (tool !== "cursor") return;
    drawings.pop();
    save();
    redraw();
  }

  function drawLine(a, b, color, dash) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function redraw() {
    if (!ctx || !canvas) return;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    ctx.clearRect(0, 0, w, h);
    const all = draft ? [...drawings, draft] : drawings;
    all.forEach(d => {
      const col = "#38bdf8";
      if (d.type === "trendline" || d.type === "ray") {
        const end = d.type === "ray" ? { x: d.p2.x + (d.p2.x - d.p1.x) * 3, y: d.p2.y + (d.p2.y - d.p1.y) * 3 } : d.p2;
        drawLine(d.p1, end, col);
      } else if (d.type === "hline") {
        drawLine({ x: 0, y: d.p1.y }, { x: w, y: d.p1.y }, "#10b981", [6, 4]);
      } else if (d.type === "vline") {
        drawLine({ x: d.p1.x, y: 0 }, { x: d.p1.x, y: h }, "#f472b6", [6, 4]);
      } else if (d.type === "rect") {
        ctx.strokeStyle = col;
        ctx.strokeRect(d.p1.x, d.p1.y, d.p2.x - d.p1.x, d.p2.y - d.p1.y);
      } else if (d.type === "fib") {
        const top = Math.min(d.p1.y, d.p2.y);
        const bot = Math.max(d.p1.y, d.p2.y);
        const levels = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
        levels.forEach(lv => {
          const y = top + (bot - top) * lv;
          drawLine({ x: Math.min(d.p1.x, d.p2.x), y }, { x: Math.max(d.p1.x, d.p2.x), y }, "#94a3b8", [4, 3]);
          ctx.fillStyle = "#64748b";
          ctx.font = "10px sans-serif";
          ctx.fillText((lv * 100).toFixed(1) + "%", Math.max(d.p1.x, d.p2.x) + 4, y + 3);
        });
      } else if (d.type === "text") {
        ctx.fillStyle = "#f1f5f9";
        ctx.font = "11px sans-serif";
        ctx.fillText(d.label || "", d.x, d.y);
      }
    });
  }

  return { init, setApi, setStorageKey, TOOLS };
})();
