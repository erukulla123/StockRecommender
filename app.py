#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StockRecommender Web App — BUY + SELL recommendations"""

from dotenv import load_dotenv
load_dotenv()

import os, re, sys, json, ssl, smtplib, logging, threading, uuid
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_login import login_required, current_user
from auth import db, login_manager, auth as auth_blueprint, User
import yfinance as yf
import anthropic

app = Flask(__name__)

# Auth + database config
app.config["SECRET_KEY"]                  = os.getenv("SECRET_KEY", "change-me-in-production")
app.config["SQLALCHEMY_DATABASE_URI"]     = os.getenv("DATABASE_URL", "sqlite:///stockrecommender.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
login_manager.init_app(app)
login_manager.login_view    = "auth.login"
login_manager.login_message = "Please sign in to use StockRecommender."
app.register_blueprint(auth_blueprint)

with app.app_context():
    db.create_all()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SMTP_HOST     = os.getenv("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER     = os.getenv("SMTP_USER",     "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip('"').strip("'")
EMAIL_FROM    = os.getenv("EMAIL_FROM",    "")

jobs = {}


# ─── BUY scoring (0-100) ─────────────────────────────────────────────────────

def score_buy(s, max_pe):
    score = 0.0
    score += min(s.get("pct_from_high", 0) / 50 * 25, 25)
    score += min((s.get("analyst_upside") or 0) / 40 * 20, 20)
    score += min((s.get("roe") or 0) / 0.30 * 15, 15)
    score += max(0, (max_pe - s.get("pe_ratio", max_pe)) / max_pe * 15)
    score += min((s.get("profit_margin") or 0) / 0.20 * 10, 10)
    score += max(0, (3.0 - (s.get("pb_ratio") or 3.0)) / 3.0 * 10)
    score += min((s.get("dividend_yield") or 0) / 5 * 5, 5)
    return round(score, 1)


# ─── SELL risk score (0-100, higher = more dangerous) ────────────────────────

def score_sell_risk(s):
    """Higher score = stronger SELL signal."""
    risk = 0.0
    # High P/E (overvalued) — up to 25 pts
    pe = s.get("pe_ratio") or 0
    if pe > 50:   risk += 25
    elif pe > 35: risk += 18
    elif pe > 25: risk += 10

    # Near 52-week HIGH (overbought) — up to 20 pts
    pct_from_high = s.get("pct_from_high", 100)
    if pct_from_high < 5:   risk += 20
    elif pct_from_high < 10: risk += 12
    elif pct_from_high < 15: risk += 6

    # Negative EPS growth — up to 20 pts
    eps_g = s.get("eps_growth")
    if eps_g is not None:
        if eps_g < -20:  risk += 20
        elif eps_g < -5: risk += 12
        elif eps_g < 0:  risk += 6

    # High debt — up to 15 pts
    de = s.get("debt_to_equity")
    if de is not None:
        if de > 300:   risk += 15
        elif de > 200: risk += 10
        elif de > 150: risk += 5

    # Analyst downside (target below current price) — up to 15 pts
    upside = s.get("analyst_upside")
    if upside is not None:
        if upside < -15:  risk += 15
        elif upside < -5: risk += 10
        elif upside < 0:  risk += 5

    # Negative profit margin — up to 5 pts
    margin = s.get("profit_margin")
    if margin is not None and margin < 0:
        risk += 5

    return round(risk, 1)


# ─── Filters ─────────────────────────────────────────────────────────────────

def passes_buy_filters(info, pe, params):
    roe    = info.get("returnOnEquity")
    margin = info.get("profitMargins")
    de     = info.get("debtToEquity")
    price  = info.get("currentPrice") or info.get("regularMarketPrice") or 0
    if pe <= 0 or pe > params["max_pe"]:          return False
    if roe    is not None and roe    < params["min_roe"]:    return False
    if margin is not None and margin < params["min_margin"]: return False
    if de     is not None and de     > params["max_de"]:     return False
    # Price range filter
    if price < params.get("price_min", 0):   return False
    if price > params.get("price_max", 99999): return False
    if params.get("req_dividend") and (info.get("dividendYield") or 0) <= 0: return False
    if params.get("req_eps_growth"):
        eps_g = info.get("earningsGrowth")
        if eps_g is not None and eps_g < 0: return False
    if params.get("req_analyst_up"):
        target = info.get("targetMeanPrice")
        if target and price and (target - price) / price * 100 < 10: return False
    return True


def qualifies_for_sell(info, pe, params=None):
    """Stock must show at least 2 deterioration signals to be a SELL candidate."""
    if params:
        price = info.get("currentPrice") or info.get("regularMarketPrice") or 0
        if price < params.get("price_min", 0):    return False
        if price > params.get("price_max", 99999): return False
    signals = 0
    if pe and pe > 25:                                    signals += 1
    eps_g = info.get("earningsGrowth")
    if eps_g is not None and eps_g < 0:                   signals += 1
    de = info.get("debtToEquity")
    if de is not None and de > 150:                       signals += 1
    margin = info.get("profitMargins")
    if margin is not None and margin < 0.02:              signals += 1
    target = info.get("targetMeanPrice")
    price  = info.get("currentPrice") or info.get("regularMarketPrice")
    if target and price and (target - price) / price * 100 < 0: signals += 1
    rec = info.get("recommendationMean")
    if rec is not None and rec > 3.5:                     signals += 1
    return signals >= 2


# ─── Fetch fundamentals for one ticker ───────────────────────────────────────

def fetch_stock_data(ticker):
    info   = yf.Ticker(ticker).info
    pe     = info.get("trailingPE") or info.get("forwardPE") or 0
    price  = info.get("currentPrice") or info.get("regularMarketPrice")
    low52  = info.get("fiftyTwoWeekLow")
    high52 = info.get("fiftyTwoWeekHigh")
    if not all([price, low52, high52]) or high52 <= 0 or low52 <= 0:
        return None, None, None
    target         = info.get("targetMeanPrice")
    analyst_upside = round((target - price) / price * 100, 1) if target and price else None
    stock = {
        "ticker":         ticker,
        "company":        info.get("longName", ticker),
        "sector":         info.get("sector", "Unknown"),
        "pe_ratio":       round(pe, 1) if pe else None,
        "pb_ratio":       round(info.get("priceToBook"), 2)                       if info.get("priceToBook")                       else None,
        "ps_ratio":       round(info.get("priceToSalesTrailing12Months"), 2)       if info.get("priceToSalesTrailing12Months")       else None,
        "ev_ebitda":      round(info.get("enterpriseToEbitda"), 1)                 if info.get("enterpriseToEbitda")                 else None,
        "price":          round(price, 2),
        "week52_low":     round(low52, 2),
        "week52_high":    round(high52, 2),
        "pct_from_high":  round((high52 - price) / high52 * 100, 1),
        "pct_from_low":   round((price - low52)  / low52  * 100, 1),
        "roe":            round(info.get("returnOnEquity"), 4)   if info.get("returnOnEquity")   else None,
        "profit_margin":  round(info.get("profitMargins"), 4)    if info.get("profitMargins")    else None,
        "debt_to_equity": round(info.get("debtToEquity"), 1)     if info.get("debtToEquity") is not None else None,
        "eps_growth":     round(info.get("earningsGrowth") * 100, 1) if info.get("earningsGrowth") else None,
        "dividend_yield": round(info.get("dividendYield") * 100, 2)  if info.get("dividendYield")  else None,
        "market_cap_b":   round((info.get("marketCap") or 0) / 1e9, 1),
        "analyst_target": round(target, 2) if target else None,
        "analyst_upside": analyst_upside,
        "analyst_rating": round(info.get("recommendationMean"), 1) if info.get("recommendationMean") else None,
    }
    # Fetch recent news headlines (up to 5)
    try:
        news_items = yf.Ticker(ticker).news or []
        stock["news_headlines"] = [
            n.get("content", {}).get("title", "") or n.get("title", "")
            for n in news_items[:5]
            if n.get("content", {}).get("title") or n.get("title")
        ]
    except Exception:
        stock["news_headlines"] = []

    return stock, info, pe


# ─── Claude AI analysis ───────────────────────────────────────────────────────

def get_claude_analysis(stocks, mode="buy"):
    """Ask Claude to write analysis + assign verdict using news + fundamentals."""
    if not stocks or not ANTHROPIC_API_KEY:
        for s in stocks:
            s.setdefault("analysis", "Set ANTHROPIC_API_KEY in .env to enable AI analysis.")
            s.setdefault("news_sentiment", "unknown")
            s.setdefault("verdict", "BUY" if mode == "buy" else "SELL")
        return stocks

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=90.0)

        def _pct(val): return f"{val*100:.0f}%" if val else "--"

        lines = []
        for s in stocks:
            headlines = "; ".join(s.get("news_headlines", [])) or "No recent news available"
            lines.append(
                f"- {s['ticker']} ({s['company']}, {s['sector']}): "
                f"P/E={s.get('pe_ratio','--')}, ROE={_pct(s.get('roe'))}, "
                f"Margin={_pct(s.get('profit_margin'))}, D/E={s.get('debt_to_equity','--')}, "
                f"EPS Growth={s.get('eps_growth','--')}%, "
                f"Analyst upside={s.get('analyst_upside','--')}%, "
                f"{'%.1f%% off 52w high' % s['pct_from_high'] if mode=='buy' else '%.1f%% from 52w high' % s['pct_from_high']}, "
                f"Recent news: [{headlines}]"
            )
        summary = "\n".join(lines)

        if mode == "buy":
            prompt = (
                f"You are a value investing analyst. Today is {datetime.now().strftime('%B %d, %Y')}.\n"
                f"These stocks passed a multi-factor BUY screen (low P/E, good ROE, healthy margins):\n\n"
                f"{summary}\n\n"
                f"For each ticker:\n"
                f"1. Assess the news sentiment (positive/negative/neutral)\n"
                f"2. Write 2 sentences: (a) strongest bargain reason from fundamentals, (b) key risk\n"
                f"3. Assign verdict: BUY (strong fundamentals + neutral/positive news) or HOLD (mixed signals)\n\n"
                f'Return ONLY valid JSON: {{"TICK": {{"analysis": "...", "news_sentiment": "positive|negative|neutral", "verdict": "BUY|HOLD"}}}}'
            )
        else:
            prompt = (
                f"You are a risk analyst. Today is {datetime.now().strftime('%B %d, %Y')}.\n"
                f"These stocks show fundamental deterioration signals (high P/E, declining earnings, rising debt):\n\n"
                f"{summary}\n\n"
                f"For each ticker:\n"
                f"1. Assess the news sentiment (positive/negative/neutral)\n"
                f"2. Write 2 sentences: (a) why this stock is risky based on fundamentals + news, (b) what could trigger further decline\n"
                f"3. Assign verdict: SELL (poor fundamentals + negative/mixed news) or WATCH (some concerns but not urgent)\n\n"
                f'Return ONLY valid JSON: {{"TICK": {{"analysis": "...", "news_sentiment": "positive|negative|neutral", "verdict": "SELL|WATCH"}}}}'
            )

        response = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=1200,
            messages=[{"role": "user", "content": prompt}])
        raw   = response.content[0].text
        clean = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`")
        result = json.loads(clean)

        for s in stocks:
            r = result.get(s["ticker"], {})
            if isinstance(r, dict):
                s["analysis"]       = r.get("analysis", "Analysis unavailable.")
                s["news_sentiment"] = r.get("news_sentiment", "neutral")
                s["verdict"]        = r.get("verdict", "BUY" if mode == "buy" else "SELL")
            else:
                s["analysis"]       = str(r) if r else "Analysis unavailable."
                s["news_sentiment"] = "neutral"
                s["verdict"]        = "BUY" if mode == "buy" else "SELL"

    except Exception as e:
        log.warning("Claude analysis failed: %s", e)
        for s in stocks:
            s.setdefault("analysis", "Fundamental analysis based on screener data.")
            s.setdefault("news_sentiment", "neutral")
            s.setdefault("verdict", "BUY" if mode == "buy" else "SELL")

    return stocks


# ─── Background screener job ──────────────────────────────────────────────────

def run_screener_job(job_id, params):
    def update(pct, msg):
        jobs[job_id].update({"pct": pct, "msg": msg})

    try:
        update(3, "Connecting to Yahoo Finance screener...")
        tickers = []
        try:
            from yfinance import EquityQuery
            # For BUY: screen low P/E stocks
            q = EquityQuery('and', [
                EquityQuery('eq',    ['region', 'us']),
                EquityQuery('is-in', ['exchange', 'NMS', 'NYQ']),
                EquityQuery('gt',    ['intradaymarketcap', 500_000_000]),
            ])
            result  = yf.screen(q, sortField='intradaymarketcap', sortAsc=False, size=300)
            tickers = [q['symbol'] for q in result.get('quotes', []) if q.get('symbol')]
            update(8, f"Screener returned {len(tickers)} candidates. Analyzing...")
        except Exception as e:
            update(5, f"Using fallback screen...")
            try:
                from yfinance import EquityQuery
                q = EquityQuery('and', [
                    EquityQuery('eq',    ['region', 'us']),
                    EquityQuery('is-in', ['exchange', 'NMS', 'NYQ']),
                    EquityQuery('gt',    ['intradaymarketcap', 1_000_000_000]),
                ])
                result  = yf.screen(q, sortField='intradaymarketcap', sortAsc=False, size=200)
                tickers = [q['symbol'] for q in result.get('quotes', []) if q.get('symbol')]
            except Exception as e2:
                jobs[job_id].update({"status": "error", "msg": f"Screener failed: {e2}"})
                return

        if not tickers:
            jobs[job_id].update({"status": "error", "msg": "No tickers returned."})
            return

        buy_candidates  = []
        sell_candidates = []
        total = len(tickers)

        for i, ticker in enumerate(tickers):
            if i % 10 == 0:
                update(int(8 + i / total * 72), f"Analyzing {ticker}... ({i}/{total})")
            try:
                stock, info, pe = fetch_stock_data(ticker)
                if stock is None:
                    continue

                # BUY check
                if pe and passes_buy_filters(info, pe, params):
                    stock["buy_score"] = score_buy(stock, params["max_pe"])
                    buy_candidates.append(stock)

                # SELL check (separate — same stock can appear in both if fundamentals are mixed)
                elif qualifies_for_sell(info, pe, params):
                    stock["sell_risk"] = score_sell_risk(stock)
                    sell_candidates.append(stock)

            except Exception as e:
                log.warning("Skip %s: %s", ticker, e)

        update(82, f"Found {len(buy_candidates)} BUY candidates, {len(sell_candidates)} SELL candidates. Running AI analysis...")

        # Sort and take top N
        buy_candidates.sort(key=lambda x: x.get("buy_score", 0), reverse=True)
        sell_candidates.sort(key=lambda x: x.get("sell_risk", 0), reverse=True)
        top_buys  = buy_candidates[:params["top_n"]]
        top_sells = sell_candidates[:params["top_n"]]

        # Claude analysis for both lists
        update(88, "Analyzing BUY candidates with AI + news...")
        top_buys  = get_claude_analysis(top_buys,  mode="buy")

        update(94, "Analyzing SELL candidates with AI + news...")
        top_sells = get_claude_analysis(top_sells, mode="sell")

        jobs[job_id].update({
            "status": "done",
            "pct":    100,
            "msg":    "Done!",
            "result": {
                "buy_stocks":     top_buys,
                "sell_stocks":    top_sells,
                "total_scanned":  total,
                "buy_count":      len(buy_candidates),
                "sell_count":     len(sell_candidates),
            }
        })

    except Exception as e:
        log.exception("Job %s failed: %s", job_id, e)
        jobs[job_id].update({"status": "error", "msg": str(e)})


# ─── Email ────────────────────────────────────────────────────────────────────

def mcell(label, value, color="#111827"):
    return (
        '<td style="text-align:center;padding:7px 5px;border-right:1px solid #f3f4f6;">'
        f'<div style="font-size:0.55rem;text-transform:uppercase;letter-spacing:0.08em;color:#9ca3af;margin-bottom:2px;">{label}</div>'
        f'<div style="font-size:0.82rem;font-weight:600;font-family:monospace;color:{color};">{value}</div></td>'
    )

def fv(val, pre="", suf="", fb="--"):
    return f"{pre}{val}{suf}" if val is not None else fb

def build_stock_card_html(s, mode="buy"):
    is_buy    = mode == "buy"
    verdict   = s.get("verdict", "BUY" if is_buy else "SELL")
    sentiment = s.get("news_sentiment", "neutral")
    bar_col   = "#10b981" if verdict in ("BUY",) else "#ef4444" if verdict == "SELL" else "#f59e0b"
    v_bg      = {"BUY":"#d1fae5","HOLD":"#fef3c7","SELL":"#fee2e2","WATCH":"#fef3c7"}.get(verdict,"#f3f4f6")
    v_col     = {"BUY":"#065f46","HOLD":"#92400e","SELL":"#991b1b","WATCH":"#92400e"}.get(verdict,"#374151")
    sent_col  = "#059669" if sentiment=="positive" else "#dc2626" if sentiment=="negative" else "#6b7280"
    sent_icon = "+" if sentiment=="positive" else "-" if sentiment=="negative" else "~"
    pe_col    = "#059669" if (s.get("pe_ratio") or 99) <= 15 else "#d97706"
    up_col    = "#059669" if (s.get("analyst_upside") or 0) >= 10 else "#dc2626" if (s.get("analyst_upside") or 0) < 0 else "#374151"
    score     = s.get("buy_score") or s.get("sell_risk") or 0
    sc_col    = ("#10b981" if score>=60 else "#f59e0b" if score>=40 else "#ef4444") if is_buy else ("#ef4444" if score>=60 else "#f59e0b" if score>=40 else "#6b7280")
    roe_s     = f"{s['roe']*100:.0f}%"          if s.get("roe")            else "--"
    mar_s     = f"{s['profit_margin']*100:.0f}%" if s.get("profit_margin") else "--"
    div_s     = f"{s['dividend_yield']:.2f}%"    if s.get("dividend_yield") else "--"
    up_s      = f"+{s['analyst_upside']:.1f}%"   if (s.get("analyst_upside") or 0) >= 0 else f"{s['analyst_upside']:.1f}%"  if s.get("analyst_upside") is not None else "--"
    score_lbl = "Buy Score" if is_buy else "Risk Score"

    return (
        f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:20px;overflow:hidden;">'
        f'<div style="height:3px;background:{bar_col};"></div>'
        f'<div style="padding:14px 18px 10px;border-bottom:1px solid #f3f4f6;">'
        f'<table width="100%"><tr>'
        f'<td><span style="font-family:monospace;font-size:1.1rem;font-weight:700;color:#111827;">{s["ticker"]}</span>'
        f'<div style="font-size:0.75rem;color:#6b7280;margin-top:2px;">{s["company"]} &middot; {s["sector"]}</div>'
        f'<div style="font-size:0.65rem;margin-top:3px;"><span style="color:{sent_col};font-weight:600;">{sent_icon} News: {sentiment.title()}</span></div></td>'
        f'<td align="right" style="vertical-align:top;">'
        f'<span style="background:{v_bg};color:{v_col};font-size:0.62rem;font-weight:700;letter-spacing:0.1em;padding:3px 10px;border-radius:20px;text-transform:uppercase;">{verdict}</span>'
        f'<div style="font-size:0.65rem;color:#9ca3af;margin-top:3px;text-align:right;">{score_lbl}: <strong style="color:{sc_col};">{score}</strong>/100</div>'
        f'</td></tr></table></div>'
        f'<table width="100%" style="border-collapse:collapse;border-bottom:1px solid #f3f4f6;"><tr>'
        + mcell("P/E", fv(s.get("pe_ratio"),"","x"), pe_col)
        + mcell("P/B", fv(s.get("pb_ratio"),"","x"))
        + mcell("Price", fv(s.get("price"),"$"))
        + mcell("52W High", fv(s.get("week52_high"),"$"))
        + mcell("% off High", f"-{s.get('pct_from_high',0)}%", "#059669" if is_buy else "#dc2626")
        + mcell("Analyst Up", up_s, up_col)
        + mcell("Div Yield", div_s)
        + '</tr></table>'
        f'<table width="100%" style="border-collapse:collapse;border-bottom:1px solid #f3f4f6;"><tr>'
        + mcell("ROE", roe_s)
        + mcell("Net Margin", mar_s)
        + mcell("D/E", fv(s.get("debt_to_equity"),"","%") if s.get("debt_to_equity") is not None else "--")
        + mcell("EPS Growth", (f"+{s['eps_growth']:.0f}%" if (s.get("eps_growth") or 0) >= 0 else f"{s['eps_growth']:.0f}%") if s.get("eps_growth") is not None else "--", "#059669" if (s.get("eps_growth") or 0) >= 0 else "#dc2626")
        + mcell("EV/EBITDA", fv(s.get("ev_ebitda"),"","x"))
        + mcell("Mkt Cap", f"${s['market_cap_b']:.1f}B" if s.get("market_cap_b") else "--")
        + mcell("Target", fv(s.get("analyst_target"),"$"), up_col)
        + '</tr></table>'
        f'<div style="padding:10px 18px 14px;">'
        f'<div style="font-size:0.58rem;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#9ca3af;margin-bottom:4px;">AI Analysis + News Sentiment</div>'
        f'<div style="font-size:0.8rem;color:#374151;line-height:1.65;">{s.get("analysis","")}</div>'
        f'</div></div>'
    )


def build_email_html(buy_stocks, sell_stocks, params):
    today = datetime.now().strftime("%A, %B %d, %Y")
    def schip(val, lbl, col="#3b82f6"):
        return (f'<td style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:10px 14px;text-align:center;">'
                f'<div style="font-size:1.2rem;font-weight:800;color:{col};line-height:1;">{val}</div>'
                f'<div style="font-size:0.6rem;text-transform:uppercase;letter-spacing:0.08em;color:#9ca3af;margin-top:2px;">{lbl}</div></td>')

    buy_cards  = "".join(build_stock_card_html(s, "buy")  for s in buy_stocks)
    sell_cards = "".join(build_stock_card_html(s, "sell") for s in sell_stocks)

    return (
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        '<title>StockRecommender Report</title></head>'
        '<body style="margin:0;padding:0;background:#f1f5f9;font-family:\'Segoe UI\',Arial,sans-serif;">'
        '<div style="max-width:700px;margin:0 auto;padding:24px 16px;">'
        '<div style="background:linear-gradient(135deg,#0f172a,#1e293b);border-radius:14px;padding:26px 32px;margin-bottom:18px;text-align:center;">'
        '<div style="font-size:1.7rem;font-weight:800;color:#22d3ee;">Stock<span style="color:#e2e8f0;">Recommender</span></div>'
        '<div style="font-size:0.68rem;letter-spacing:0.15em;text-transform:uppercase;color:#64748b;margin-top:4px;">BUY + SELL Daily Report</div>'
        f'<div style="font-size:0.78rem;color:#94a3b8;margin-top:8px;">{today}</div></div>'
        f'<table width="100%" style="border-collapse:separate;border-spacing:8px;margin-bottom:18px;"><tr>'
        + schip(str(len(buy_stocks)),  "BUY Picks",   "#10b981")
        + schip(str(len(sell_stocks)), "SELL Alerts", "#ef4444")
        + schip(str(len(buy_stocks) + len(sell_stocks)), "Total Signals", "#22d3ee")
        + '</tr></table>'
        '<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:9px 14px;font-size:0.71rem;color:#92400e;margin-bottom:18px;">'
        '<strong>Disclaimer:</strong> For informational purposes only. Not financial advice.</div>'
        + (f'<h2 style="font-size:1rem;color:#065f46;margin:0 0 12px;padding:10px 14px;background:#d1fae5;border-radius:8px;">BUY Recommendations ({len(buy_stocks)})</h2>' + buy_cards if buy_stocks else '')
        + (f'<h2 style="font-size:1rem;color:#991b1b;margin:16px 0 12px;padding:10px 14px;background:#fee2e2;border-radius:8px;">SELL Alerts ({len(sell_stocks)})</h2>' + sell_cards if sell_stocks else '')
        + f'<div style="text-align:center;font-size:0.67rem;color:#94a3b8;border-top:1px solid #e2e8f0;padding-top:14px;">'
        f'StockRecommender &middot; {datetime.now().strftime("%Y-%m-%d %H:%M")} &middot; Yahoo Finance + Claude AI</div>'
        '</div></body></html>'
    )


def send_report_email(to_email, buy_stocks, sell_stocks, params):
    if not SMTP_USER or not SMTP_PASSWORD or not EMAIL_FROM:
        return False, "SMTP not configured in .env"
    today   = datetime.now().strftime("%b %d, %Y")
    subject = f"StockRecommender -- {len(buy_stocks)} BUY, {len(sell_stocks)} SELL -- {today}"
    html    = build_email_html(buy_stocks, sell_stocks, params)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = to_email
    msg.attach(MIMEText(f"StockRecommender Report -- {today}. View in HTML client.", "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as server:
                server.ehlo(); server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(EMAIL_FROM, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.ehlo(); server.starttls(context=ctx); server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(EMAIL_FROM, [to_email], msg.as_string())
        return True, "Email sent successfully!"
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP auth failed. Check credentials in .env"
    except Exception as e:
        return False, str(e)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/legal")
def legal():
    return render_template("legal.html")


@app.route("/")
@login_required
def index():
    return render_template("index.html",
                           user_email=current_user.email,
                           user_plan=current_user.plan,
                           scans_today=current_user.scans_today)


@app.route("/start_screen", methods=["POST"])
@login_required
def start_screen():
    if not current_user.can_scan():
        return jsonify({"error": "Daily scan limit reached. Upgrade to Pro for unlimited scans."}), 429
    current_user.record_scan()
    data = request.get_json()
    params = {
        "max_pe":         float(data.get("max_pe",     20)),
        "min_roe":        float(data.get("min_roe",     8)) / 100,
        "min_margin":     float(data.get("min_margin",  3)) / 100,
        "max_de":         float(data.get("max_de",    150)),
        "top_n":          int(data.get("top_n",         6)),
        "price_min":      float(data.get("price_min",    0)),
        "price_max":      float(data.get("price_max", 99999)),
        "req_dividend":   bool(data.get("req_dividend",   False)),
        "req_eps_growth": bool(data.get("req_eps_growth", False)),
        "req_analyst_up": bool(data.get("req_analyst_up", False)),
    }
    job_id = str(uuid.uuid4())
    jobs[job_id] = {"status": "running", "pct": 0, "msg": "Starting...", "result": None}
    threading.Thread(target=run_screener_job, args=(job_id, params), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/poll/<job_id>")
@login_required
def poll(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"status": "error", "msg": "Job not found."}), 404
    return jsonify(job)


@app.route("/send_email", methods=["POST"])
@login_required
def send_email_route():
    data       = request.get_json()
    to_email   = (data.get("email") or "").strip()
    buy_stocks  = data.get("buy_stocks",  [])
    sell_stocks = data.get("sell_stocks", [])
    raw_p       = data.get("params", {})
    if not to_email or "@" not in to_email:
        return jsonify({"success": False, "msg": "Please enter a valid email address."})
    if not buy_stocks and not sell_stocks:
        return jsonify({"success": False, "msg": "No stocks to send. Run the screen first."})
    params = {
        "max_pe":     float(raw_p.get("max_pe", 20)),
        "min_roe":    float(raw_p.get("min_roe", 0.08)),
        "min_margin": float(raw_p.get("min_margin", 0.03)),
        "max_de":     float(raw_p.get("max_de", 150)),
        "top_n":      int(raw_p.get("top_n", 6)),
    }
    ok, msg = send_report_email(to_email, buy_stocks, sell_stocks, params)
    return jsonify({"success": ok, "msg": msg})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
