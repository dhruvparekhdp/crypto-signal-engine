"""Build the four wireframe artboards from one shared shell."""
NAV = [("Dashboard","M3 12h7V3H3zM14 21h7v-9h-7zM14 9h7V3h-7zM3 21h7v-6H3z"),
       ("Signals","M3 17l6-6 4 4 8-8"),
       ("Paper Trading","M3 6h18M3 12h18M3 18h12")]
NAV2 = [("Accuracy","M12 20V10M18 20V4M6 20v-4"),
        ("Historic Data","M3 5a9 3 0 1018 0 9 3 0 10-18 0M3 5v14a9 3 0 0018 0V5"),
        ("Watchlist","M12 3l2.6 5.3 5.9.9-4.3 4.1 1 5.8-5.2-2.7-5.2 2.7 1-5.8L3.5 9.2l5.9-.9z"),
        ("Diagnostics","M12 2v4M12 18v4M4.9 4.9l2.9 2.9M16.2 16.2l2.9 2.9M2 12h4M18 12h4"),
        ("Settings","M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.9l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-2.9 1.2V21a2 2 0 11-4 0v-.1A1.7 1.7 0 004.6 19l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00-1.2-2.9H.5a2 2 0 110-4h.1A1.7 1.7 0 001.8 4.6l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 002.9-1.2V.5a2 2 0 114 0v.1A1.7 1.7 0 0019.4 5z")]

def nav(items, active):
    out=[]
    for label, d in items:
        on = label==active
        bg = "background:#1e293b;border-left:2px solid #0ea5e9;" if on else "border-left:2px solid transparent;"
        col = "#f1f5f9" if on else "#94a3b8"
        stroke = "#0ea5e9" if on else "#64748b"
        out.append(f'<div style="{bg}padding:7px 16px;display:flex;align-items:center;gap:9px">'
                   f'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="{stroke}" stroke-width="2" stroke-linecap="round"><path d="{d}"/></svg>'
                   f'<span style="font-size:12px;color:{col};font-weight:{"600" if on else "400"}">{label}</span></div>')
    return "\n  ".join(out)

SIDEBAR = open("_sidebar.txt").read()

def shell(active, title, subtitle, body, actions=""):
    side = SIDEBAR.replace("__NAV__", nav(NAV, active)).replace("__NAV2__", nav(NAV2, active))
    return f'''<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <script src="./support.js"></script>
</head>
<body>
<x-dc>
<helmet>
  <style>
    body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            background:#0f172a; color:#e2e8f0; }}
    a {{ color:#38bdf8; text-decoration:none; }} a:hover {{ color:#7dd3fc; }}
  </style>
</helmet>
<div style="display:flex;width:1280px;height:900px;background:#0f172a;overflow:hidden">
{side}
  <div style="flex-grow:1;display:flex;flex-direction:column;min-width:0">
    <div style="padding:16px 24px;border-bottom:1px solid #334155;display:flex;align-items:center;gap:16px">
      <div style="min-width:0">
        <div style="font-size:17px;font-weight:600;color:#f1f5f9;letter-spacing:-.01em">{title}</div>
        <div style="font-size:11px;color:#64748b;margin-top:2px">{subtitle}</div>
      </div>
      <div style="flex-grow:1"></div>
      {actions}
    </div>
    <div style="flex-grow:1;padding:20px 24px;overflow:hidden;display:flex;flex-direction:column;gap:16px">
{body}
    </div>
  </div>
</div>
</x-dc>
<script data-dc-script data-props='{{}}'>
class Component extends DCLogic {{
  renderVals() {{ return {{}}; }}
}}
</script>
</body>
</html>
'''

def card(title, inner, note=""):
    n = f'<span style="font-size:10px;color:#475569;font-weight:400;text-transform:none;letter-spacing:0">{note}</span>' if note else ""
    return f'''<div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:10px;min-height:0">
        <div style="font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.08em;display:flex;align-items:baseline;gap:8px">{title} {n}</div>
        {inner}
      </div>'''

def kpi(label, value, sub, color="#f1f5f9"):
    return f'''<div style="background:#1e293b;border:1px solid #334155;border-radius:10px;padding:12px 14px">
          <div style="font-size:10px;color:#64748b;text-transform:uppercase;letter-spacing:.07em">{label}</div>
          <div style="font-size:21px;font-weight:600;color:{color};margin-top:3px;font-variant-numeric:tabular-nums">{value}</div>
          <div style="font-size:10px;color:#475569;margin-top:2px">{sub}</div>
        </div>'''

