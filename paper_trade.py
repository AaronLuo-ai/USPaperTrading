#!/usr/bin/env python3
"""
Daily US Stock Paper Trader — cloud-safe version
Runs on GitHub Actions after market close every weekday.
Results committed back to the repo as HTML reports.
"""

import json
import math
import sys
from datetime import datetime, date
from pathlib import Path

import yfinance as yf

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
PORTFOLIO   = BASE_DIR / "portfolio.json"
REPORTS_DIR = BASE_DIR / "reports"

WATCHLIST = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA",
    # Semis
    "AMD", "AVGO", "QCOM",
    # Financials
    "JPM", "GS", "V",
    # Healthcare / Consumer
    "UNH", "JNJ", "WMT", "COST",
    # Energy
    "XOM", "CVX",
    # Index ETFs
    "SPY", "QQQ", "DIA",
]

BUY_THRESHOLD  =  0.40
SELL_THRESHOLD = -0.25
POSITION_PCT   =  0.05
MAX_POSITIONS  =  10

# ── Portfolio helpers ─────────────────────────────────────────────────────────

def load_portfolio():
    with open(PORTFOLIO) as f:
        return json.load(f)

def save_portfolio(p):
    with open(PORTFOLIO, "w") as f:
        json.dump(p, f, indent=2, default=str)

def current_price(ticker):
    try:
        return round(float(yf.Ticker(ticker).fast_info.last_price), 4)
    except Exception:
        return 0.0

def portfolio_value(p):
    total = p["cash"]
    for ticker, pos in p["holdings"].items():
        price = current_price(ticker)
        total += price * pos["shares"]
    return round(total, 2)

# ── Market data ───────────────────────────────────────────────────────────────

def get_market_snapshot():
    symbols = {
        "S&P 500":   "^GSPC",
        "Nasdaq":    "^IXIC",
        "Dow Jones": "^DJI",
        "VIX":       "^VIX",
        "10Y Yield": "^TNX",
        "Gold":      "GLD",
        "Oil (WTI)": "CL=F",
    }
    result = {}
    for label, sym in symbols.items():
        try:
            fi  = yf.Ticker(sym).fast_info
            pct = round((fi.last_price - fi.previous_close) / fi.previous_close * 100, 2) \
                  if fi.previous_close else 0
            result[label] = {"price": round(float(fi.last_price), 2), "pct": pct}
        except Exception:
            result[label] = {"price": 0.0, "pct": 0.0}
    return result

def get_news():
    news_items = []
    for symbol in ["^GSPC", "^DJI", "^IXIC", "GLD", "TLT"]:
        try:
            for n in (yf.Ticker(symbol).news or [])[:3]:
                content = n.get("content") or {}
                title   = content.get("title") or n.get("title", "")
                summary = content.get("summary") or ""
                pub     = content.get("pubDate") or n.get("providerPublishTime", "")
                link    = (content.get("canonicalUrl") or {}).get("url", "") \
                          if isinstance(content.get("canonicalUrl"), dict) else ""
                if title:
                    news_items.append({"title": title, "summary": summary,
                                       "link": link, "symbol": symbol,
                                       "published": str(pub)[:16]})
        except Exception:
            pass
    seen, unique = set(), []
    for item in news_items:
        if item["title"] not in seen:
            seen.add(item["title"])
            unique.append(item)
    return unique[:15]

# ── Analysis ──────────────────────────────────────────────────────────────────

