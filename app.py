#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StockRecommender Web App — Flask backend"""

from dotenv import load_dotenv
load_dotenv()

import os, re, sys, json, ssl, smtplib, logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import yfinance as yf
import anthropic

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SMTP_HOST     = os.getenv("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER     = os.getenv("SMTP_USER",     "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip('"').strip("'")
EMAIL_FROM    = os.getenv("EMAIL_FROM",    "")


# ─── Scoring ──────────────────────────────────────────────────────────────────

def score_stock(s, max_pe):
    score = 0.0
    score += min(s.get("pct_from_high", 0) / 50 * 25, 25)
    score += min((s.get("analyst_upside") or 0) / 40 * 20, 20)
    score += min((s.get("roe") or 0) / 0.30 * 15, 15)
    score += max(0, (max_pe - s.get("pe_ratio", max_pe)) / max_pe * 15)
    score += min((s.get("profit_margin") or 0) / 0.20 * 10, 10)
    score += max(0, (3.0 - (s.get("pb_ratio") or 3.0)) / 3.0 * 10)
    score += min((s.get("dividend_yield") or 0) / 5 * 5, 5)
    return round(score, 1)


def passes_filters(info, pe, params):
    roe    = info.get("returnOnEquity")
    margin = info.get("profitMargins")
    de     = info.get("debtToEquity")

    # Core filters
    if pe <= 0 or pe > params["max_pe"]:
        return False, f"P/E {pe:.1f} > {params['max_pe']}"
    if roe is not None and roe < params["min_roe"]:
        return False, f"ROE {roe*100:.1f}% < {params['min_roe']*100:.0f}%"
    if margin is not None and margin < params["min_margin"]:
        return False, f"Margin {margin*100:.1f}% < {params['min_margin']*100:.0f}%"
    if de is not None and de > params["max_de"]:
        return False, f"D/E {de:.0f}% > {params['max_de']:.0f}%"

    # Optional filters (only applied when toggled ON in the UI)
    if params.get("req_dividend"):
        div = info.get("dividendYield") or 0
        if div <= 0:
            return False, "No dividend (filter active)"

    if params.get("req_eps_growth"):
        eps_g = info.get("earningsGrowth")
        if eps_g is not None and eps_g < 0:
            return False, f"Negative EPS growth {eps_g*100:.1f}% (filter active)"

    if params.get("req_analyst_up"):
        target = info.get("targetMeanPrice")
        price  = info.get("currentPrice") or info.get("regularMarketPrice")
        if target and price:
            upside = (target - price) / price * 100
            if upside < 10:
                return False, f"Analyst upside {upside:.1f}% < 10% (filter active)"
        else:
            return False, "No analyst target available (filter active)"

    return True, ""


# ─── Screener ─────────────────────────────────────────────────────────────────

def run_screener(params):
    def sse(event, data):
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"

    yield sse("status", {"msg": "Connecting to Yahoo Finance screener..."})
    tickers = []
    try:
        from yfinance import EquityQuery
        q = EquityQuery('and', [
            EquityQuery('eq',    ['region', 'us']),
            EquityQuery('is-in', ['exchange', 'NMS', 'NYQ']),
            EquityQuery('btwn',  ['trailingpe', 0.01, params["max_pe"]]),
            EquityQuery('gt',    ['intradaymarketcap', 500_000_000]),
        ])
        result  = yf.screen(q, sortField='trailingpe', sortAsc=True, size=250)
        tickers = [q['symbol'] for q in result.get('quotes', []) if q.get('symbol')]
        yield sse("status", {"msg": f"Screener found {len(tickers)} candidates. Fetching fundamentals..."})
    except Exception as e:
        yield sse("status", {"msg": f"Trying broad screen ({e})..."})
        try:
            from yfinance import EquityQuery
            q = EquityQuery('and', [
                EquityQuery('eq',    ['region', 'us']),
                EquityQuery('is-in', ['exchange', 'NMS', 'NYQ']),
                EquityQuery('gt',    ['intradaymarketcap', 1_000_000_000]),
            ])
            result  = yf.screen(q, sortField='intradaymarketcap', sortAsc=False, size=250)
            tickers = [q['symbol'] for q in result.get('quotes', []) if q.get('symbol')]
            yield sse("status", {"msg": f"Broad screen returned {len(tickers)} candidates..."})
        except Exception as e2:
            yield sse("error", {"msg": f"Screener failed: {e2}"})
            return

    if not tickers:
        yield sse("error", {"msg": "No tickers returned from Yahoo Finance."})
        return

    candidates = []
    passed = failed = 0
    total  = len(tickers)

    for i, ticker in enumerate(tickers):
        if i % 10 == 0:
            yield sse("progress", {"pct": int(i / total * 80), "msg": f"Analyzing {ticker}... ({i}/{total})"})
        try:
            info   = yf.Ticker(ticker).info
            pe     = info.get("trailingPE") or info.get("forwardPE") or 0
            price  = info.get("currentPrice") or info.get("regularMarketPrice")
            low52  = info.get("fiftyTwoWeekLow")
            high52 = info.get("fiftyTwoWeekHigh")
            if not all([pe, price, low52, high52]) or high52 <= 0 or low52 <= 0:
                continue
            ok, _ = passes_filters(info, pe, params)
            if not ok:
                failed += 1
                continue
            pct_from_high  = round((high52 - price) / high52 * 100, 1)
            pct_from_low   = round((price - low52)  / low52  * 100, 1)
            target         = info.get("targetMeanPrice")
            analyst_upside = round((target - price) / price * 100, 1) if target and price else None
            stock = {
                "ticker":         ticker,
                "company":        info.get("longName", ticker),
                "sector":         info.get("sector", "Unknown"),
                "pe_ratio":       round(pe, 1),
                "pb_ratio":       round(info.get("priceToBook"), 2)                        if info.get("priceToBook")                        else None,
                "ps_ratio":       round(info.get("priceToSalesTrailing12Months"), 2)        if info.get("priceToSalesTrailing12Months")        else None,
                "ev_ebitda":      round(info.get("enterpriseToEbitda"), 1)                  if info.get("enterpriseToEbitda")                  else None,
                "price":          round(price, 2),
                "week52_low":     round(low52, 2),
                "week52_high":    round(high52, 2),
                "pct_from_high":  pct_from_high,
                "pct_from_low":   pct_from_low,
                "roe":            round(info.get("returnOnEquity"), 4)    if info.get("returnOnEquity")    else None,
                "profit_margin":  round(info.get("profitMargins"), 4)     if info.get("profitMargins")     else None,
                "debt_to_equity": round(info.get("debtToEquity"), 1)      if info.get("debtToEquity") is not None else None,
                "eps_growth":     round(info.get("earningsGrowth") * 100, 1) if info.get("earningsGrowth") else None,
                "dividend_yield": round(info.get("dividendYield") * 100, 2)  if info.get("dividendYield")  else None,
                "market_cap_b":   round((info.get("marketCap") or 0) / 1e9, 1),
                "analyst_target": round(target, 2) if target else None,
                "analyst_upside": analyst_upside,
                "analyst_rating": round(info.get("recommendationMean"), 1) if info.get("recommendationMean") else None,
            }
            stock["score"] = score_stock(stock, params["max_pe"])
            candidates.append(stock)
            passed += 1
        except Exception as e:
            log.warning("Skip %s: %s", ticker, e)

    yield sse("progress", {"pct": 85, "msg": f"Found {passed} qualifying stocks. Generating AI analysis..."})

    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = candidates[:params["top_n"]]

    if top and ANTHROPIC_API_KEY:
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)
            def _pct(val): return f"{val*100:.0f}%" if val else "--"
            lines = []
            for s in top:
                lines.append(
                    f"- {s['ticker']} ({s['company']}, {s['sector']}): "
                    f"Score={s['score']}, P/E={s['pe_ratio']}, "
                    f"ROE={_pct(s.get('roe'))}, Margin={_pct(s.get('profit_margin'))}, "
                    f"D/E={s.get('debt_to_equity','--')}, "
                    f"Analyst upside={s.get('analyst_upside','--')}%, "
                    f"{s['pct_from_high']}% off 52w high"
                )
            summary = "\n".join(lines)
            prompt = (f"You are a value investing analyst. Today is {datetime.now().strftime('%B %d, %Y')}.\n"
                      f"These stocks passed a multi-factor screen:\n{summary}\n\n"
                      f"For each ticker write exactly 2 sentences: (1) strongest bargain reason, (2) key risk.\n"
                      f'Return ONLY valid JSON: {{"TICK": "sentence 1. sentence 2.", ...}}')
            response = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=900,
                messages=[{"role": "user", "content": prompt}])
            raw   = response.content[0].text
            clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`")
            analyses = json.loads(clean)
            for s in top:
                s["analysis"] = analyses.get(s["ticker"], "Strong multi-factor value profile.")
                s["verdict"]  = "BUY" if s["score"] >= 40 else "HOLD"
        except Exception as e:
            log.warning("Claude analysis failed: %s", e)
            for s in top:
                s.setdefault("analysis", "Meets multi-factor value criteria.")
                s.setdefault("verdict", "BUY" if s.get("score", 0) >= 40 else "HOLD")
    else:
        for s in top:
            s.setdefault("analysis", "Set ANTHROPIC_API_KEY in .env to enable AI analysis.")
            s.setdefault("verdict", "BUY" if s.get("score", 0) >= 40 else "HOLD")

    yield sse("progress", {"pct": 100, "msg": "Done!"})
    yield sse("results", {"stocks": top, "total_scanned": passed + failed, "passed": passed})


# ─── Email ────────────────────────────────────────────────────────────────────

def build_email_html(stocks, params):
    today  = datetime.now().strftime("%A, %B %d, %Y")
    buys   = sum(1 for s in stocks if s.get("verdict") == "BUY")
    holds  = len(stocks) - buys
    avg_pe = sum(s.get("pe_ratio", 0) for s in stocks) / len(stocks) if stocks else 0

    def mcell(label, value, color="#111827"):
        return (
            '<td style="text-align:center;padding:7px 5px;border-right:1px solid #f3f4f6;">'
            f'<div style="font-size:0.55rem;text-transform:uppercase;letter-spacing:0.08em;color:#9ca3af;margin-bottom:2px;">{label}</div>'
            f'<div style="font-size:0.82rem;font-weight:600;font-family:monospace;color:{color};">{value}</div></td>'
        )

    def fv(val, pre="", suf="", fb="--"):
        return f"{pre}{val}{suf}" if val is not None else fb

    cards = ""
    for s in stocks:
        is_buy   = s.get("verdict") == "BUY"
        bar_col  = "#10b981" if is_buy else "#f59e0b"
        v_bg     = "#d1fae5" if is_buy else "#fef3c7"
        v_col    = "#065f46" if is_buy else "#92400e"
        pe_col   = "#059669" if (s.get("pe_ratio") or 99) <= 15 else "#d97706"
        up_col   = "#059669" if (s.get("analyst_upside") or 0) >= 15 else "#374151"
        score    = s.get("score", 0)
        sc_col   = "#10b981" if score >= 60 else "#f59e0b" if score >= 40 else "#ef4444"
        roe_s    = f"{s['roe']*100:.0f}%"      if s.get("roe")            else "--"
        mar_s    = f"{s['profit_margin']*100:.0f}%" if s.get("profit_margin") else "--"
        div_s    = f"{s['dividend_yield']:.2f}%"    if s.get("dividend_yield") else "--"
        mcap_s   = f"${s['market_cap_b']:.1f}B"     if s.get("market_cap_b")   else "--"
        up_s     = f"+{s['analyst_upside']:.1f}%"   if s.get("analyst_upside") is not None else "--"
        cards += (
            f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:20px;overflow:hidden;">'
            f'<div style="height:3px;background:{bar_col};"></div>'
            f'<div style="padding:14px 18px 10px;border-bottom:1px solid #f3f4f6;">'
            f'<table width="100%"><tr>'
            f'<td><span style="font-family:monospace;font-size:1.1rem;font-weight:700;color:#111827;">{s["ticker"]}</span>'
            f'<div style="font-size:0.75rem;color:#6b7280;margin-top:2px;">{s["company"]} &nbsp;·&nbsp; {s["sector"]}</div></td>'
            f'<td align="right">'
            f'<span style="background:{v_bg};color:{v_col};font-size:0.62rem;font-weight:700;letter-spacing:0.1em;padding:3px 10px;border-radius:20px;text-transform:uppercase;">{s.get("verdict","--")}</span>'
            f'<div style="font-size:0.65rem;color:#9ca3af;margin-top:3px;text-align:right;">Score <strong style="color:{sc_col};">{score}</strong>/100</div>'
            f'</td></tr></table>'
            f'<div style="margin-top:8px;">'
            f'<div style="display:flex;justify-content:space-between;font-size:0.6rem;color:#9ca3af;margin-bottom:3px;"><span>StockRecommender Score</span><span style="color:{sc_col};font-weight:600;">{score}</span></div>'
            f'<div style="background:#f3f4f6;border-radius:4px;height:5px;overflow:hidden;">'
            f'<div style="background:{sc_col};height:100%;width:{min(int(score),100)}%;border-radius:4px;"></div></div></div></div>'
            f'<table width="100%" style="border-collapse:collapse;border-bottom:1px solid #f3f4f6;"><tr>'
            + mcell("P/E", fv(s.get("pe_ratio"),"","x"), pe_col)
            + mcell("P/B", fv(s.get("pb_ratio"),"","x"))
            + mcell("Price", fv(s.get("price"),"$"))
            + mcell("52W High", fv(s.get("week52_high"),"$"))
            + mcell("% off High", fv(s.get("pct_from_high"),"-","%"), "#059669")
            + mcell("Analyst Up", up_s, up_col)
            + mcell("Div Yield", div_s)
            + '</tr></table>'
            f'<table width="100%" style="border-collapse:collapse;border-bottom:1px solid #f3f4f6;"><tr>'
            + mcell("ROE", roe_s)
            + mcell("Net Margin", mar_s)
            + mcell("D/E", fv(s.get("debt_to_equity"),"","%") if s.get("debt_to_equity") is not None else "--")
            + mcell("EPS Growth", fv(s.get("eps_growth"),"+","%") if s.get("eps_growth") is not None else "--")
            + mcell("EV/EBITDA", fv(s.get("ev_ebitda"),"","x"))
            + mcell("Mkt Cap", mcap_s)
            + mcell("Target", fv(s.get("analyst_target"),"$"))
            + '</tr></table>'
            f'<div style="padding:10px 18px 14px;">'
            f'<div style="font-size:0.58rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#9ca3af;margin-bottom:4px;">AI Analysis</div>'
            f'<div style="font-size:0.8rem;color:#374151;line-height:1.65;">{s.get("analysis","")}</div>'
            f'</div></div>'
        )

    def schip(val, lbl, col="#3b82f6"):
        return (
            f'<td style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:10px 14px;text-align:center;">'
            f'<div style="font-size:1.2rem;font-weight:800;color:{col};line-height:1;">{val}</div>'
            f'<div style="font-size:0.6rem;text-transform:uppercase;letter-spacing:0.08em;color:#9ca3af;margin-top:2px;">{lbl}</div></td>'
        )

    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        '<title>StockRecommender Report</title></head>'
        '<body style="margin:0;padding:0;background:#f1f5f9;font-family:\'Segoe UI\',Arial,sans-serif;">'
        '<div style="max-width:700px;margin:0 auto;padding:24px 16px;">'
        '<div style="background:linear-gradient(135deg,#0f172a,#1e293b);border-radius:14px;padding:26px 32px;margin-bottom:18px;text-align:center;">'
        '<div style="font-size:1.7rem;font-weight:800;color:#22d3ee;letter-spacing:-0.5px;">Stock<span style="color:#e2e8f0;">Recommender</span></div>'
        '<div style="font-size:0.68rem;letter-spacing:0.15em;text-transform:uppercase;color:#64748b;margin-top:4px;">Multi-Factor Bargain Stock Report</div>'
        f'<div style="font-size:0.78rem;color:#94a3b8;margin-top:8px;">{today}</div>'
        f'<div style="font-size:0.7rem;color:#475569;margin-top:6px;">'
        f'P/E &lt;= {params["max_pe"]} &nbsp;|&nbsp; ROE &gt;= {params["min_roe"]*100:.0f}% &nbsp;|&nbsp; '
        f'Margin &gt;= {params["min_margin"]*100:.0f}% &nbsp;|&nbsp; D/E &lt;= {params["max_de"]:.0f}%</div></div>'
        f'<table width="100%" style="border-collapse:separate;border-spacing:8px;margin-bottom:18px;"><tr>'
        + schip(str(len(stocks)), "Stocks Found")
        + schip(str(buys), "BUY Signals", "#10b981")
        + schip(str(holds), "HOLD / Watch", "#f59e0b")
        + schip(f"{avg_pe:.1f}x", "Avg P/E")
        + '</tr></table>'
        '<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:9px 14px;font-size:0.71rem;color:#92400e;margin-bottom:18px;">'
        '<strong>Disclaimer:</strong> For informational purposes only. Not financial advice. Always verify independently.</div>'
        + cards +
        f'<div style="text-align:center;font-size:0.67rem;color:#94a3b8;border-top:1px solid #e2e8f0;padding-top:14px;">'
        f'StockRecommender &middot; {datetime.now().strftime("%Y-%m-%d %H:%M")} &middot; Yahoo Finance + Claude AI</div>'
        '</div></body></html>'
    )


def send_report_email(to_email, stocks, params):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_FROM:
        return False, "SMTP not configured in .env (SMTP_USER, SMTP_PASSWORD, EMAIL_FROM)"
    today   = datetime.now().strftime("%b %d, %Y")
    subject = f"StockRecommender Daily Report -- {len(stocks)} Bargain Stock Picks -- {today}"
    html    = build_email_html(stocks, params)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = to_email
    msg.attach(MIMEText(f"StockRecommender Report -- {today}. View in HTML email client.", "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as server:
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(EMAIL_FROM, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.ehlo(); server.starttls(context=ctx); server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(EMAIL_FROM, [to_email], msg.as_string())
        log.info("Email sent to %s", to_email)
        return True, "Email sent successfully!"
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP authentication failed. Check SMTP_USER and SMTP_PASSWORD in .env"
    except Exception as e:
        return False, str(e)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/screen")
def screen():
    params = {
        "max_pe":         float(request.args.get("max_pe",     20)),
        "min_roe":        float(request.args.get("min_roe",     8)) / 100,
        "min_margin":     float(request.args.get("min_margin",  3)) / 100,
        "max_de":         float(request.args.get("max_de",    150)),
        "top_n":          int(request.args.get("top_n",         6)),
        "req_dividend":   request.args.get("req_dividend",   "0") == "1",
        "req_eps_growth": request.args.get("req_eps_growth", "0") == "1",
        "req_analyst_up": request.args.get("req_analyst_up", "0") == "1",
    }
    def generate():
        for chunk in run_screener(params):
            yield chunk
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/send_email", methods=["POST"])
def send_email_route():
    data     = request.get_json()
    to_email = (data.get("email") or "").strip()
    stocks   = data.get("stocks", [])
    raw_p    = data.get("params", {})
    if not to_email or "@" not in to_email:
        return jsonify({"success": False, "msg": "Please enter a valid email address."})
    if not stocks:
        return jsonify({"success": False, "msg": "No stocks to send. Run the screen first."})
    params = {
        "max_pe":         float(raw_p.get("max_pe", 20)),
        "min_roe":        float(raw_p.get("min_roe", 0.08)),
        "min_margin":     float(raw_p.get("min_margin", 0.03)),
        "max_de":         float(raw_p.get("max_de", 150)),
        "top_n":          int(raw_p.get("top_n", 6)),
        "req_dividend":   bool(raw_p.get("req_dividend", 0)),
        "req_eps_growth": bool(raw_p.get("req_eps_growth", 0)),
        "req_analyst_up": bool(raw_p.get("req_analyst_up", 0)),
    }
    ok, msg = send_report_email(to_email, stocks, params)
    return jsonify({"success": ok, "msg": msg})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