def btn(text, primary=False):
    if primary:
        return f'<div style="background:#0ea5e9;color:#03252f;font-size:11px;font-weight:600;padding:7px 13px;border-radius:6px">{text}</div>'
    return f'<div style="border:1px solid #334155;color:#94a3b8;font-size:11px;padding:7px 13px;border-radius:6px">{text}</div>'

def chip(text, on=False):
    if on:
        return f'<div style="background:#1e3a5f;border:1px solid #0ea5e9;color:#7dd3fc;font-size:10px;padding:4px 10px;border-radius:12px">{text}</div>'
    return f'<div style="border:1px solid #334155;color:#64748b;font-size:10px;padding:4px 10px;border-radius:12px">{text}</div>'

def table(cols, rows, widths=None):
    w = widths or ["1fr"]*len(cols)
    grid = " ".join(w)
    head = "".join(f'<div style="font-size:9px;color:#475569;text-transform:uppercase;letter-spacing:.07em;padding:0 0 7px">{c}</div>' for c in cols)
    body=""
    for r in rows:
        cells="".join(f'<div style="font-size:11px;color:{c[1] if isinstance(c,tuple) else "#cbd5e1"};padding:7px 0;border-top:1px solid #1e293b;font-variant-numeric:tabular-nums">{c[0] if isinstance(c,tuple) else c}</div>' for c in r)
        body+=cells
    return f'<div style="display:grid;grid-template-columns:{grid};align-items:center">{head}{body}</div>'

open("_lib.py","w").write("")
print("helpers ready")

# ─────────────────────────────── 1. Dashboard ───────────────────────────────
sparkline = ('<svg width="100%" height="54" viewBox="0 0 320 54" preserveAspectRatio="none">'
  '<polyline fill="none" stroke="#4ade80" stroke-width="1.5" points="0,42 27,38 53,44 80,30 107,33 133,22 160,26 187,14 213,19 240,12 267,16 293,7 320,10"/>'
  '<line x1="0" y1="47" x2="320" y2="47" stroke="#334155" stroke-width="1" stroke-dasharray="3 3"/></svg>')

dash_body = f'''      <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px">
        {kpi("Equity","₹3,412","from ₹3,000 · 17% to target","#4ade80")}
        {kpi("Signals, 7d","41","28 taken · 13 filtered out")}
        {kpi("Hit rate, 7d","—","needs outcome tracking","#64748b")}
        {kpi("Costs, 7d","₹96","15% of gross","#f87171")}
      </div>

      <div style="display:grid;grid-template-columns:1.55fr 1fr;gap:16px;min-height:0;flex-grow:1">
        <div style="display:flex;flex-direction:column;gap:16px;min-height:0">
          {card("Predictions · last 7 days", 
             '<div style="display:flex;gap:6px;flex-wrap:wrap">' + chip("All 41", True) + chip("Confluence 22") + chip("Squeeze 11") + chip("Divergence 8") + chip("Gold only") + chip("Taken") + chip("Filtered") + '</div>'
             + table(["Fired","Symbol","Setup","Dir","Move","× cost","Conf","Outcome"],
               [("2h ago","XAUUSDT","Confluence 4/5",("LONG","#4ade80"),"0.81%",("18.5×","#4ade80"),"84%",("open","#7dd3fc")),
                ("5h ago","ETHUSDT","Squeeze break",("SHORT","#f87171"),"1.24%",("10.5×","#4ade80"),"78%",("won","#4ade80")),
                ("9h ago","BTCUSDT","Confluence 3/5",("LONG","#4ade80"),"0.62%",("5.3×","#4ade80"),"71%",("lost","#f87171")),
                ("14h ago","XAUUSDT","Divergence",("SHORT","#f87171"),"0.44%",("18.6×","#4ade80"),"69%",("won","#4ade80")),
                ("1d ago","SOLUSDT","Confluence 3/5",("LONG","#4ade80"),"0.29%",("2.5×","#64748b"),"66%",("filtered","#64748b")),
                ("1d ago","ETHUSDT","Squeeze break",("LONG","#4ade80"),"0.97%",("8.2×","#4ade80"),"81%",("won","#4ade80")),
                ("2d ago","LTCUSDT","Divergence",("SHORT","#f87171"),"0.18%",("1.5×","#f87171"),"73%",("filtered","#64748b"))],
               ["78px","92px","1fr","62px","64px","64px","52px","72px"]),
             "rolls into Historic after 7d")}
        </div>

        <div style="display:flex;flex-direction:column;gap:16px;min-height:0">
          {card("Open positions",
            table(["Symbol","Side","Entry","Now","Unreal."],
              [("XAUUSDT",("LONG","#4ade80"),"4,365.9","4,401.2",("+₹128","#4ade80")),
               ("ETHUSDT",("SHORT","#f87171"),"1,906.5","1,912.4",("−₹41","#f87171"))],
              ["1fr","54px","62px","62px","62px"]))}
          {card("Equity · 7 days", sparkline)}
          {card("Why signals were refused",
            '<div style="display:flex;flex-direction:column;gap:7px">'
            + "".join(f'<div style="display:flex;align-items:center;gap:8px"><div style="height:6px;background:#334155;border-radius:3px;width:{w}px"></div><span style="font-size:10px;color:#94a3b8">{t}</span></div>'
                      for w,t in [(96,"below cost floor · 7"),(64,"only 2 of 5 agree · 4"),(40,"market too quiet · 2")])
            + '</div>')}
        </div>
      </div>'''

