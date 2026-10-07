# Binance plan (7 Oct 2026)

Everything here is paper trading until the owner says yes to the **REAL MONEY** alert. The only Binance key in use
is read-only (Mac, `~/.config/crypto-engine/binance.env`, chmod 600, "Enable Reading" only, no IP whitelist).

## 1. What we verified (from the Mac, Indian network, 7 Oct)

| Check | Result |
|---|---|
| 500 public calls (spot, futures, funding, depth, data mirror) | 500/500 OK, median ~210 ms, no HTTP 451 (not geo-blocked) |
| 60 s continuous test of every read-only endpoint, once per second | 679/679 OK; peak weight 1,531 / 2,400 per minute (futures), 828 / 6,000 (spot) |
| Account endpoints with the read-only key | All OK: key permissions, all wallets, spot and futures balances, positions, open orders, income, user trades, fee rate |
| **Futures access for the owner's Indian account** | **Works** (USD-M wallet and a live position exist). Guides saying Binance Futures is closed to Indian users do not apply to this account today; it could change. |
| Owner's futures fees | maker 0.020%, taker 0.050% (VIP 0) |
| WebSocket | Binance split the futures streams (see 3). |

## 2. Order rules (analysis/binance_filters.py)

| Coin | Min order value | Step | Notes |
|---|---|---|---|
| XRP, ADA, BNB, DOGE, SOL, AVAX, SUI | $5 | 0.1 / 1 / 0.01 / 1 / 0.01 / 1 / 0.1 | tradable from a ~$30 wallet at 3% risk |
| ETH, LINK, BCH, LTC | $20 | 0.001 / 0.01 / 0.001 / 0.001 | need ~$100+ at 1% risk |
| BTC | $50, and min 0.001 BTC (~$120) | 0.001 | needs ~$330 at 1% risk |

Paper swing trades now follow these rules (`swing_binance_rules`, on): quantity rounded down to the step, and a
trade Binance would reject is skipped as `below_one_lot` or `below_min_notional`. Refresh the table with
`python -m scripts.binance_filters`.

## 3. Data plan: REST vs WebSocket

**Use WebSocket for anything per second** (free: no rate-limit weight). Endpoints as of Oct 2026:

| Stream | URL | Rate |
|---|---|---|
| Mark price + funding (`<sym>@markPrice@1s`), trades (`@aggTrade`), candles (`@kline_1m`) | `wss://fstream.binance.com/market/stream?streams=...` | mark price exactly 1/s per coin |
| Best bid/ask (`@bookTicker`), order book (`@depth5@100ms`) | `wss://fstream.binance.com/public/stream` (legacy `/stream` still works for these) | ~1,600 msgs/s for 11 coins |
| **Pitfall** | The legacy `wss://fstream.binance.com/stream` returns **nothing** for markPrice / aggTrade / kline, silently | |

**REST budget (futures 2,400 weight/min per IP):** the 60 s test used 1,531/min with all of: all prices,
all best bid/ask and all mark prices every second (17/s), one coin's order book, open interest, candles and trades
every second, account reads every 5 s, 24h stats every 20 s. The engine needs far less: swing decisions use closed
4h/8h candles; the paper tick needs one batch price call (`_perp_prices`, every 10 s). Keep REST under 1,200/min
so a burst never reaches 429 (and never 418, an IP ban). Rule in code: any 429 or 418 stops all REST calls.

## 4. Engine changes made (branch, not deployed: owner reviews first)

1. **Sizing freeze fixed**: one trade's margin is capped at 15% of the wallet (`swing_max_margin_frac`); leverage
   rises to fit (ceiling 10x). Before: a 1x SOL trade took ₹1,238 of ₹1,735 and every later signal was skipped.
2. **Binance order rules** in paper (above).
3. **Lab wallet can size like production** (`WalletConfig(live_sizing=True, max_margin_frac=..., binance_rules=True)`)
   and `scripts/sizing_backtest.py` compares sizing rules by wallet size.
4. **Honest significance**: null tests report z and a normal-approx p judged at Bonferroni N (N from the trial
   ledger, at least 579: the distinct configurations the lab has scored).

## 5. Backtest of the changes (in-sample, 2021-10 to 2026-09, 12-month windows started monthly)

See `data/lab/sizing_backtest.json`. Median 12-month result (end balance / start), median worst drawdown:

| Wallet | Old: 3%, no cap | New: 3%, cap + Binance rules | New: 1%, cap + Binance rules |
|---|---|---|---|
| $29 (~₹3,000) | x2.38, dd 64% | x2.19, dd 62% | x1.26, dd 24% (most signals too small for Binance) |
| $50 | x2.40, dd 66% | x2.18, dd 63% | x1.49, dd 35% |
| $100 | x2.47, dd 67% | x2.31, dd 66% | x1.52, dd 42% |
| $300 | x2.48, dd 67% | x2.54, dd 67% | x1.69, dd 46% |

Read with care: the strategies were chosen on this same history, so real results will be lower. The cap does not
raise returns; it stops one trade freezing the book and lets the book take its other signals. **3% risk means a
typical worst drawdown of about two thirds of the wallet** in every size; 1% roughly halves that and the return.
Phase 1 guards (9% daily loss, pause after 3 losses) cost ~5-8% of the median return and did not reduce the
drawdown at 3% risk: the drawdown comes from the risk level, not from bad days.

## 6. Tax (owner's question: speculative business income)

- What it is: crypto futures that never transfer a coin (only price differences settled) can be argued to be a
  speculative business (old Act s.43(5)), taxed at slab rates instead of the 30% flat VDA rate (s.115BBH), with no
  1% TDS. Several CAs argue it for INR-settled futures on Indian exchanges; some (e.g. a July 2026 CAclubindia
  article) extend it to USDT-settled perpetuals on offshore exchanges like Binance.
- What it is not: settled law. No CBDT circular and no court ruling; the conservative view is 30% VDA tax with no
  loss set-off. From 1 Apr 2026 the VDA definition explicitly covers crypto-assets, and the new Income-tax Act 2025
  renumbers the sections.
- Speculative losses only offset speculative profits, and carry forward 4 years (if filed on time). Turnover is
  the sum of absolute profits and losses, which matters for audit limits. Needs full exchange records and a
  reasoned note.
- "INR wallet only" is not the deciding fact; the instrument is (no delivery of the coin). Depositing INR and
  converting to USDT on Binance is itself a VDA transfer.
- **Gate: a CA's written view before the first real trade.** It changes after-tax returns, not the engine.

## 7. Path to a live trade (each step needs the owner)

1. Owner reviews this branch; deploy to production (CI gate) after review.
2. Paper wallet reset with a recorded start date, sized like the intended live wallet (owner picks the size).
3. Forward paper period: about 3 months or 60 swing trades, results inside the backtest's expected band.
4. CA's written view on tax (section 6).
5. **REAL MONEY alert** -> owner's explicit yes -> new Binance key with "Enable Futures" (never withdrawals),
   IP-whitelisted to the server that trades (EC2 52.62.37.4 now, or the home server later).
6. Wire `execution/` (built and unit-tested on a fake exchange) with: minimum size first, every order
   reconciled against Binance's own position, a kill switch on /keys, Telegram on every fill.
