# Chart Pro vs TradingView — parity matrix

We use **TradingView Lightweight Charts** (open source) plus our own indicator library and drawing canvas. This is **not** the commercial TradingView widget (no Pine Script, no TV account login).

Legend: **✅ Available** · **🟡 Partial** · **❌ Blocked / not yet** · **🔒 TV proprietary**

---

## Indicators & filters (target: 50+)

| Feature | Status | Notes |
|--------|--------|-------|
| SMA / EMA (many periods) | ✅ | Catalog: 7–200 |
| WMA, HMA, DEMA, TEMA, SMMA, VWMA | ✅ | Client-side |
| Bollinger, Keltner, Donchian, ATR bands | ✅ | |
| **SuperTrend** | ✅ | (7,2) and (10,3) presets |
| Parabolic SAR | ✅ | Dots as line series |
| Ichimoku (lines) | 🟡 | Tenkan/Kijun/Senkou lines; **cloud fill** not shaded yet |
| VWAP | 🟡 | Session-cumulative on loaded bars (not anchored RTH) |
| Pivot points | 🟡 | Classic, prior-bar proxy (not true daily session) |
| RSI, MACD, Stochastic, CCI, Williams %R, ROC | ✅ | Oscillator panes |
| ADX, Aroon | ✅ | |
| OBV, MFI, CMF | ✅ | |
| Volume histogram | ✅ | On main chart |
| Custom indicator params UI | ❌ | Presets only; **blocker:** need param editor per indicator |
| Pine Script / TV public library | 🔒 | Requires TradingView **Advanced Chart Widget** + license |
| 400+ built-in TV indicators | 🔒 | Same; we add ours incrementally in `chart-indicators.js` |

**Current catalog size:** see `/api/chart/indicator-catalog` (same list as the Indicators modal).

---

## Drawings & annotations

| Feature | Status | Notes |
|--------|--------|-------|
| Trend line | ✅ | Snap to time/price |
| Horizontal / vertical line | ✅ | |
| Ray | ✅ | |
| Rectangle | ✅ | |
| Fibonacci retracement | ✅ | 7 levels |
| Text label | ✅ | |
| Select / delete last (double-click) | ✅ | |
| Clear all drawings | ✅ | Per symbol+TF in localStorage |
| Extended line, parallel channel, pitchfork | ❌ | **Blocker:** drawing engine v2 |
| Elliott wave, Gann fan | ❌ | Low priority |
| Drawings synced server-side / multi-device | ❌ | **Blocker:** API + auth + DB |
| Alerts on drawings | ❌ | **Blocker:** alert service |

---

## Chart interactions

| Feature | Status | Notes |
|--------|--------|-------|
| Pan / zoom / crosshair | ✅ | |
| Log scale | ✅ | |
| Fit content | ✅ | |
| Multi-chart layout (2×2) | ❌ | **Blocker:** layout UI |
| Bar replay / step forward | ❌ | **Blocker:** replay controller + time scrubber |
| Compare symbol (ratio) | ❌ | **Blocker:** second series feed |
| Heikin Ashi, Renko, Kagi | ❌ | **Blocker:** transform pipeline on klines |
| Depth / DOM / footprint | ❌ | **Blocker:** L2 order book data on Delta/Binance |
| Volume profile (VPVR) | ❌ | **Blocker:** tick/volume-at-price aggregation |

---

## Data & strategies (our stack)

| Feature | Status | Notes |
|--------|--------|-------|
| Lake parquet klines | ✅ | Resampled TF |
| Strategy scan markers (simulator) | ✅ | |
| DB signal history overlays | ✅ | |
| Live tick updates | ❌ | **Blocker:** websocket → chart push |
| News/macro badges (old chart) | 🟡 | API exists; re-wire to LWC markers |

---

## How to unblock 🔒 / ❌ items

1. **TradingView full widget + Pine** — Product decision: TV commercial license, embed widget, surrender custom strategy scan on chart or bridge via webhooks.
2. **Cloud Ichimoku / shaded bands** — Add `AreaSeries` pair in Lightweight Charts.
3. **Indicator parameter UI** — Modal fields per indicator (`period`, `mult`) stored in localStorage.
4. **Server-side drawings** — `chart_drawings` table + CRUD API.
5. **Live streaming candles** — Reuse portal candle poll or Binance WS into chart series `update()`.
6. **Volume profile** — Aggregate `data/lake` 1m/5m volume by price bins; heavy compute, optional worker.

When you pick a blocked row, we implement in priority order on branch (never push `main` without deploy request).