open("Main.dc.html","w").write(shell("Dashboard","Dashboard",
  "Last 7 days · 41 predictions · cycle #3 running",
  dash_body, btn("Last 7 days")+btn("Export",True)))
print("Main.dc.html")

# ─────────────────────────────── 2. Historic Data ───────────────────────────
hist_body = f'''      <div style="display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px">
        {kpi("Records","2,847","since 11 May 2026")}
        {kpi("Signals","1,204","after 7-day rollover")}
        {kpi("Closed trades","318","across 6 cycles")}
        {kpi("Snapshots","1,325","price + indicator")}
        {kpi("Oldest","99d","auto-purge at 180d","#64748b")}
      </div>

      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <div style="border:1px solid #334155;border-radius:6px;padding:6px 11px;font-size:11px;color:#94a3b8;display:flex;align-items:center;gap:7px">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#64748b" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
          1 Jun — 12 Aug 2026</div>
        {chip("Signals", True)}{chip("Trades")}{chip("Snapshots")}{chip("Cycles")}
        <div style="flex-grow:1"></div>
        <div style="border:1px solid #334155;border-radius:6px;padding:6px 11px;font-size:11px;color:#475569;min-width:190px">Search symbol, setup, order id…</div>
      </div>

      <div style="flex-grow:1;min-height:0">
        {card("Archived signals · 1,204 rows",
          table(["Date","Symbol","Setup","Dir","Entry","Move","× cost","Conf","Outcome","P&L"],
            [("12 Aug 14:22","XAUUSDT","Confluence 4/5",("LONG","#4ade80"),"4,312.40","0.94%","19.9×","82%",("won","#4ade80"),("+₹142","#4ade80")),
             ("12 Aug 09:07","ETHUSDT","Squeeze break",("SHORT","#f87171"),"1,884.10","1.11%","9.4×","76%",("won","#4ade80"),("+₹88","#4ade80")),
             ("11 Aug 21:44","BTCUSDT","Divergence",("LONG","#4ade80"),"61,204.0","0.71%","6.0×","70%",("lost","#f87171"),("−₹96","#f87171")),
             ("11 Aug 18:03","XAUUSDT","Confluence 3/5",("SHORT","#f87171"),"4,401.85","0.52%","22.0×","74%",("won","#4ade80"),("+₹71","#4ade80")),
             ("11 Aug 11:29","SOLUSDT","Squeeze break",("LONG","#4ade80"),"184.22","0.24%","2.0×","68%",("filtered","#64748b"),("—","#475569")),
             ("10 Aug 22:15","ETHUSDT","Confluence 4/5",("LONG","#4ade80"),"1,921.66","1.38%","11.7×","85%",("won","#4ade80"),("+₹204","#4ade80")),
             ("10 Aug 16:50","LTCUSDT","Divergence",("SHORT","#f87171"),"72.14","0.16%","1.4×","71%",("filtered","#64748b"),("—","#475569")),
             ("10 Aug 08:31","XAUUSDT","Confluence 3/5",("LONG","#4ade80"),"4,288.90","0.61%","25.8×","72%",("lost","#f87171"),("−₹64","#f87171"))],
            ["104px","88px","1fr","58px","78px","58px","58px","48px","70px","64px"])
          + '<div style="display:flex;align-items:center;gap:10px;padding-top:4px"><span style="font-size:10px;color:#475569">Showing 1–8 of 1,204</span><div style="flex-grow:1"></div>'
          + btn("‹ Prev") + btn("Next ›") + '</div>',
          "read-only archive")}
      </div>'''
