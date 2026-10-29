/* Indicator library for Chart Pro — TradingView-style catalog (client-side). */
window.ChartIndicatorLib = (function () {
  const closes = b => b.map(x => x.close);
  const highs = b => b.map(x => x.high);
  const lows = b => b.map(x => x.low);
  const vols = b => b.map(x => x.volume || 0);

  function sma(vals, period) {
    const out = new Array(vals.length).fill(null);
    for (let i = period - 1; i < vals.length; i++) {
      let s = 0;
      for (let j = i - period + 1; j <= i; j++) s += vals[j];
      out[i] = s / period;
    }
    return out;
  }

  function ema(vals, period) {
    const k = 2 / (period + 1);
    const out = [];
    let prev = vals[0];
    for (let i = 0; i < vals.length; i++) {
      prev = i === 0 ? vals[0] : vals[i] * k + prev * (1 - k);
      out.push(prev);
    }
    return out;
  }

  function wma(vals, period) {
    const out = new Array(vals.length).fill(null);
    const denom = (period * (period + 1)) / 2;
    for (let i = period - 1; i < vals.length; i++) {
      let s = 0, w = 1;
      for (let j = i - period + 1; j <= i; j++, w++) s += vals[j] * w;
      out[i] = s / denom;
    }
    return out;
  }

  function dema(vals, period) {
    const e1 = ema(vals, period);
    const e2 = ema(e1, period);
    return e1.map((v, i) => 2 * v - e2[i]);
  }

  function tema(vals, period) {
    const e1 = ema(vals, period);
    const e2 = ema(e1, period);
    const e3 = ema(e2, period);
    return e1.map((v, i) => 3 * v - 3 * e2[i] + e3[i]);
  }

  function hma(vals, period) {
    const half = Math.max(1, Math.floor(period / 2));
    const sqrt = Math.max(1, Math.floor(Math.sqrt(period)));
    const w1 = wma(vals, half);
    const w2 = wma(vals, period);
    const diff = w1.map((v, i) => (v != null && w2[i] != null) ? 2 * v - w2[i] : null);
    return wma(diff.map(v => v ?? 0), sqrt);
  }

  function smma(vals, period) {
    const out = [];
    let prev = null;
    for (let i = 0; i < vals.length; i++) {
      if (i < period) { out.push(null); continue; }
      if (prev == null) {
        let s = 0;
        for (let j = 0; j < period; j++) s += vals[j];
        prev = s / period;
      } else prev = (prev * (period - 1) + vals[i]) / period;
      out.push(prev);
    }
    return out;
  }

  function vwma(bars, period) {
    const out = new Array(bars.length).fill(null);
    for (let i = period - 1; i < bars.length; i++) {
      let pv = 0, v = 0;
      for (let j = i - period + 1; j <= i; j++) {
        pv += bars[j].close * (bars[j].volume || 0);
        v += bars[j].volume || 0;
      }
      out[i] = v > 0 ? pv / v : bars[i].close;
    }
    return out;
  }

  function trueRange(bars) {
    const tr = [bars[0].high - bars[0].low];
    for (let i = 1; i < bars.length; i++) {
      const h = bars[i].high, l = bars[i].low, pc = bars[i - 1].close;
      tr.push(Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc)));
    }
    return tr;
  }

  function atr(bars, period) {
    const tr = trueRange(bars);
    return smma(tr, period).map((v, i) => (i < period ? null : v));
  }

  function bollinger(vals, period, mult) {
    const mid = sma(vals, period);
    const upper = [], lower = [];
    for (let i = 0; i < vals.length; i++) {
      if (mid[i] == null) { upper.push(null); lower.push(null); continue; }
      const slice = vals.slice(i - period + 1, i + 1);
      const mean = mid[i];
      const std = Math.sqrt(slice.reduce((a, b) => a + (b - mean) ** 2, 0) / period);
      upper.push(mean + mult * std);
      lower.push(mean - mult * std);
    }
    return { mid, upper, lower };
  }

  function vwapSession(bars) {
    let cumPV = 0, cumV = 0;
    return bars.map(b => {
      const tp = (b.high + b.low + b.close) / 3;
      cumPV += tp * (b.volume || 0);
      cumV += b.volume || 0;
      return cumV > 0 ? cumPV / cumV : b.close;
    });
  }

  function rsi(vals, period) {
    const out = new Array(vals.length).fill(null);
    let ag = 0, al = 0;
    for (let i = 1; i < vals.length; i++) {
      const ch = vals[i] - vals[i - 1];
      const g = Math.max(0, ch), l = Math.max(0, -ch);
      if (i <= period) { ag += g; al += l; continue; }
      if (i === period + 1) { ag /= period; al /= period; }
      else { ag = (ag * (period - 1) + g) / period; al = (al * (period - 1) + l) / period; }
      const rs = al === 0 ? 100 : ag / al;
      out[i] = 100 - 100 / (1 + rs);
    }
    return out;
  }

  function macd(vals) {
    const e12 = ema(vals, 12), e26 = ema(vals, 26);
    const line = e12.map((v, i) => v - e26[i]);
    const signal = ema(line, 9);
    const hist = line.map((v, i) => v - signal[i]);
    return { line, signal, hist };
  }

  function stochastic(bars, kPeriod, dPeriod) {
    const k = [];
    for (let i = 0; i < bars.length; i++) {
      if (i < kPeriod - 1) { k.push(null); continue; }
      const sl = bars.slice(i - kPeriod + 1, i + 1);
      const lo = Math.min(...sl.map(x => x.low));
      const hi = Math.max(...sl.map(x => x.high));
      k.push(hi === lo ? 50 : ((bars[i].close - lo) / (hi - lo)) * 100);
    }
    return { k, d: sma(k.map(v => v ?? 50), dPeriod) };
  }

  function cci(bars, period) {
    const tp = bars.map(b => (b.high + b.low + b.close) / 3);
    const out = new Array(bars.length).fill(null);
    for (let i = period - 1; i < bars.length; i++) {
      const slice = tp.slice(i - period + 1, i + 1);
      const mean = slice.reduce((a, b) => a + b, 0) / period;
      const md = slice.reduce((a, b) => a + Math.abs(b - mean), 0) / period;
      out[i] = md === 0 ? 0 : (tp[i] - mean) / (0.015 * md);
    }
    return out;
  }

  function williamsR(bars, period) {
    const out = new Array(bars.length).fill(null);
    for (let i = period - 1; i < bars.length; i++) {
      const sl = bars.slice(i - period + 1, i + 1);
      const hi = Math.max(...sl.map(x => x.high));
      const lo = Math.min(...sl.map(x => x.low));
      out[i] = hi === lo ? -50 : ((hi - bars[i].close) / (hi - lo)) * -100;
    }
    return out;
  }

  function roc(vals, period) {
    return vals.map((v, i) => (i < period ? null : ((v - vals[i - period]) / vals[i - period]) * 100));
  }

  function obv(bars) {
    const out = [0];
    for (let i = 1; i < bars.length; i++) {
      const dir = bars[i].close >= bars[i - 1].close ? 1 : -1;
      out.push(out[i - 1] + dir * (bars[i].volume || 0));
    }
    return out;
  }

  function mfi(bars, period) {
    const out = new Array(bars.length).fill(null);
    const tp = bars.map(b => (b.high + b.low + b.close) / 3);
    const mf = tp.map((p, i) => p * (bars[i].volume || 0));
    for (let i = period; i < bars.length; i++) {
      let pos = 0, neg = 0;
      for (let j = i - period + 1; j <= i; j++) {
        if (tp[j] > tp[j - 1]) pos += mf[j];
        else neg += mf[j];
      }
      const ratio = neg === 0 ? 100 : pos / neg;
      out[i] = 100 - 100 / (1 + ratio);
    }
    return out;
  }

  function cmf(bars, period) {
    const out = new Array(bars.length).fill(null);
    for (let i = period - 1; i < bars.length; i++) {
      let num = 0, den = 0;
      for (let j = i - period + 1; j <= i; j++) {
        const b = bars[j];
        const mfm = (b.high === b.low) ? 0 : ((b.close - b.low) - (b.high - b.close)) / (b.high - b.low);
        num += mfm * (b.volume || 0);
        den += b.volume || 0;
      }
      out[i] = den === 0 ? 0 : num / den;
    }
    return out;
  }

  function supertrend(bars, period, mult) {
    const a = atr(bars, period);
    const st = new Array(bars.length).fill(null);
    const dir = new Array(bars.length).fill(1);
    const upper = [], lower = [];
    for (let i = 0; i < bars.length; i++) {
      const hl2 = (bars[i].high + bars[i].low) / 2;
      const au = a[i] != null ? hl2 + mult * a[i] : null;
      const al = a[i] != null ? hl2 - mult * a[i] : null;
      upper.push(au);
      lower.push(al);
    }
    for (let i = 1; i < bars.length; i++) {
      if (upper[i] == null) continue;
      upper[i] = upper[i] < upper[i - 1] || bars[i - 1].close > upper[i - 1] ? upper[i] : upper[i - 1];
      lower[i] = lower[i] > lower[i - 1] || bars[i - 1].close < lower[i - 1] ? lower[i] : lower[i - 1];
      if (st[i - 1] == null) { dir[i] = bars[i].close > upper[i] ? 1 : -1; }
      else if (st[i - 1] === upper[i - 1]) dir[i] = bars[i].close > upper[i] ? 1 : -1;
      else dir[i] = bars[i].close < lower[i] ? -1 : 1;
      st[i] = dir[i] === 1 ? lower[i] : upper[i];
    }
    return { line: st, bullish: dir.map(d => d === 1) };
  }

  function psar(bars, step, maxStep) {
    const out = new Array(bars.length).fill(null);
    if (bars.length < 2) return out;
    let af = step, ep = bars[0].high, sar = bars[0].low, bull = true;
    out[0] = sar;
    for (let i = 1; i < bars.length; i++) {
      sar = sar + af * (ep - sar);
      if (bull) {
        if (bars[i].low < sar) { bull = false; sar = ep; ep = bars[i].low; af = step; }
        else {
          if (bars[i].high > ep) { ep = bars[i].high; af = Math.min(maxStep, af + step); }
          sar = Math.min(sar, bars[i - 1].low, i > 1 ? bars[i - 2].low : bars[i - 1].low);
        }
      } else {
        if (bars[i].high > sar) { bull = true; sar = ep; ep = bars[i].high; af = step; }
        else {
          if (bars[i].low < ep) { ep = bars[i].low; af = Math.min(maxStep, af + step); }
          sar = Math.max(sar, bars[i - 1].high, i > 1 ? bars[i - 2].high : bars[i - 1].high);
        }
      }
      out[i] = sar;
    }
    return out;
  }

  function ichimoku(bars) {
    const mid = (h, l) => (Math.max(...h) + Math.min(...l)) / 2;
    const tenkan = [], kijun = [], sa = [], sb = [];
    for (let i = 0; i < bars.length; i++) {
      if (i >= 8) tenkan.push(mid(bars.slice(i - 8, i + 1).map(x => x.high), bars.slice(i - 8, i + 1).map(x => x.low)));
      else tenkan.push(null);
      if (i >= 25) kijun.push(mid(bars.slice(i - 25, i + 1).map(x => x.high), bars.slice(i - 25, i + 1).map(x => x.low)));
      else kijun.push(null);
      sa.push(tenkan[i] != null && kijun[i] != null ? (tenkan[i] + kijun[i]) / 2 : null);
      if (i >= 51) sb.push(mid(bars.slice(i - 51, i + 1).map(x => x.high), bars.slice(i - 51, i + 1).map(x => x.low)));
      else sb.push(null);
    }
    const spanA = sa.map((v, i) => (i + 25 < bars.length && v != null ? { i: i + 25, v } : null));
    const spanB = sb.map((v, i) => (i + 25 < bars.length && v != null ? { i: i + 25, v } : null));
    const senkouA = new Array(bars.length).fill(null);
    const senkouB = new Array(bars.length).fill(null);
    spanA.forEach((p, i) => { if (p) senkouA[p.i] = p.v; });
    spanB.forEach((p, i) => { if (p) senkouB[p.i] = sb[i]; });
    return { tenkan, kijun, senkouA, senkouB };
  }

  function keltner(bars, period, mult) {
    const c = closes(bars);
    const mid = ema(c, period);
    const a = atr(bars, period);
    return { mid, upper: mid.map((m, i) => (m != null && a[i] != null ? m + mult * a[i] : null)), lower: mid.map((m, i) => (m != null && a[i] != null ? m - mult * a[i] : null)) };
  }

  function donchian(bars, period) {
    const upper = [], lower = [], mid = [];
    for (let i = 0; i < bars.length; i++) {
      if (i < period - 1) { upper.push(null); lower.push(null); mid.push(null); continue; }
      const sl = bars.slice(i - period + 1, i + 1);
      const hi = Math.max(...sl.map(x => x.high));
      const lo = Math.min(...sl.map(x => x.low));
      upper.push(hi); lower.push(lo); mid.push((hi + lo) / 2);
    }
    return { upper, lower, mid };
  }

  function adx(bars, period) {
    const len = bars.length;
    const plusDM = [0], minusDM = [0], tr = trueRange(bars);
    for (let i = 1; i < len; i++) {
      const up = bars[i].high - bars[i - 1].high;
      const dn = bars[i - 1].low - bars[i].low;
      plusDM.push(up > dn && up > 0 ? up : 0);
      minusDM.push(dn > up && dn > 0 ? dn : 0);
    }
    const atrv = smma(tr, period);
    const pdi = plusDM.map((v, i) => (atrv[i] ? (100 * smma(plusDM, period)[i] / atrv[i]) : null));
    const mdi = minusDM.map((v, i) => (atrv[i] ? (100 * smma(minusDM, period)[i] / atrv[i]) : null));
    const dx = pdi.map((p, i) => (p != null && mdi[i] != null && (p + mdi[i]) ? (100 * Math.abs(p - mdi[i]) / (p + mdi[i])) : null));
    return { adx: smma(dx.map(v => v ?? 0), period), pdi, mdi };
  }

  function aroon(bars, period) {
    const up = [], down = [];
    for (let i = 0; i < bars.length; i++) {
      if (i < period) { up.push(null); down.push(null); continue; }
      const sl = bars.slice(i - period, i + 1);
      let hiI = 0, loI = 0;
      sl.forEach((b, j) => {
        if (b.high >= sl[hiI].high) hiI = j;
        if (b.low <= sl[loI].low) loI = j;
      });
      up.push(((period - (period - hiI)) / period) * 100);
      down.push(((period - (period - loI)) / period) * 100);
    }
    return { up, down };
  }

  function pivotsClassic(bars) {
    const p = new Array(bars.length).fill(null);
    const r1 = [], s1 = [];
    for (let i = 1; i < bars.length; i++) {
      const prev = bars[i - 1];
      const P = (prev.high + prev.low + prev.close) / 3;
      p[i] = P;
      r1[i] = 2 * P - prev.low;
      s1[i] = 2 * P - prev.high;
    }
    return { pivot: p, r1, s1 };
  }

  function maLine(type, period, color) {
    return {
      id: `${type}${period}`,
      name: `${type.toUpperCase()} ${period}`,
      category: "Moving averages",
      pane: "main",
      tv: "available",
      outputs: [{ key: "line", type: "line", color, width: 1 }],
      compute(bars) {
        const c = closes(bars);
        let line;
        if (type === "sma") line = sma(c, period);
        else if (type === "ema") line = ema(c, period);
        else if (type === "wma") line = wma(c, period);
        else if (type === "hma") line = hma(c, period);
        else if (type === "dema") line = dema(c, period);
        else if (type === "tema") line = tema(c, period);
        else if (type === "smma") line = smma(c, period);
        else line = ema(c, period);
        return { line };
      },
    };
  }

  const REGISTRY = [];
  [7, 9, 10, 12, 14, 20, 21, 26, 30, 50, 55, 60, 100, 150, 200, 250].forEach(p => {
    REGISTRY.push(maLine("sma", p, "#94a3b8"));
    REGISTRY.push(maLine("ema", p, p <= 21 ? "#38bdf8" : "#fb923c"));
  });
  REGISTRY.push(maLine("wma", 20, "#a3e635"));
  REGISTRY.push(maLine("hma", 21, "#2dd4bf"));
  REGISTRY.push(maLine("dema", 21, "#818cf8"));
  REGISTRY.push(maLine("tema", 21, "#f472b6"));
  REGISTRY.push(maLine("smma", 14, "#fcd34d"));

  REGISTRY.push({
    id: "vwma_20", name: "VWMA 20", category: "Moving averages", pane: "main", tv: "available",
    outputs: [{ key: "line", type: "line", color: "#eab308", width: 1 }],
    compute(b) { return { line: vwma(b, 20) }; },
  });

  REGISTRY.push({
    id: "vwap", name: "VWAP (session cumulative)", category: "Volume", pane: "main", tv: "partial",
    outputs: [{ key: "line", type: "line", color: "#facc15", width: 2 }],
    compute(b) { return { line: vwapSession(b) }; },
  });

  REGISTRY.push({
    id: "bb_20", name: "Bollinger Bands (20,2)", category: "Volatility", pane: "main", tv: "available",
    outputs: [
      { key: "upper", type: "line", color: "rgba(148,163,184,0.6)", width: 1 },
      { key: "mid", type: "line", color: "rgba(148,163,184,0.35)", width: 1 },
      { key: "lower", type: "line", color: "rgba(148,163,184,0.6)", width: 1 },
    ],
    compute(b) { const bb = bollinger(closes(b), 20, 2); return { upper: bb.upper, mid: bb.mid, lower: bb.lower }; },
  });

  REGISTRY.push({
    id: "keltner_20", name: "Keltner Channel (20, 1.5 ATR)", category: "Volatility", pane: "main", tv: "available",
    outputs: [
      { key: "upper", type: "line", color: "#67e8f9", width: 1 },
      { key: "mid", type: "line", color: "#0891b2", width: 1 },
      { key: "lower", type: "line", color: "#67e8f9", width: 1 },
    ],
    compute(b) { return keltner(b, 20, 1.5); },
  });

  REGISTRY.push({
    id: "donchian_20", name: "Donchian Channel 20", category: "Volatility", pane: "main", tv: "available",
    outputs: [
      { key: "upper", type: "line", color: "#c084fc", width: 1 },
      { key: "mid", type: "line", color: "#9333ea", width: 1, dash: true },
      { key: "lower", type: "line", color: "#c084fc", width: 1 },
    ],
    compute(b) { return donchian(b, 20); },
  });

  REGISTRY.push({
    id: "supertrend_10_3", name: "SuperTrend (10, 3)", category: "Trend", pane: "main", tv: "available",
    outputs: [{ key: "line", type: "line", color: "#22d3ee", width: 2 }],
    compute(b) { return { line: supertrend(b, 10, 3).line }; },
  });

  REGISTRY.push({
    id: "supertrend_7_2", name: "SuperTrend (7, 2)", category: "Trend", pane: "main", tv: "available",
    outputs: [{ key: "line", type: "line", color: "#06b6d4", width: 2 }],
    compute(b) { return { line: supertrend(b, 7, 2).line }; },
  });

  REGISTRY.push({
    id: "psar", name: "Parabolic SAR (0.02, 0.2)", category: "Trend", pane: "main", tv: "available",
    outputs: [{ key: "line", type: "dots", color: "#fbbf24", width: 1 }],
    compute(b) { return { line: psar(b, 0.02, 0.2) }; },
  });

  REGISTRY.push({
    id: "ichimoku", name: "Ichimoku Cloud (lines)", category: "Trend", pane: "main", tv: "partial",
    outputs: [
      { key: "tenkan", type: "line", color: "#ef4444", width: 1 },
      { key: "kijun", type: "line", color: "#3b82f6", width: 1 },
      { key: "senkouA", type: "line", color: "rgba(34,197,94,0.5)", width: 1 },
      { key: "senkouB", type: "line", color: "rgba(239,68,68,0.5)", width: 1 },
    ],
    compute(b) { return ichimoku(b); },
  });

  REGISTRY.push({
    id: "rsi_14", name: "RSI 14", category: "Momentum", pane: "osc", tv: "available",
    outputs: [{ key: "line", type: "line", color: "#a78bfa", width: 1.5 }],
    compute(b) { return { line: rsi(closes(b), 14) }; },
  });

  REGISTRY.push({
    id: "macd", name: "MACD (12,26,9)", category: "Momentum", pane: "osc_macd", tv: "available",
    outputs: [
      { key: "line", type: "line", color: "#38bdf8", width: 1 },
      { key: "signal", type: "line", color: "#fb923c", width: 1 },
      { key: "hist", type: "hist", color: "#64748b", width: 1 },
    ],
    compute(b) { return macd(closes(b)); },
  });

  REGISTRY.push({
    id: "stoch_14_3", name: "Stochastic (14,3)", category: "Momentum", pane: "osc", tv: "available",
    outputs: [
      { key: "k", type: "line", color: "#38bdf8", width: 1 },
      { key: "d", type: "line", color: "#f97316", width: 1 },
    ],
    compute(b) { return stochastic(b, 14, 3); },
  });

  REGISTRY.push({
    id: "cci_20", name: "CCI 20", category: "Momentum", pane: "osc", tv: "available",
    outputs: [{ key: "line", type: "line", color: "#e879f9", width: 1 }],
    compute(b) { return { line: cci(b, 20) }; },
  });

  REGISTRY.push({
    id: "williams_14", name: "Williams %R 14", category: "Momentum", pane: "osc", tv: "available",
    outputs: [{ key: "line", type: "line", color: "#f472b6", width: 1 }],
    compute(b) { return { line: williamsR(b, 14) }; },
  });

  REGISTRY.push({
    id: "roc_12", name: "ROC 12", category: "Momentum", pane: "osc", tv: "available",
    outputs: [{ key: "line", type: "line", color: "#4ade80", width: 1 }],
    compute(b) { return { line: roc(closes(b), 12) }; },
  });

  REGISTRY.push({
    id: "adx_14", name: "ADX 14 (+DI/-DI)", category: "Trend strength", pane: "osc", tv: "available",
    outputs: [
      { key: "adx", type: "line", color: "#f1f5f9", width: 1.5 },
      { key: "pdi", type: "line", color: "#10b981", width: 1 },
      { key: "mdi", type: "line", color: "#ef4444", width: 1 },
    ],
    compute(b) { return adx(b, 14); },
  });

  REGISTRY.push({
    id: "aroon_25", name: "Aroon 25", category: "Trend strength", pane: "osc", tv: "available",
    outputs: [
      { key: "up", type: "line", color: "#22c55e", width: 1 },
      { key: "down", type: "line", color: "#ef4444", width: 1 },
    ],
    compute(b) { return aroon(b, 25); },
  });

  REGISTRY.push({
    id: "obv", name: "OBV", category: "Volume", pane: "osc", tv: "available",
    outputs: [{ key: "line", type: "line", color: "#94a3b8", width: 1 }],
    compute(b) { return { line: obv(b) }; },
  });

  REGISTRY.push({
    id: "mfi_14", name: "MFI 14", category: "Volume", pane: "osc", tv: "available",
    outputs: [{ key: "line", type: "line", color: "#2dd4bf", width: 1 }],
    compute(b) { return { line: mfi(b, 14) }; },
  });

  REGISTRY.push({
    id: "cmf_20", name: "Chaikin Money Flow 20", category: "Volume", pane: "osc", tv: "available",
    outputs: [{ key: "line", type: "line", color: "#60a5fa", width: 1 }],
    compute(b) { return { line: cmf(b, 20) }; },
  });

  REGISTRY.push({
    id: "atr_bands", name: "ATR envelope (14, 2×)", category: "Volatility", pane: "main", tv: "available",
    outputs: [
      { key: "upper", type: "line", color: "#fda4af", width: 1 },
      { key: "lower", type: "line", color: "#fda4af", width: 1 },
    ],
    compute(b) {
      const c = closes(b);
      const a = atr(b, 14);
      return { upper: c.map((v, i) => (a[i] != null ? v + 2 * a[i] : null)), lower: c.map((v, i) => (a[i] != null ? v - 2 * a[i] : null)) };
    },
  });

  REGISTRY.push({
    id: "pivot_classic", name: "Pivot points (classic, daily bar proxy)", category: "Levels", pane: "main", tv: "partial",
    outputs: [
      { key: "pivot", type: "line", color: "#e2e8f0", width: 1, dash: true },
      { key: "r1", type: "line", color: "#86efac", width: 1, dash: true },
      { key: "s1", type: "line", color: "#fca5a5", width: 1, dash: true },
    ],
    compute(b) { return pivotsClassic(b); },
  });

  const BY_ID = Object.fromEntries(REGISTRY.map(x => [x.id, x]));
  const CATEGORIES = [...new Set(REGISTRY.map(x => x.category))].sort();

  function catalogMeta() {
    return REGISTRY.map(({ id, name, category, pane, tv }) => ({ id, name, category, pane, tv: tv || "available" }));
  }

  function compute(id, bars) {
    const ind = BY_ID[id];
    if (!ind) return null;
    return ind.compute(bars);
  }

  return { REGISTRY, BY_ID, CATEGORIES, catalogMeta, compute, count: () => REGISTRY.length };
})();
