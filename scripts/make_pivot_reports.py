"""Build the 5 pivot PDF reports (8 Oct 2026).

    python -m scripts.make_pivot_reports

Outputs to reports/:
  01_test_data.pdf           backtest / edge / AI-gate numbers
  02_wallet_simulation.pdf   Rs5,000 plan, compounding, monthly ledger
  03_pivot_analysis.pdf      crypto -> Indian equities: what transfers, tax, hurdles
  04_new_project_plan.pdf    the Indian equity engine plan + phases
  05_architecture_changes.pdf current vs target architecture, service levels

All figures are the real outputs pulled from the ThinkPad lab and the live DB on 8 Oct 2026.
Paper / research only. Not investment advice.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                Preformatted, Spacer, Table, TableStyle)

OUT = Path("reports")
OUT.mkdir(exist_ok=True)

INK = colors.HexColor("#0f172a")
MUTE = colors.HexColor("#475569")
LINE = colors.HexColor("#cbd5e1")
GOOD = colors.HexColor("#15803d")
BAD = colors.HexColor("#b91c1c")
WARN = colors.HexColor("#b45309")
SOFT = colors.HexColor("#f1f5f9")
ACCENT = colors.HexColor("#1d4ed8")

S = {
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=INK),
    "sub": ParagraphStyle("sub", fontName="Helvetica", fontSize=10.5, leading=15, textColor=MUTE),
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=14, leading=18, textColor=ACCENT,
                         spaceBefore=12, spaceAfter=4),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, leading=15, textColor=INK,
                         spaceBefore=8, spaceAfter=2),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.8, leading=14, textColor=INK,
                           spaceAfter=5),
    "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=9.8, leading=14, textColor=INK,
                             leftIndent=12, bulletIndent=2, spaceAfter=3),
    "note": ParagraphStyle("note", fontName="Helvetica-Oblique", fontSize=9, leading=13, textColor=MUTE,
                           spaceAfter=5),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.6, leading=11, textColor=INK),
    "cellb": ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=8.6, leading=11, textColor=colors.white),
    "code": ParagraphStyle("code", fontName="Courier", fontSize=8, leading=10.5, textColor=INK),
}


def esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def P(t, style="body"):
    return Paragraph(t, S[style])


def bullets(items):
    return [Paragraph(f'<bullet>&bull;</bullet>{t}', S["bullet"]) for t in items]


def table(header, rows, widths=None, align_right_from=1):
    data = [[Paragraph(f"<b>{esc(str(h))}</b>", ParagraphStyle("hh", parent=S["cell"], textColor=colors.white))
             for h in header]]
    for r in rows:
        data.append([Paragraph(esc(str(c)), S["cell"]) for c in r])
    t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
    ]))
    return t


def codeblock(txt):
    return Preformatted(txt, S["code"])


def note(txt, color=WARN):
    box = Table([[Paragraph(f"<b>{esc(txt)}</b>", ParagraphStyle("n", parent=S["body"], textColor=color))]],
                colWidths=[170 * mm])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fffbeb")),
        ("BOX", (0, 0), (-1, -1), 0.6, color),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return box


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTE)
    canvas.drawString(20 * mm, 12 * mm, doc._title)
    canvas.drawRightString(190 * mm, 12 * mm, f"page {doc.page}  ·  paper/research only · not investment advice")
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.4)
    canvas.line(20 * mm, 15 * mm, 190 * mm, 15 * mm)
    canvas.restoreState()


def build(fname, title, subtitle, blocks):
    doc = BaseDocTemplate(str(OUT / fname), pagesize=A4,
                          leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=20 * mm)
    doc._title = title
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=_footer)])
    story = [Paragraph(esc(title), S["title"]), Spacer(1, 2 * mm), Paragraph(esc(subtitle), S["sub"]),
             Spacer(1, 3 * mm)]
    for kind, payload in blocks:
        if kind == "h1":
            story.append(Paragraph(esc(payload), S["h1"]))
        elif kind == "h2":
            story.append(Paragraph(esc(payload), S["h2"]))
        elif kind == "p":
            story.append(P(payload))
        elif kind == "note":
            story.append(Spacer(1, 1 * mm)); story.append(note(payload)); story.append(Spacer(1, 1 * mm))
        elif kind == "bullets":
            story.extend(bullets(payload))
        elif kind == "table":
            h, rows = payload[0], payload[1]
            widths = payload[2] if len(payload) > 2 else None
            story.append(Spacer(1, 1 * mm)); story.append(table(h, rows, widths)); story.append(Spacer(1, 2 * mm))
        elif kind == "code":
            story.append(Spacer(1, 1 * mm)); story.append(codeblock(payload)); story.append(Spacer(1, 2 * mm))
        elif kind == "spacer":
            story.append(Spacer(1, payload))
    doc.build(story)
    print("wrote", OUT / fname)


# ----------------------------------------------------------------------------- PDF 1: TEST DATA
def pdf_test_data():
    blocks = [
        ("p", "Everything below is the measured output of the lab backtests run on the ThinkPad "
              "(8 Oct 2026) over the full 4-year Binance history. No AI involved unless labelled. "
              "Costs are Binance taker fees + funding + slippage, netted into every R figure."),
        ("h1", "1. What was tested"),
        ("bullets", [
            "Data: 4 years of BTC/ETH/SOL/XRP/DOGE/LINK/BNB + alts, 1h bars aggregated to 1h/4h/8h/1d.",
            "Strategies: 8 specs (vol_breakout, keltner, ichimoku, trend_pullback, range_fade, ...) x 4 timeframes.",
            "Wallet sim: Rs5,000 start, sweep Rs3,000 each time equity hits Rs10,000, live risk settings.",
            "AI gate: 150/150 completed trades scored by the local LLM (Ollama) vs a no-AI baseline.",
            "Trial ledger: audit/trial_ledger.jsonl holds 2,879 configs (2,823 from search).",
        ]),
        ("h1", "2. Edge reality (in-sample, net of costs)"),
        ("table", (
            ["Metric", "Value", "Reading"],
            [
                ["Book expectancy", "+0.357 R / trade", "real, positive"],
                ["Cluster t-stat", "6.5", "well above the ~2 bar"],
                ["95% CI on expectancy", "+0.25 ... +0.47 R", "excludes zero"],
                ["Win rate", "48%", "trend-style: <50% but winners bigger"],
                ["Trades in sample", "3,430", "statistically meaningful"],
                ["Specs beating random", "8 / 8", "null_p = 0.00498"],
                ["Best 4h spec (CI-low)", "vol_breakout 0.195", "most robust"],
                ["Best 8h spec (CI-low)", "keltner 0.187", "best on 8h"],
            ],
            [52*mm, 52*mm, 66*mm],
        )),
        ("h1", "3. What the sweep found"),
        ("bullets", [
            "Timeframes: edge is real on 4h and 8h. Ichimoku is net-negative below 4h.",
            "Exits: current exit (3x ATR stop / 3R target / 7-day time stop) is the best of the set tested.",
            "Cost stress: edge survives at +0.01 R extra cost per trade (still positive).",
            "Regimes: edge VANISHES in wild/high-vol markets (-0.32 R). The volatility filter is the key rule.",
            "Filters REJECTED: KAMA, McGinley, dead-market filter all hurt or added nothing.",
        ]),
        ("h1", "4. AI gate verdict (150/150 done)"),
        ("table", (
            ["Metric", "Value", "Reading"],
            [
                ["AUC", "0.499", "coin flip (0.5 = no skill)"],
                ["Veto precision", "50.0%", "vetoes are random"],
                ["Take / Skip", "36 / 114", "skipped most trades"],
                ["Lift 95% CI", "-0.36 ... +0.81", "includes zero -> no effect"],
                ["Kept share of total R", "32%", "threw away 68% of profit"],
            ],
            [52*mm, 52*mm, 66*mm],
        )),
        ("note", "VERDICT: the AI gate is statistically worthless on this data. Do NOT wire it to live trades."),
        ("h1", "5. Honest limits"),
        ("bullets", [
            "All of the above is IN-SAMPLE. The forward test (cycle 2, started 7 Oct) has ~0 closed trades so far.",
            "Survivorship bias: only coins that survived are in the universe.",
            "Constants baked into the live path (STRATEGY_R, COIN_R, VOL_RANK_MAX=0.67) are in-sample fits.",
            "execution/ (453 lines) has never placed a real order - fake-exchange tested only.",
        ]),
    ]
    build("01_test_data.pdf", "Test Data & Edge Report",
          "Crypto swing engine - lab backtests, null tests, AI-gate verdict. 8 Oct 2026.", blocks)


# ----------------------------------------------------------------------------- PDF 2: WALLET SIM
def pdf_wallet():
    blocks = [
        ("p", "Simulation of the live plan over the 4-year backtest: start Rs5,000, withdraw Rs3,000 "
              "every time equity reaches Rs10,000, using the live risk settings (1-3% risk per trade, "
              "9% side-risk cap, 3x ATR / 3R / 7-day exits). All figures net of costs."),
        ("h1", "1. Headline result"),
        ("table", (
            ["Metric", "Value"],
            [
                ["Starting capital", "Rs5,000"],
                ["Trades taken", "808 (of ~3,430 signals - small balance skips many)"],
                ["Win rate", "48%"],
                ["Avg per trade", "+0.31 R"],
                ["Total withdrawn", "Rs27,000 (9 sweeps of Rs3,000)"],
                ["Final balance", "Rs7,056"],
                ["Total value out", "Rs34,056 (x6.8 on Rs5,000)"],
                ["Times busted", "0 (never)"],
                ["Max drawdown", "30%"],
                ["First withdrawal", "26 months in (Dec 2023)"],
            ],
            [70*mm, 100*mm],
        )),
        ("note", "Patience is the whole game: the first Rs3,000 sweep took 26 months. This is not a monthly-income plan."),
        ("h1", "2. Pure compounding (no withdrawals)"),
        ("p", "The same trades with profits left in to compound instead of being swept:"),
        ("table", (
            ["Path", "Rs5,000 becomes", "Multiple"],
            [
                ["Sweep plan (withdraw Rs3k @ Rs10k)", "Rs34,056 total out", "x6.8"],
                ["Pure compound (no withdrawals)", "Rs118,104", "x23.6"],
            ],
            [80*mm, 55*mm, 35*mm],
        )),
        ("p", "Compounding beats the sweep plan ~3.5x over 4 years, but leaves all gains exposed the whole "
              "time. The sweep plan trades some upside for realised, banked profit and lower tail risk."),
        ("h1", "3. Sizing reality at small balance"),
        ("bullets", [
            "At Rs5,000 the 1-3% risk rule plus minimum lot sizes means ~80% of signals are skipped.",
            "Only the highest-conviction, cheapest-margin setups actually get taken (808 of 3,430).",
            "This is correct behaviour: forcing every signal at tiny size would just bleed fees.",
        ]),
        ("h1", "4. Honest notes"),
        ("bullets", [
            "In-sample. The forward test will confirm or break these numbers.",
            "Assumes fills at backtest prices; live slippage on a real account will shave the edge.",
            "30% max drawdown on Rs5,000 is Rs1,500 - survivable, but it will feel bad in the moment.",
        ]),
    ]
    build("02_wallet_simulation.pdf", "Wallet Simulation",
          "Rs5,000 swing plan - sweep vs compound, 4-year backtest. 8 Oct 2026.", blocks)


# ----------------------------------------------------------------------------- PDF 3: PIVOT
def pdf_pivot():
    blocks = [
        ("p", "Should the work pivot from crypto to Indian equities? Short answer: the discipline, "
              "statistics, risk framework and engineering all transfer. The crypto edge, the Binance "
              "data, the funding mechanics and the 24/7 assumption do NOT."),
        ("h1", "1. What transfers vs what does not"),
        ("table", (
            ["Asset", "Transfers?", "Notes"],
            [
                ["Risk framework (1-3% risk, caps)", "YES", "broker-agnostic, the core asset"],
                ["Stats discipline (CI, null, walk-forward)", "YES", "same method, new data"],
                ["Lab / simulate / metrics engine", "YES (port)", "re-skin bars to NSE daily"],
                ["Ledger + wallet accounting", "YES (port)", "reuse the cycle/sweep logic"],
                ["Engineering (tests, CI, ledger)", "YES", "carry the habits"],
                ["The crypto edge itself", "NO", "re-validate from scratch on NSE"],
                ["Binance data + funding mechanics", "NO", "NSE has no funding rate"],
                ["24/7 trading assumption", "NO", "NSE is 09:15-15:30 IST, T+1"],
            ],
            [62*mm, 26*mm, 82*mm],
        )),
        ("h1", "2. Tax: F&O vs crypto VDA (India)"),
        ("table", (
            ["Rule", "Indian F&O / equity", "Crypto (VDA)"],
            [
                ["Tax on gains", "Slab / STCG-LTCG rates", "30% flat"],
                ["Loss set-off", "Allowed (business/capital)", "NOT allowed"],
                ["Expense deduction", "Allowed (business income)", "NOT allowed"],
                ["TDS", "None on gains", "1% TDS on every sell"],
            ],
            [45*mm, 62*mm, 62*mm],
        )),
        ("p", "Structurally F&O/equity is friendlier than crypto VDA. But at the Rs5k-50k scale this is a "
              "rounding error. Do not pivot FOR tax - pivot only if the Indian edge proves better."),
        ("h1", "3. The leverage correction"),
        ("note", "CORRECTION: 11x intraday leverage is NOT available. Under SEBI peak-margin rules "
                 "(mandatory since Sep 2021), intraday equity leverage is capped at 5x (20% margin) "
                 "across ALL brokers, Zerodha included. 11x is outdated marketing or a different product. "
                 "Design the plan for the 5x regulatory cap."),
        ("h1", "4. Recommendation"),
        ("bullets", [
            "Do not abandon crypto mid-forward-test. Let cycle 2 finish and give an out-of-sample verdict.",
            "In parallel, build the Indian equity engine (this plan) on daily bars - the natural swing TF.",
            "Re-validate every edge on NSE data from scratch. Assume nothing carries over until proven.",
            "Keep both paper-only until each passes its own go-live gate.",
        ]),
    ]
    build("03_pivot_analysis.pdf", "Pivot Analysis",
          "Crypto -> Indian equities: what transfers, tax, the 11x correction. 8 Oct 2026.", blocks)


# ----------------------------------------------------------------------------- PDF 4: NEW PLAN
def pdf_new_plan():
    blocks = [
        ("p", "A new, separate project: an Indian equity swing + intraday engine on Zerodha (Kite Connect). "
              "Equity delivery and intraday only. NO futures. Options rarely, only around major events. "
              "Blue-chip / Nifty-50 universe. This is a patience game, not a daily-income machine."),
        ("note", "Leverage: design for the SEBI 5x intraday cap (20% margin), not 11x. Delivery (CNC) is "
                 "1x - full cash, no leverage. Intraday (MIS) gets up to 5x on margin. No overnight short "
                 "in cash equity; intraday positions auto-square-off ~15:20 IST."),
        ("h1", "1. Constraints & ground rules"),
        ("bullets", [
            "Instruments: equity delivery (CNC) + intraday (MIS). No futures. Options only for major events.",
            "Universe: Nifty-50 / large-cap blue chips (liquid, tight spreads, hard to manipulate).",
            "Broker: Zerodha via Kite Connect API (paid). Paper-first, real money only after the go-live gate.",
            "Timeframe: daily bars for swing; intraday uses 5m/15m within 09:15-15:30 IST.",
            "Risk: 0.5-1% per trade to start (smaller than crypto - equities gap on news/results).",
            "Non-negotiable: paper/POC only until an explicit go-live gate is passed. No real orders by default.",
        ]),
        ("h1", "2. Indian market specifics to bake in"),
        ("table", (
            ["Item", "Value / rule"],
            [
                ["Hours", "09:15-15:30 IST, Mon-Fri (plus NSE/BSE holidays)"],
                ["Settlement", "T+1 (shares/cash settle next day)"],
                ["STT", "0.1% delivery (both sides) / 0.025% intraday-sell"],
                ["Stamp duty", "0.015% on buy"],
                ["Exchange txn", "~0.00345% (NSE)"],
                ["GST", "18% on brokerage + txn charges"],
                ["SEBI charge", "Rs10 / crore"],
                ["DP charge", "Rs15.93 / scrip on delivery SELL"],
                ["Zerodha brokerage", "Rs0 delivery / Rs20 (or 0.03%) per intraday order"],
                ["Intraday square-off", "auto ~15:20 IST if not closed"],
            ],
            [45*mm, 125*mm],
        )),
        ("h1", "3. Knowledge carried over from the crypto project"),
        ("bullets", [
            "1. Ledger-first accounting: every trade, fee and sweep in an append-only ledger.",
            "2. In-sample vs out-of-sample discipline: never trust a backtest alone.",
            "3. Null tests + cluster t-stats + walk-forward before believing any edge.",
            "4. Cost model first: realistic STT/stamp/GST/DP netted into every R.",
            "5. Risk caps enforced in code (per-trade, per-side, daily-loss kill switch).",
            "6. Position reconciliation loop: broker positions vs book, alert on drift.",
            "7. Tests + CI gate before any deploy; never push red.",
            "8. No secrets in git; keys in env / a secrets store.",
            "9. Dashboard + watchdog so the monitor survives network lag.",
            "10. AI gate is optional and must PROVE lift before it touches trades (it failed in crypto).",
        ]),
        ("h1", "4. Phases (start at Phase 0; await go-ahead each step)"),
        ("table", (
            ["Phase", "Goal", "Key deliverables", "Exit criteria"],
            [
                ["0 - Foundation", "Zerodha + data + repo",
                 "Kite Connect app + token flow; NSE daily-bar data lake (Nifty-50, 5-10y); "
                 "Indian cost model module; repo skeleton w/ tests+CI; ledger schema",
                 "can pull clean daily bars; cost model unit-tested; CI green"],
                ["1 - Lab port", "backtest engine on NSE",
                 "port simulate/metrics/wallet; daily-bar swing strategies (trend, breakout, pullback); "
                 "null + walk-forward + cluster stats",
                 "an edge with CI excluding zero, or an honest 'no edge' report"],
                ["2 - Paper engine", "market-hours forward test",
                 "live paper book on NSE hours; entry/exit engine; dashboard + watchdog; "
                 "forward ledger",
                 "60+ paper trades logged, accounting reconciles"],
                ["3 - Execution spine", "order plumbing (still paper)",
                 "Kite order placement in sandbox/paper; position reconciliation; risk watchdog; "
                 "kill switch",
                 "paper orders fill + reconcile cleanly for 2+ weeks"],
                ["4 - Go-live gate", "decide real money",
                 "3 months / 60+ trades forward record; CA tax opinion (business income + audit); "
                 "min-size real plan",
                 "explicit YES from owner; start min size"],
                ["5 - Scale (optional)", "grow carefully",
                 "size up only on proven edge; consider rare event options with strict IV rules",
                 "only after Phase 4 passes"],
            ],
            [26*mm, 30*mm, 78*mm, 36*mm],
        )),
        ("h1", "5. Honest cautions"),
        ("bullets", [
            "Options buying around events suffers IV crush - the move can be right and the option still loses.",
            "Equities gap on results/news; a stop is not a guarantee. Size down vs crypto.",
            "Daily-bar swing means weeks-long holds - this is the patience the user asked for.",
            "No edge is assumed. Phase 1 must PROVE it on NSE data before any money is discussed.",
        ]),
    ]
    build("04_new_project_plan.pdf", "New Project Plan - Indian Equity Engine",
          "Zerodha, delivery + intraday, no futures, rare options, blue chips. 8 Oct 2026.", blocks)


# ----------------------------------------------------------------------------- PDF 5: ARCHITECTURE
def pdf_architecture():
    blocks = [
        ("p", "Current crypto architecture vs the target, then the proposed architecture for the new "
              "Indian equity engine. The theme: separate the always-on collector from the trading "
              "decision path, and make the ledger the single source of truth."),
        ("h1", "1. Current crypto architecture (as-is)"),
        ("code",
         "EC2 (single box, 17d up)                 Aiven free Postgres (54MB)\n"
         "  crypto-engine.service                    crypto_snapshots 32MB/94.9k rows\n"
         "    collectors -> DB -> analysis -> book\n"
         "    health.py (8,131 lines - god file)\n"
         "  deploy: main -> GitHub Actions -> EC2 (test gate)\n"
         "ThinkPad dhruv-ai: lab backtests + Ollama AI gate\n"
         "Mac: dash.py monitor :8765 + lab_pull + watchdog"),
        ("h1", "2. Pain points found in the audit"),
        ("bullets", [
            "Single point of failure: 1 EC2 + free-tier DB, no failover.",
            "health.py is an 8,131-line god file - hard to change safely.",
            "No hard position-reconciliation loop (C1 orphan-position bug proved it).",
            "DB TLS unverified (CERT_NONE) - creds exposed to MITM.",
            "In-sample constants baked into the live path.",
            "execution/ never placed a real order (fake-exchange tested only).",
        ]),
        ("h1", "3. Target architecture (service-separated)"),
        ("code",
         "[collector svc] --bars/news--> [Postgres (TLS, backed up)]\n"
         "                                      |\n"
         "[decision svc] <--signals-- [analysis/lab]\n"
         "      | risk caps + reconciliation\n"
         "[execution svc] --orders--> [exchange/broker]\n"
         "      |\n"
         "[ledger svc] (append-only, source of truth) -> [dashboard]"),
        ("table", (
            ["Service", "Level", "Why"],
            [
                ["Collector", "always-on, stateless", "just ingests bars/news; restart freely"],
                ["Analysis / lab", "batch, offline", "backtests + stats; never in the live path"],
                ["Decision", "live, guarded", "applies risk caps; the only writer of intents"],
                ["Execution", "live, minimal", "turns intents into orders; reconciles positions"],
                ["Ledger", "append-only", "single source of truth; every service reads it"],
                ["Dashboard", "read-only", "observability; survives restarts (watchdog)"],
            ],
            [40*mm, 40*mm, 90*mm],
        )),
        ("h1", "4. New Indian equity engine - proposed architecture"),
        ("code",
         "Kite Connect (Zerodha)\n"
         "   | historical daily bars + live quotes (09:15-15:30 IST)\n"
         "   v\n"
         "[data lake: NSE daily bars, Nifty-50] -> [lab: backtest + stats]\n"
         "   |                                            |\n"
         "[paper engine: market-hours book] <--- signals --+\n"
         "   | risk caps (0.5-1%), reconciliation, kill switch\n"
         "   v\n"
         "[ledger: append-only] -> [dashboard + watchdog]\n"
         "   |\n"
         "[execution svc] --(PAPER by default; real only after gate)--> Kite orders"),
        ("h1", "5. What changes vs crypto"),
        ("bullets", [
            "Market-hours, not 24/7: the engine wakes 09:00-15:30 IST and idles otherwise.",
            "No funding-rate mechanics; cost model is STT/stamp/GST/DP/brokerage instead.",
            "Daily bars are the swing timeframe (4h bars are awkward inside a 6h15m session).",
            "No overnight short in cash equity; intraday auto-square-off ~15:20 must be handled.",
            "Reconciliation and the ledger are built in from day 1 (the crypto C1 bug lesson).",
            "DB TLS verified from the start; secrets in a store, never loose at repo root.",
        ]),
    ]
    build("05_architecture_changes.pdf", "Architecture Changes",
          "Crypto as-is vs target, and the proposed Indian equity engine. 8 Oct 2026.", blocks)


if __name__ == "__main__":
    pdf_test_data()
    pdf_wallet()
    pdf_pivot()
    pdf_new_plan()
    pdf_architecture()
    print("done ->", OUT.resolve())