open("Historic.dc.html","w").write(shell("Historic Data","Historic Data",
  "Everything older than 7 days · 2,847 records retained",
  hist_body, btn("Download CSV")+btn("Re-run analysis",True)))
print("Historic.dc.html")

# ─────────────────────────────── 3. Accuracy & Analytics ────────────────────
def bars(data, w=430, h=140, floor=None, floor_label=""):
    n=len(data); bw=w/n
    mx=max(v for _,v in data) or 1
    out=[f'<svg width="100%" height="{h+26}" viewBox="0 0 {w} {h+26}" preserveAspectRatio="none">']
    for i,(lab,v) in enumerate(data):
        bh=v/mx*(h-14)
        col = "#f87171" if (floor is not None and i<floor) else "#0ea5e9"
        out.append(f'<rect x="{i*bw+3:.1f}" y="{h-bh:.1f}" width="{bw-6:.1f}" height="{bh:.1f}" rx="2" fill="{col}"/>')
        out.append(f'<text x="{i*bw+bw/2:.1f}" y="{h+13}" fill="#475569" font-size="9" text-anchor="middle" font-family="system-ui">{lab}</text>')
    if floor is not None:
        x=floor*bw
        out.append(f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{h}" stroke="#f87171" stroke-width="1" stroke-dasharray="3 3"/>')
        out.append(f'<text x="{x+5:.1f}" y="12" fill="#f87171" font-size="9" font-family="system-ui">{floor_label}</text>')
    out.append('</svg>')
    return "".join(out)

def calib(w=430,h=150):
    pts=[(0.65,0.58),(0.70,0.66),(0.75,0.74),(0.80,0.72),(0.85,0.88),(0.90,0.83)]
    def X(v): return (v-0.60)/0.35*w
    def Y(v): return h-(v-0.40)/0.60*h
    poly=" ".join(f"{X(a):.0f},{Y(b):.0f}" for a,b in pts)
    return (f'<svg width="100%" height="{h+24}" viewBox="0 0 {w} {h+24}" preserveAspectRatio="none">'
      f'<line x1="{X(0.60):.0f}" y1="{Y(0.40):.0f}" x2="{X(0.95):.0f}" y2="{Y(0.95):.0f}" stroke="#475569" stroke-width="1" stroke-dasharray="4 4"/>'
      f'<text x="{X(0.86):.0f}" y="{Y(0.90):.0f}" fill="#475569" font-size="9" font-family="system-ui">perfect</text>'
      f'<polyline fill="none" stroke="#0ea5e9" stroke-width="2" points="{poly}"/>'
      + "".join(f'<circle cx="{X(a):.0f}" cy="{Y(b):.0f}" r="3" fill="#0ea5e9"/>' for a,b in pts)
      + "".join(f'<text x="{X(a):.0f}" y="{h+16}" fill="#475569" font-size="9" text-anchor="middle" font-family="system-ui">{int(a*100)}</text>' for a,b in pts)
      + '</svg>')

def hbars(rows,w=430):
    out=['<div style="display:flex;flex-direction:column;gap:8px">']
    mx=max(v for _,v,_ in rows) or 1
    for lab,v,n in rows:
        out.append(f'<div style="display:grid;grid-template-columns:104px 1fr 62px;align-items:center;gap:9px">'
          f'<span style="font-size:10px;color:#94a3b8">{lab}</span>'
          f'<div style="height:8px;background:#0f172a;border-radius:4px;overflow:hidden"><div style="height:8px;width:{v/mx*100:.0f}%;background:#0ea5e9;border-radius:4px"></div></div>'
          f'<span style="font-size:10px;color:#64748b;text-align:right;font-variant-numeric:tabular-nums">{v}% · {n}</span></div>')
    out.append('</div>')
    return "".join(out)

warn = ('<div style="background:#1e3a5f;border:1px solid #0ea5e9;border-radius:8px;padding:11px 13px;display:flex;gap:10px;align-items:flex-start">'
  '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#7dd3fc" stroke-width="2" style="flex:none;margin-top:1px"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>'
  '<div><div style="font-size:11px;color:#e2e8f0;font-weight:600">These charts are mock data, not the live archive</div>'
  '<div style="font-size:10px;color:#94a3b8;margin-top:3px">Signals resolve through <span style="font-family:ui-monospace,monospace">resolve_signal_outcomes</span>, but the numbers on this page are illustrative — read the real ones from <span style="font-family:ui-monospace,monospace">/audit</span>.</div></div></div>')

acc_body = f'''      {warn}
      <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;flex-grow:1;min-height:0">
        {card("Is confidence honest?","<div style='font-size:10px;color:#64748b;margin:-4px 0 2px'>Stated confidence (x) against realised win rate (y). Below the dashed line the bot is overconfident.</div>"+calib(), "the chart that matters most")}
        {card("Accuracy by setup", hbars([("Confluence 4/5",81,"n=64"),("Confluence 3/5",69,"n=142"),("Squeeze break",64,"n=88"),("Divergence",57,"n=51")]))}
        {card("Move size vs the cost floor",
          "<div style='font-size:10px;color:#64748b;margin:-4px 0 2px'>Every archived signal by how far its target sat. Red bars cannot pay for the round trip.</div>"
          + bars([("0.05",31),("0.1",44),("0.15",38),("0.2",29),("0.3",41),("0.5",36),("0.8",24),("1.2",15),("2%",7)], floor=2, floor_label="0.118% break-even"),
          "the small-move question")}
        {card("Hit rate over time",
          bars([("W1",58),("W2",61),("W3",55),("W4",67),("W5",64),("W6",72),("W7",69),("W8",74)]))}
      </div>'''
open("Accuracy.dc.html","w").write(shell("Accuracy","Accuracy & Analytics",
  "1,204 archived signals · grouped by confidence, setup and move size",
  acc_body, btn("Last 90 days")+btn("Rebuild stats",True)))
print("Accuracy.dc.html")

# ─────────────────────────────── 4. Signal Explorer ─────────────────────────
def slider(label, lo, hi, fill_from, fill_to):
    return (f'<div style="display:flex;flex-direction:column;gap:6px">'
      f'<div style="display:flex"><span style="font-size:10px;color:#94a3b8">{label}</span>'
      f'<div style="flex-grow:1"></div><span style="font-size:10px;color:#7dd3fc;font-variant-numeric:tabular-nums">{lo} — {hi}</span></div>'
      f'<div style="height:4px;background:#0f172a;border-radius:2px;position:relative">'
      f'<div style="position:absolute;left:{fill_from}%;width:{fill_to-fill_from}%;height:4px;background:#0ea5e9;border-radius:2px"></div>'
      f'<div style="position:absolute;left:{fill_from}%;top:-4px;width:12px;height:12px;border-radius:50%;background:#e2e8f0;margin-left:-6px"></div>'
      f'<div style="position:absolute;left:{fill_to}%;top:-4px;width:12px;height:12px;border-radius:50%;background:#e2e8f0;margin-left:-6px"></div>'
      f'</div></div>')

def toggle(label, on, note=""):
    knob = "right:2px;background:#0ea5e9" if on else "left:2px;background:#475569"
    track = "#1e3a5f" if on else "#0f172a"
    return (f'<div style="display:flex;align-items:center;gap:9px">'
      f'<div style="width:30px;height:17px;border-radius:9px;background:{track};border:1px solid #334155;position:relative;flex:none">'
      f'<div style="position:absolute;top:2px;{knob};width:11px;height:11px;border-radius:50%"></div></div>'
      f'<div><div style="font-size:11px;color:#cbd5e1">{label}</div>'
      + (f'<div style="font-size:9px;color:#475569">{note}</div>' if note else "") + '</div></div>')

filters = f'''<div style="width:246px;flex:none;display:flex;flex-direction:column;gap:14px">
        {card("Filters",
          '<div style="display:flex;flex-direction:column;gap:13px">'
          + slider("Confidence","65%","95%",0,86)
          + slider("Move size","0.30%","3.0%",10,100)
          + slider("× cost floor","3.0×","40×",7,100)
          + '<div style="height:1px;background:#334155"></div>'
          + toggle("Hide filtered-out signals", True, "the ones the gate refused")
          + toggle("Only where 4 of 5 agree", False)
          + toggle("No dissenting family", True, "measured: same accuracy, 4× the trades")
          + toggle("Exclude funding windows", False)
          + '</div>')}
        {card("Symbol",
          '<div style="display:flex;gap:6px;flex-wrap:wrap">' + chip("XAU", True) + chip("ETH", True) + chip("BTC") + chip("SOL") + chip("LTC") + chip("BCH") + chip("XRP") + '</div>')}
        {card("Setup",
          '<div style="display:flex;gap:6px;flex-wrap:wrap">' + chip("Confluence", True) + chip("Squeeze") + chip("Divergence") + chip("Volume") + '</div>')}
      </div>'''

results = f'''<div style="flex-grow:1;display:flex;flex-direction:column;gap:14px;min-width:0">
        <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px">
          {kpi("Matching","186","of 1,204 archived")}
          {kpi("Median move","0.74%","6.3× the cost floor","#4ade80")}
          {kpi("Would keep","86%","of gross, after fees","#4ade80")}
          {kpi("Refused","1,018","84% never fired","#64748b")}
        </div>
        {card("Matching signals",
          table(["Date","Symbol","Setup","Agree","Dir","Move","× cost","Conf","Kept"],
            [("12 Aug 14:22","XAUUSDT","Confluence","4/5",("LONG","#4ade80"),"0.94%","19.9×","82%",("95%","#4ade80")),
             ("11 Aug 18:03","XAUUSDT","Confluence","3/5",("SHORT","#f87171"),"0.52%","22.0×","74%",("95%","#4ade80")),
             ("10 Aug 22:15","ETHUSDT","Confluence","4/5",("LONG","#4ade80"),"1.38%","11.7×","85%",("91%","#4ade80")),
             ("09 Aug 13:40","ETHUSDT","Confluence","3/5",("SHORT","#f87171"),"0.88%","7.5×","77%",("87%","#4ade80")),
             ("08 Aug 20:11","XAUUSDT","Confluence","4/5",("LONG","#4ade80"),"1.04%","44.1×","88%",("98%","#4ade80")),
             ("08 Aug 09:55","ETHUSDT","Confluence","3/5",("LONG","#4ade80"),"0.61%","5.2×","71%",("81%","#4ade80")),
             ("07 Aug 17:26","XAUUSDT","Confluence","3/5",("SHORT","#f87171"),"0.47%","19.9×","73%",("95%","#4ade80"))],
            ["96px","84px","1fr","48px","56px","56px","56px","46px","50px"]))}
        {card("What this filter set would have cost",
          '<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px">'
          + '<div><div style="font-size:10px;color:#64748b">Gross captured</div><div style="font-size:16px;color:#4ade80;font-weight:600;margin-top:2px">₹4,180</div></div>'
          + '<div><div style="font-size:10px;color:#64748b">Fees + funding</div><div style="font-size:16px;color:#f87171;font-weight:600;margin-top:2px">₹585</div></div>'
          + '<div><div style="font-size:10px;color:#64748b">Net kept</div><div style="font-size:16px;color:#e2e8f0;font-weight:600;margin-top:2px">₹3,595</div></div>'
          + '</div>', "same maths as the live engine")}
      </div>'''

open("Explorer.dc.html","w").write(shell("Signals","Signal Explorer",
  "186 of 1,204 signals match · filters apply to the whole archive",
  f'<div style="display:flex;gap:16px;flex-grow:1;min-height:0">{filters}{results}</div>',
  btn("Reset")+btn("Save as preset",True)))
print("Explorer.dc.html")

# ─────────────────────────── 5. Session Guard ───────────────────────────────
def flag(level, title, detail, evidence):
    col = {"red":"#f87171","amber":"#fbbf24","ok":"#4ade80"}[level]
    bg  = {"red":"#3f1d1d","amber":"#3a2f14","ok":"#14532d"}[level]
    return (f'<div style="border:1px solid {col};background:{bg};border-radius:8px;padding:11px 13px;display:flex;gap:10px">'
      f'<div style="width:3px;background:{col};border-radius:2px;flex:none"></div>'
      f'<div style="min-width:0"><div style="font-size:11px;font-weight:600;color:#f1f5f9">{title}</div>'
      f'<div style="font-size:10px;color:#cbd5e1;margin-top:3px">{detail}</div>'
      f'<div style="font-size:9px;color:#94a3b8;margin-top:5px;font-variant-numeric:tabular-nums">{evidence}</div></div></div>')

def trend(label, vals, unit, good_dir):
    n=len(vals); w=150; h=34
    mx=max(vals) or 1
    pts=" ".join(f"{i/(n-1)*w:.0f},{h-(v/mx*h):.0f}" for i,v in enumerate(vals))
    col = "#f87171" if good_dir=="down" else "#4ade80"
    return (f'<div style="display:flex;align-items:center;gap:12px">'
      f'<div style="width:118px"><div style="font-size:10px;color:#94a3b8">{label}</div>'
      f'<div style="font-size:9px;color:#475569">{vals[0]}{unit} → {vals[-1]}{unit}</div></div>'
      f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}"><polyline fill="none" stroke="{col}" stroke-width="1.5" points="{pts}"/></svg></div>')

guard_body = f'''      <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px">
        {kpi("Trades today","3","2 in the last 12 min","#fbbf24")}
        {kpi("Size trend","+60%","₹20.3k → ₹32.4k","#f87171")}
        {kpi("Median hold","3m 43s","was 1h 53m","#f87171")}
        {kpi("Kept of gross","34%","was 94% on trade 1","#f87171")}
      </div>

      <div style="display:grid;grid-template-columns:1.35fr 1fr;gap:16px;flex-grow:1;min-height:0">
        <div style="display:flex;flex-direction:column;gap:14px;min-height:0">
          {card("Live flags",
            '<div style="display:flex;flex-direction:column;gap:9px">'
            + flag("red","Escalating size after wins",
                   "Each trade this session has been larger than the last while the captured move shrank. Fees scale with size; the edge did not.",
                   "₹20,322 → ₹28,915 → ₹32,441 · move 1.878% → 0.637% → 0.177%")
            + flag("red","Target has fallen under the cost floor",
                   "At this leverage a 20% ROE exit asks for a 0.18% move. The round trip costs 0.118%, so this trade keeps a third of what it earns.",
                   "0.177% captured · ₹38.24 fees on ₹57.53 gross · kept ₹19.29")
            + flag("amber","Direction reversed inside 20 minutes",
                   "A profitable short in XAUUSDT was closed and a long opened at the same price 19 minutes later.",
                   "closed 4363.41 at 20:49 · opened 4365.91 at 21:08")
            + flag("ok","Stop tightened three times while winning",
                   "Risk was reduced as the position recovered, never widened. This is the pattern that produced the best trade.",
                   "₹197.72 → ₹164.77 → ₹131.83")
            + '</div>')}
        </div>

        <div style="display:flex;flex-direction:column;gap:14px;min-height:0">
          {card("Session drift",
            '<div style="display:flex;flex-direction:column;gap:11px">'
            + trend("Position size", [20,29,32], "k", "down")
            + trend("Hold time", [113,1,4], "m", "down")
            + trend("× cost floor", [16,5,2], "×", "down")
            + trend("Kept of gross", [94,82,34], "%", "down")
            + '</div>', "all four moving the wrong way")}
          {card("What a 20% ROE target asks for",
            table(["Leverage","Price move","Verdict"],
              [("10×","2.000%",("fine","#4ade80")),
               ("25×","0.800%",("fine","#4ade80")),
               ("50×","0.400%",("thin","#fbbf24")),
               ("100×","0.200%",("under floor","#f87171"))],
              ["1fr","88px","84px"])
            + '<div style="font-size:10px;color:#64748b">The target is a share of margin; the fee is a share of price. They diverge as leverage rises.</div>')}
          {card("Cheaper venue for the same setup",
            table(["","Gold","Ether"],
              [("Round trip","0.024%","0.118%"),
               ("Min viable move",("0.147%","#4ade80"),("0.336%","#94a3b8")),
               ("Your win rate","3 of 3","4 of 4")],
              ["1fr","72px","72px"]))}
        </div>
      </div>'''
open("Guard.dc.html","w").write(shell("Paper Trading","Session Guard",
  "Behavioural flags from your own trade history · live",
  guard_body, btn("Mute for 1h")+btn("Rules",True)))
print("Guard.dc.html")