def analyze_stocks(tickers):
    results = {}
    for ticker in tickers:
        try:
            t     = yf.Ticker(ticker)
            hist  = t.history(period="60d")
            if hist.empty:
                continue
            fi    = t.fast_info
            price = round(float(fi.last_price), 2)
            prev  = float(fi.previous_close) if fi.previous_close else price
            pct   = round((price - prev) / prev * 100, 2) if prev else 0

            closes = hist["Close"]
            vol    = hist["Volume"]
            ma20   = closes.rolling(20).mean().iloc[-1]
            ma50   = closes.rolling(50).mean().iloc[-1]
            va     = vol.rolling(20).mean().iloc[-1]
            vt     = vol.iloc[-1]

            delta = closes.diff()
            gain  = delta.clip(lower=0).rolling(14).mean().iloc[-1]
            loss  = (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
            rsi   = 100 - (100 / (1 + gain / loss)) if loss else 50

            score = 0.0
            if price > ma20:        score += 0.20
            if price > ma50:        score += 0.20
            if ma20 > ma50:         score += 0.15
            if rsi < 70:            score += 0.10
            if rsi < 60:            score += 0.05
            if rsi > 30:            score += 0.05
            if vt > va * 1.2:       score += 0.10
            score += min(pct * 0.02, 0.10) if pct > 0 else 0
            if pct < -2:            score -= 0.20
            if rsi > 75:            score -= 0.25
            score = max(-1.0, min(1.0, round(score, 3)))

            signal = "BUY" if score >= BUY_THRESHOLD else \
                     ("SELL" if score <= SELL_THRESHOLD else "HOLD")

            name = ""
            try:
                name = t.info.get("shortName", ticker)
            except Exception:
                name = ticker

            results[ticker] = {
                "score": score, "signal": signal, "price": price,
                "pct_day": pct, "rsi": round(rsi, 1),
                "ma20": round(float(ma20), 2), "ma50": round(float(ma50), 2),
                "advice": f"{signal} — RSI {rsi:.0f}, {pct:+.1f}% today, "
                          f"{'above' if price > ma20 else 'below'} 20MA",
                "name": name,
            }
        except Exception as e:
            results[ticker] = {
                "score": 0.0, "signal": "HOLD", "price": 0.0, "pct_day": 0.0,
                "rsi": 50, "ma20": 0, "ma50": 0,
                "advice": f"Error: {e}", "name": ticker,
            }
    return results

# ── Trading logic ─────────────────────────────────────────────────────────────

def execute_trades(p, analysis, today):
    trades = []
    port_val = portfolio_value(p)
    for ticker, data in analysis.items():
        price = data["price"]
        if price <= 0:
            continue
        if data["signal"] == "BUY" and ticker not in p["holdings"]:
            if len(p["holdings"]) >= MAX_POSITIONS:
                continue
            shares = math.floor(port_val * POSITION_PCT / price)
            cost   = round(shares * price, 2)
            if shares > 0 and p["cash"] >= cost:
                p["cash"] -= cost
                p["holdings"][ticker] = {
                    "shares": shares, "avg_cost": price,
                    "buy_date": today, "buy_price": price,
                }
                trade = {"date": today, "action": "BUY", "ticker": ticker,
                         "shares": shares, "price": price, "total": cost,
                         "reason": data["advice"]}
                trades.append(trade)
                p["trade_history"].append(trade)

        elif data["signal"] == "SELL" and ticker in p["holdings"]:
            pos      = p["holdings"][ticker]
            proceeds = round(pos["shares"] * price, 2)
            pnl      = round(proceeds - pos["shares"] * pos["avg_cost"], 2)
            p["cash"] += proceeds
            trade = {"date": today, "action": "SELL", "ticker": ticker,
                     "shares": pos["shares"], "price": price,
                     "total": proceeds, "pnl": pnl, "reason": data["advice"]}
            trades.append(trade)
            p["trade_history"].append(trade)
            del p["holdings"][ticker]
    return trades

# ── HTML report ───────────────────────────────────────────────────────────────

def clr(v, pos="#27ae60", neg="#e74c3c", neu="#555"):
    return pos if v > 0 else (neg if v < 0 else neu)

def badge(sig):
    c = {"BUY": "#27ae60", "SELL": "#e74c3c", "HOLD": "#f39c12"}.get(sig, "#555")
    return f'<span style="background:{c};color:#fff;padding:2px 9px;border-radius:12px;font-size:.75rem;font-weight:700">{sig}</span>'

def pct_html(v):
    c = "#27ae60" if v >= 0 else "#e74c3c"
    a = "▲" if v > 0 else ("▼" if v < 0 else "")
    return f'<span style="color:{c};font-weight:600">{a}{abs(v):.2f}%</span>'

def generate_html(today, p_before, p_after, trades, analysis, market, news):
    val_b  = portfolio_value(p_before)
    val_a  = portfolio_value(p_after)
    t_pnl  = round(val_a - p_after["initial_capital"], 2)
    t_pct  = round(t_pnl / p_after["initial_capital"] * 100, 2)
    d_pnl  = round(val_a - val_b, 2)
    d_pct  = round(d_pnl / val_b * 100, 2) if val_b else 0

    mkt_rows = "".join(
        f'<tr><td>{l}</td><td>{d["price"]:,.2f}</td>'
        f'<td style="color:{clr(d["pct"])};font-weight:600">'
        f'{"▲" if d["pct"]>0 else "▼" if d["pct"]<0 else ""}{abs(d["pct"]):.2f}%</td></tr>'
        for l, d in market.items()
    )

    if trades:
        trade_rows = "".join(
            f'<tr><td>{badge(t["action"])}</td><td><strong>{t["ticker"]}</strong></td>'
            f'<td>{t["shares"]} @ ${t["price"]:,.2f}</td><td>${t["total"]:,.2f}'
            f'{"<br><small style=color:" + clr(t["pnl"]) + ">P&L: $" + f"{t["pnl"]:+,.2f}" + "</small>" if "pnl" in t else ""}'
            f'</td><td style="font-size:.8rem;color:#555">{t["reason"][:110]}</td></tr>'
            for t in trades
        )
    else:
        trade_rows = '<tr><td colspan="5" style="text-align:center;color:#888;padding:20px">No trades today — all signals HOLD</td></tr>'

    if p_after["holdings"]:
        hold_rows = ""
        for tk, pos in p_after["holdings"].items():
            pr   = current_price(tk)
            cost = pos["avg_cost"] * pos["shares"]
            val  = pr * pos["shares"]
            pnl  = round(val - cost, 2)
            pp   = round(pnl / cost * 100, 2) if cost else 0
            a    = analysis.get(tk, {})
            hold_rows += (
                f'<tr><td><strong>{tk}</strong><br><small style="color:#777">{a.get("name","")}</small></td>'
                f'<td>{pos["shares"]}</td><td>${pos["avg_cost"]:,.2f}</td>'
                f'<td>${pr:,.2f}</td><td>${val:,.2f}</td>'
                f'<td style="color:{clr(pnl)};font-weight:600">${pnl:+,.2f}<br>'
                f'<small>{pct_html(pp)}</small></td>'
                f'<td>{badge(a.get("signal","HOLD"))}</td></tr>'
            )
    else:
        hold_rows = '<tr><td colspan="7" style="text-align:center;color:#888;padding:20px">No open positions</td></tr>'

    ana_rows = "".join(
        f'<tr><td><strong>{tk}</strong><br><small style="color:#777">{d.get("name","")}</small></td>'
        f'<td>{badge(d["signal"])}</td><td>{pct_html(d["pct_day"])}</td>'
        f'<td><div style="background:#eee;border-radius:4px;height:10px;width:100px;display:inline-block">'
        f'<div style="background:{"#27ae60" if d["score"]>=0 else "#e74c3c"};height:10px;border-radius:4px;'
        f'width:{int(abs(d["score"])*100)}px"></div></div> <small>{d["score"]:+.2f}</small></td>'
        f'<td>RSI {d.get("rsi","-")}</td>'
        f'<td style="font-size:.8rem;color:#444">{d.get("advice","")[:100]}</td></tr>'
        for tk, d in sorted(analysis.items(), key=lambda x: -x[1]["score"])
    )

    news_html = "".join(
        f'<div style="border-left:3px solid #1a73e8;padding:8px 12px;margin:8px 0;background:#f8f9ff">'
        f'<div style="font-weight:600;font-size:.9rem">'
        f'{"<a href=" + repr(n["link"]) + " target=_blank style=color:#1a73e8;text-decoration:none>" + n["title"] + "</a>" if n.get("link") else n["title"]}'
        f'</div>'
        f'{"<div style=color:#555;font-size:.82rem;margin-top:3px>" + n["summary"][:200] + "...</div>" if n.get("summary") else ""}'
        f'<div style="font-size:.72rem;color:#aaa;margin-top:3px">{n.get("published","")} · {n.get("symbol","")}</div>'
        f'</div>'
        for n in news
    ) or '<p style="color:#888">No news available.</p>'

    hist_rows = "".join(
        f'<tr><td>{t["date"]}</td><td>{badge(t["action"])}</td>'
        f'<td><strong>{t["ticker"]}</strong></td>'
        f'<td>{t["shares"]} @ ${t["price"]:,.2f}</td><td>${t["total"]:,.2f}</td>'
        f'<td style="color:{clr(t.get("pnl",0))};font-weight:600">'
        f'{"$" + f"{t["pnl"]:+,.2f}" if "pnl" in t else "—"}</td></tr>'
        for t in reversed(p_after["trade_history"][-30:])
    ) or '<tr><td colspan="6" style="text-align:center;color:#888;padding:20px">No history yet</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paper Trade Report — {today}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f6f8;color:#1a1a2e;padding:20px}}
h2{{font-size:1.05rem;font-weight:600;color:#333;margin:22px 0 10px;border-bottom:2px solid #e0e0e0;padding-bottom:6px}}
.card{{background:#fff;border-radius:12px;padding:20px;margin:14px 0;box-shadow:0 2px 8px rgba(0,0,0,.06)}}
.kpis{{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0}}
.kpi{{flex:1;min-width:140px;background:#fff;border-radius:12px;padding:14px 18px;box-shadow:0 2px 8px rgba(0,0,0,.06);text-align:center}}
.kpi .v{{font-size:1.5rem;font-weight:700}}
.kpi .l{{font-size:.72rem;color:#888;margin-top:3px;text-transform:uppercase;letter-spacing:.05em}}
table{{width:100%;border-collapse:collapse;font-size:.86rem}}
th{{text-align:left;padding:7px 11px;background:#f0f2f5;border-bottom:2px solid #ddd;font-weight:600;font-size:.76rem;text-transform:uppercase;letter-spacing:.04em}}
td{{padding:7px 11px;border-bottom:1px solid #eee;vertical-align:middle}}
tr:hover td{{background:#fafafa}}
footer{{text-align:center;color:#aaa;font-size:.76rem;margin-top:28px}}
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;flex-wrap:wrap;gap:8px">
  <div>
    <div style="font-size:1.6rem;font-weight:700">📈 Daily Paper Trade Report</div>
    <div style="color:#888;font-size:.88rem;margin-top:3px">{today} &nbsp;·&nbsp; US Markets &nbsp;·&nbsp; Paper Trading</div>
  </div>
  <div style="text-align:right">
    <div style="font-size:1.3rem;font-weight:700;color:{clr(t_pnl)}">{"+$" if t_pnl>=0 else "-$"}{abs(t_pnl):,.2f}</div>
    <div style="font-size:.78rem;color:#888">Total P&L since inception ({t_pct:+.2f}%)</div>
  </div>
</div>

<div class="kpis">
  <div class="kpi"><div class="v">${val_a:,.2f}</div><div class="l">Portfolio Value</div></div>
  <div class="kpi"><div class="v" style="color:{clr(t_pnl)}">{"+$" if t_pnl>=0 else "-$"}{abs(t_pnl):,.2f}</div><div class="l">Total P&L ({t_pct:+.2f}%)</div></div>
  <div class="kpi"><div class="v" style="color:{clr(d_pnl)}">{"+$" if d_pnl>=0 else "-$"}{abs(d_pnl):,.2f}</div><div class="l">Today's P&L ({d_pct:+.2f}%)</div></div>
  <div class="kpi"><div class="v">${p_after["cash"]:,.2f}</div><div class="l">Cash</div></div>
  <div class="kpi"><div class="v">{len(p_after["holdings"])}</div><div class="l">Open Positions</div></div>
  <div class="kpi"><div class="v">{len([t for t in trades if t["action"]=="BUY"])} / {len([t for t in trades if t["action"]=="SELL"])}</div><div class="l">Buys / Sells Today</div></div>
</div>

<div class="card"><h2>🌍 Market Snapshot</h2>
<table><thead><tr><th>Index</th><th>Price</th><th>Change</th></tr></thead><tbody>{mkt_rows}</tbody></table></div>

<div class="card"><h2>🔄 Today's Trades</h2>
<table><thead><tr><th>Action</th><th>Ticker</th><th>Size</th><th>Value / P&L</th><th>Reason</th></tr></thead><tbody>{trade_rows}</tbody></table></div>

<div class="card"><h2>💼 Current Holdings</h2>
<table><thead><tr><th>Stock</th><th>Shares</th><th>Avg Cost</th><th>Price</th><th>Value</th><th>P&L</th><th>Signal</th></tr></thead><tbody>{hold_rows}</tbody></table></div>

<div class="card"><h2>📊 Watchlist Analysis</h2>
<table><thead><tr><th>Stock</th><th>Signal</th><th>Today</th><th>Score</th><th>RSI</th><th>Analysis</th></tr></thead><tbody>{ana_rows}</tbody></table></div>

<div class="card"><h2>📰 Market &amp; Economic News</h2>{news_html}</div>

<div class="card"><h2>📜 Trade History (Last 30)</h2>
<table><thead><tr><th>Date</th><th>Action</th><th>Ticker</th><th>Size</th><th>Total</th><th>P&L</th></tr></thead><tbody>{hist_rows}</tbody></table></div>

<footer>Generated {datetime.now().strftime("%Y-%m-%d %H:%M UTC")} &nbsp;·&nbsp; Starting capital: $100,000 &nbsp;·&nbsp; Paper trading only — not financial advice</footer>
</body></html>"""

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = date.today().isoformat()
    REPORTS_DIR.mkdir(exist_ok=True)

    print(f"Loading portfolio...")
    p_before = load_portfolio()
    import copy
    p_after = copy.deepcopy(p_before)

    print(f"Market snapshot...")
    market = get_market_snapshot()

    print(f"Analyzing {len(WATCHLIST)} stocks...")
    analysis = analyze_stocks(WATCHLIST)

    print(f"Executing paper trades...")
    trades = execute_trades(p_after, analysis, today)

    print(f"Fetching news...")
    news = get_news()

    print(f"Writing report...")
    html = generate_html(today, p_before, p_after, trades, analysis, market, news)

    report_path = REPORTS_DIR / f"{today}_report.html"
    report_path.write_text(html)
    save_portfolio(p_after)

    val = portfolio_value(p_after)
    pnl = round(val - p_after["initial_capital"], 2)
    print(f"Done. Portfolio: ${val:,.2f} | P&L: ${pnl:+,.2f} | Trades: {len(trades)} | Holdings: {len(p_after['holdings'])}")
    print(f"Report: {report_path}")

if __name__ == "__main__":
    main()
