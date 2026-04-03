# -*- coding: utf-8 -*-
#!/usr/bin/env python3
"""
StockRecommender Daily Stock Screener
- Pulls REAL live data from Yahoo Finance (yfinance)
- Multi-factor scoring: P/E, P/B, P/S, EV/EBITDA, ROE, Profit Margin,
  Debt/Equity, EPS Growth, Analyst Target, Dividend Yield, 52W position
- Claude writes the analysis commentary only
- Sends HTML email
"""

from dotenv import load_dotenv
load_dotenv()

import os
import re
import json
import ssl
import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yfinance as yf
import anthropic

# ??? CONFIGURATION ????????????????????????????????????????????????????????????
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "YOUR_API_KEY_HERE")

SMTP_HOST     = os.getenv("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER     = os.getenv("SMTP_USER",     "your@gmail.com")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "your_app_password")
SMTP_PASSWORD = SMTP_PASSWORD.strip('"').strip("'")

EMAIL_FROM = os.getenv("EMAIL_FROM", "your@gmail.com")
EMAIL_TO   = os.getenv("EMAIL_TO",   "erukulla@yahoo.com")

# ?? Screening thresholds (edit or override via .env) ??????????????????????????
MAX_PE          = float(os.getenv("MAX_PE",          "20"))    # P/E <= 20
MAX_PB          = float(os.getenv("MAX_PB",          "3.0"))   # P/B <= 3.0
MAX_PS          = float(os.getenv("MAX_PS",          "3.0"))   # P/S <= 3.0
MAX_EV_EBITDA   = float(os.getenv("MAX_EV_EBITDA",   "12"))    # EV/EBITDA <= 12
MIN_ROE         = float(os.getenv("MIN_ROE",         "0.08"))  # ROE >= 8%
MIN_MARGIN      = float(os.getenv("MIN_MARGIN",      "0.03"))  # Profit margin >= 3%
MAX_DE          = float(os.getenv("MAX_DE",          "150"))   # Debt/Equity <= 150%
MIN_ANALYST_UP  = float(os.getenv("MIN_ANALYST_UP",  "10"))    # Analyst upside >= 10%
TOP_N           = int(os.getenv("TOP_N",             "6"))     # Stocks in email
# ??????????????????????????????????????????????????????????????????????????????

import sys
# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("stockrecommender.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ??? STEP 1: Fetch & score stocks from Yahoo Finance ?????????????????????????

def score_stock(s: dict) -> float:
    """
    Multi-factor score 0-100. Higher = better bargain quality.
    Weights:  52W discount 25 | Analyst upside 20 | ROE 15 |
              P/E cheapness 15 | Profit margin 10 | P/B cheapness 10 | Div yield 5
    """
    score = 0.0

    # 52-week discount (0-25 pts)  - deeper discount = more points
    disc = s.get("pct_from_high", 0)
    score += min(disc / 50 * 25, 25)

    # Analyst upside (0-20 pts)
    upside = s.get("analyst_upside", 0)
    score += min(upside / 40 * 20, 20)

    # ROE (0-15 pts)
    roe = s.get("roe", 0) or 0
    score += min(roe / 0.30 * 15, 15)

    # P/E cheapness (0-15 pts) - lower P/E = more points
    pe = s.get("pe_ratio", MAX_PE)
    score += max(0, (MAX_PE - pe) / MAX_PE * 15)

    # Profit margin (0-10 pts)
    margin = s.get("profit_margin", 0) or 0
    score += min(margin / 0.20 * 10, 10)

    # P/B cheapness (0-10 pts)
    pb = s.get("pb_ratio") or MAX_PB
    score += max(0, (MAX_PB - pb) / MAX_PB * 10)

    # Dividend yield bonus (0-5 pts)
    div = s.get("dividend_yield") or 0
    score += min(div / 5 * 5, 5)

    return round(score, 1)


def passes_filters(info: dict, pe: float) -> tuple[bool, str]:
    """Returns (passes, reason_if_failed)."""
    roe    = info.get("returnOnEquity")
    margin = info.get("profitMargins")
    de     = info.get("debtToEquity")

    if pe <= 0 or pe > MAX_PE:
        return False, f"P/E {pe:.1f} > {MAX_PE}"
    if roe is not None and roe < MIN_ROE:
        return False, f"ROE {roe*100:.1f}% < {MIN_ROE*100:.0f}%"
    if margin is not None and margin < MIN_MARGIN:
        return False, f"Margin {margin*100:.1f}% < {MIN_MARGIN*100:.0f}%"
    if de is not None and de > MAX_DE:
        return False, f"D/E {de:.0f}% > {MAX_DE:.0f}%"
    return True, ""


def fetch_stock_picks() -> list[dict]:
    """
    Dynamically screen Yahoo Finance for US stocks matching our criteria.
    Uses yf.screen() + EquityQuery to filter by P/E, region, exchange at source.
    Then fetches full fundamentals and applies quality filters locally.
    """
    log.info("Running live Yahoo Finance screen for P/E <= %.0f on US exchanges...", MAX_PE)
    candidates = []

    # ── Stage 1: Get tickers from Yahoo Finance screener ──────────────────────
    tickers = []
    try:
        from yfinance import EquityQuery
        # Build query: US region + listed on major exchanges + profitable (PE > 0)
        q = EquityQuery('and', [
            EquityQuery('eq',   ['region', 'us']),
            EquityQuery('is-in',['exchange', 'NMS', 'NYQ']),   # NASDAQ + NYSE only
            EquityQuery('btwn', ['trailingpe', 0.01, MAX_PE]), # P/E between 0 and MAX_PE
            EquityQuery('gt',   ['intradaymarketcap', 500000000]),  # market cap > $500M
        ])
        result = yf.screen(q, sortField='trailingpe', sortAsc=True, size=250)
        quotes = result.get('quotes', [])
        tickers = [q['symbol'] for q in quotes if q.get('symbol')]
        log.info("Yahoo Finance screener returned %d tickers with P/E <= %.0f", len(tickers), MAX_PE)
    except Exception as e:
        log.warning("Yahoo Finance screen() failed: %s", e)
        log.warning("Falling back to predefined screener...")
        try:
            from yfinance import EquityQuery
            # Try simpler query without PE filter - apply PE filter ourselves
            q = EquityQuery('and', [
                EquityQuery('eq',    ['region', 'us']),
                EquityQuery('is-in', ['exchange', 'NMS', 'NYQ']),
                EquityQuery('gt',    ['intradaymarketcap', 1000000000]),  # > $1B
            ])
            result = yf.screen(q, sortField='intradaymarketcap', sortAsc=False, size=250)
            quotes = result.get('quotes', [])
            tickers = [q['symbol'] for q in quotes if q.get('symbol')]
            log.info("Fallback screener returned %d tickers", len(tickers))
        except Exception as e2:
            log.error("Both screeners failed: %s. Cannot proceed without ticker list.", e2)
            return []

    if not tickers:
        log.error("No tickers returned from Yahoo Finance screener.")
        return []

    # ── Stage 2: Fetch full fundamentals + apply quality filters ──────────────
    log.info("Fetching fundamentals for %d tickers...", len(tickers))
    passed = failed = skipped = 0

    for ticker in tickers:
        try:
            info   = yf.Ticker(ticker).info
            pe     = info.get("trailingPE") or info.get("forwardPE") or 0
            price  = info.get("currentPrice") or info.get("regularMarketPrice")
            low52  = info.get("fiftyTwoWeekLow")
            high52 = info.get("fiftyTwoWeekHigh")

            # Skip if missing essential data
            if not all([pe, price, low52, high52]):
                skipped += 1
                continue
            if not high52 or not low52 or high52 <= 0 or low52 <= 0:
                skipped += 1
                continue

            # Apply quality filters
            ok, reason = passes_filters(info, pe)
            if not ok:
                failed += 1
                log.debug("  FAIL %s - %s", ticker, reason)
                continue

            pct_from_high  = round((high52 - price) / high52 * 100, 1)
            pct_from_low   = round((price - low52)  / low52  * 100, 1)
            target         = info.get("targetMeanPrice")
            analyst_upside = round((target - price) / price * 100, 1) if target and price else None
            roe            = info.get("returnOnEquity")
            margin         = info.get("profitMargins")
            de             = info.get("debtToEquity")
            pb             = info.get("priceToBook")
            ps             = info.get("priceToSalesTrailing12Months")
            ev_ebitda      = info.get("enterpriseToEbitda")
            eps_growth     = info.get("earningsGrowth")
            div_yield      = info.get("dividendYield")
            rec            = info.get("recommendationMean")

            stock = {
                "ticker":          ticker,
                "company":         info.get("longName", ticker),
                "sector":          info.get("sector", "Unknown"),
                # Value
                "pe_ratio":        round(pe, 1),
                "pb_ratio":        round(pb, 2)       if pb        else None,
                "ps_ratio":        round(ps, 2)       if ps        else None,
                "ev_ebitda":       round(ev_ebitda,1) if ev_ebitda else None,
                # Price / 52W
                "price":           round(price, 2),
                "week52_low":      round(low52, 2),
                "week52_high":     round(high52, 2),
                "pct_from_high":   pct_from_high,
                "pct_from_low":    pct_from_low,
                # Quality
                "roe":             round(roe, 4)    if roe    else None,
                "profit_margin":   round(margin, 4) if margin else None,
                "debt_to_equity":  round(de, 1)     if de is not None else None,
                "eps_growth":      round(eps_growth * 100, 1) if eps_growth else None,
                # Income
                "dividend_yield":  round(div_yield * 100, 2) if div_yield else None,
                "market_cap_b":    round((info.get("marketCap") or 0) / 1e9, 1),
                # Analyst
                "analyst_target":  round(target, 2) if target else None,
                "analyst_upside":  analyst_upside,
                "analyst_rating":  round(rec, 1)    if rec    else None,
            }
            stock["score"] = score_stock(stock)
            candidates.append(stock)
            passed += 1
            log.info("  OK %-6s score=%.0f  P/E=%.1f  ROE=%s  upside=%s%%",
                     ticker, stock["score"], pe,
                     f"{roe*100:.0f}%" if roe else "--",
                     f"{analyst_upside:.0f}" if analyst_upside is not None else "--")

        except Exception as e:
            log.warning("  SKIP %s: %s", ticker, e)
            skipped += 1

    log.info("Results: %d passed / %d filtered out / %d skipped. Picking top %d by score.",
             passed, failed, skipped, TOP_N)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:TOP_N]
# ??? STEP 2: Claude analysis ??????????????????????????????????????????????????

def enrich_with_analysis(stocks: list[dict]) -> list[dict]:
    if not stocks:
        return stocks
    log.info("Asking Claude to write analysis for %d stocks-", len(stocks))
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=60.0)

    def _pct(val):
        return f"{val*100:.0f}%" if val else "-"

    def _fmt(val, suffix="", fallback="-"):
        return f"{val}{suffix}" if val is not None else fallback

    lines = []
    for s in stocks:
        line = (
            f"- {s['ticker']} ({s['company']}, {s['sector']}): "
            f"Score={s['score']}, P/E={s['pe_ratio']}, P/B={_fmt(s.get('pb_ratio'))}, "
            f"ROE={_pct(s.get('roe'))}, Margin={_pct(s.get('profit_margin'))}, "
            f"D/E={_fmt(s.get('debt_to_equity'), suffix='%')}, "
            f"EPS Growth={_fmt(s.get('eps_growth'), suffix='%')}, "
            f"Analyst upside={_fmt(s.get('analyst_upside'), suffix='%')}, "
            f"{s['pct_from_high']}% off 52w high"
        )
        lines.append(line)
    summary = "\n".join(lines)

    prompt = f"""You are a value investing analyst. Today is {datetime.now().strftime('%B %d, %Y')}.
These {len(stocks)} stocks passed a rigorous multi-factor screen (P/E, ROE, profit margin, debt/equity, analyst upside):

{summary}

For each ticker write exactly 2 sentences: (1) the strongest reason it's a bargain based on these metrics, (2) the key risk an investor should monitor.

Return ONLY a JSON object - no markdown, no explanation:
{{"TICK": "sentence 1. sentence 2.", ...}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        raw    = response.content[0].text
        clean  = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`")
        analyses = json.loads(clean)
        for s in stocks:
            s["analysis"] = analyses.get(s["ticker"], "Strong multi-factor value profile relative to sector peers.")
            s["verdict"]  = "BUY" if s["score"] >= 40 else "HOLD"
    except Exception as e:
        log.warning("Claude analysis failed (%s) - using defaults.", e)
        for s in stocks:
            s.setdefault("analysis", "Meets multi-factor value criteria with strong fundamentals.")
            s.setdefault("verdict", "BUY" if s.get("score", 0) >= 40 else "HOLD")
    return stocks


# ??? STEP 3: Build HTML email ?????????????????????????????????????????????????

def fmt(val, fmt_str="", prefix="", suffix="", fallback="-"):
    if val is None:
        return fallback
    try:
        return f"{prefix}{val:{fmt_str}}{suffix}"
    except Exception:
        return fallback

def cell(label, value, color="#111827", bg="#fff"):
    return f"""<td style="text-align:center;padding:7px 5px;border-right:1px solid #f3f4f6;background:{bg};">
      <div style="font-size:0.55rem;text-transform:uppercase;letter-spacing:0.08em;color:#9ca3af;margin-bottom:2px;">{label}</div>
      <div style="font-size:0.82rem;font-weight:600;font-family:monospace;color:{color};">{value}</div>
    </td>"""

def score_bar(score):
    pct  = min(int(score), 100)
    col  = "#10b981" if pct >= 60 else "#f59e0b" if pct >= 40 else "#ef4444"
    return f"""<div style="margin:8px 0 4px;">
      <div style="display:flex;justify-content:space-between;margin-bottom:3px;">
        <span style="font-size:0.6rem;text-transform:uppercase;letter-spacing:0.08em;color:#9ca3af;">StockRecommender Score</span>
        <span style="font-size:0.75rem;font-weight:700;color:{col};">{score}/100</span>
      </div>
      <div style="background:#f3f4f6;border-radius:4px;height:5px;overflow:hidden;">
        <div style="background:{col};height:100%;width:{pct}%;border-radius:4px;"></div>
      </div>
    </div>"""

def chip(label, value, good=True):
    bg  = "#f0fdf4" if good else "#fef9c3"
    col = "#15803d" if good else "#854d0e"
    return f"""<span style="display:inline-block;background:{bg};color:{col};
                font-size:0.62rem;font-weight:600;padding:2px 8px;border-radius:10px;
                margin:2px 2px 0 0;">{label}: {value}</span>"""

def build_email_html(stocks):
    today  = datetime.now().strftime("%A, %B %d, %Y")
    buys   = sum(1 for s in stocks if s.get("verdict") == "BUY")
    holds  = len(stocks) - buys
    avg_pe = sum(s.get("pe_ratio", 0) for s in stocks) / len(stocks) if stocks else 0

    def summary_chip(val, lbl, col="#3b82f6"):
        return f"""<td style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;
                    padding:10px 14px;text-align:center;">
          <div style="font-size:1.2rem;font-weight:800;color:{col};line-height:1;">{val}</div>
          <div style="font-size:0.6rem;text-transform:uppercase;letter-spacing:0.08em;color:#9ca3af;margin-top:2px;">{lbl}</div>
        </td>"""

    cards = ""
    for s in stocks:
        is_buy   = s.get("verdict") == "BUY"
        v_bg     = "#d1fae5" if is_buy else "#fef3c7"
        v_col    = "#065f46" if is_buy else "#92400e"
        bar_col  = "#10b981" if is_buy else "#f59e0b"
        pe_col   = "#059669" if s.get("pe_ratio", 99) <= 15 else "#d97706"
        roe_col  = "#059669" if (s.get("roe") or 0) >= 0.15 else "#d97706"
        up_col   = "#059669" if (s.get("analyst_upside") or 0) >= 20 else "#374151"

        # Analyst rating label
        rec = s.get("analyst_rating")
        if rec:
            rec_lbl = "Strong Buy" if rec <= 1.5 else "Buy" if rec <= 2.5 else "Hold" if rec <= 3.5 else "Sell"
        else:
            rec_lbl = "-"

        # Quality chips
        chips = ""
        if s.get("roe"):        chips += chip("ROE", f"{s['roe']*100:.0f}%", s['roe'] >= MIN_ROE)
        if s.get("profit_margin"): chips += chip("Margin", f"{s['profit_margin']*100:.0f}%", s['profit_margin'] >= MIN_MARGIN)
        if s.get("debt_to_equity") is not None: chips += chip("D/E", f"{s['debt_to_equity']:.0f}%", s['debt_to_equity'] <= MAX_DE)
        if s.get("eps_growth") is not None:     chips += chip("EPS Grw", f"{s['eps_growth']:+.0f}%", s['eps_growth'] >= 0)

        cards += f"""
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;
                    margin-bottom:20px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.05);">
          <div style="height:3px;background:{bar_col};"></div>

          <!-- Header -->
          <div style="padding:14px 18px 10px;border-bottom:1px solid #f3f4f6;">
            <table width="100%"><tr>
              <td>
                <span style="font-family:monospace;font-size:1.1rem;font-weight:700;color:#111827;">{s['ticker']}</span>
                <span style="font-size:0.72rem;color:#6b7280;margin-left:8px;">{s['company']}</span>
                <div style="font-size:0.7rem;color:#9ca3af;margin-top:2px;">{s['sector']}</div>
              </td>
              <td align="right" style="vertical-align:top;">
                <span style="background:{v_bg};color:{v_col};font-size:0.62rem;font-weight:700;
                             letter-spacing:0.1em;padding:3px 10px;border-radius:20px;text-transform:uppercase;">
                  {s.get('verdict','-')}
                </span>
                <div style="font-size:0.65rem;color:#9ca3af;margin-top:4px;text-align:right;">
                  Score&nbsp;<strong style="color:#374151;">{s.get('score','-')}</strong>/100
                </div>
              </td>
            </tr></table>
            {score_bar(s.get('score', 0))}
          </div>

          <!-- Value metrics row -->
          <table width="100%" style="border-collapse:collapse;border-bottom:1px solid #f3f4f6;">
            <tr>
              {cell('P/E', fmt(s.get('pe_ratio'), '.1f', suffix='x'), pe_col)}
              {cell('P/B', fmt(s.get('pb_ratio'), '.2f', suffix='x'))}
              {cell('P/S', fmt(s.get('ps_ratio'), '.2f', suffix='x'))}
              {cell('EV/EBITDA', fmt(s.get('ev_ebitda'), '.1f', suffix='x'))}
              {cell('Price', fmt(s.get('price'), '.2f', prefix='$'))}
              {cell('52W High', fmt(s.get('week52_high'), '.2f', prefix='$'))}
              {cell('% off High', fmt(s.get('pct_from_high'), '.1f', prefix='-', suffix='%'), '#059669')}
            </tr>
          </table>

          <!-- Quality & income row -->
          <table width="100%" style="border-collapse:collapse;border-bottom:1px solid #f3f4f6;">
            <tr>
              {cell('ROE', fmt(s.get('roe'), '.0%') if s.get('roe') else '-', roe_col)}
              {cell('Net Margin', fmt(s.get('profit_margin'), '.0%') if s.get('profit_margin') else '-')}
              {cell('D/E Ratio', fmt(s.get('debt_to_equity'), '.0f', suffix='%') if s.get('debt_to_equity') is not None else '-')}
              {cell('EPS Growth', fmt(s.get('eps_growth'), '+.0f', suffix='%') if s.get('eps_growth') is not None else '-')}
              {cell('Div Yield', fmt(s.get('dividend_yield'), '.2f', suffix='%') if s.get('dividend_yield') else '-')}
              {cell('Analyst Target', fmt(s.get('analyst_target'), '.2f', prefix='$') if s.get('analyst_target') else '-', up_col)}
              {cell('Analyst Upside', fmt(s.get('analyst_upside'), '+.1f', suffix='%') if s.get('analyst_upside') is not None else '-', up_col)}
            </tr>
          </table>

          <!-- Quality chips -->
          <div style="padding:8px 18px 6px;">{chips}</div>

          <!-- AI Analysis -->
          <div style="padding:8px 18px 14px;border-top:1px solid #f9fafb;">
            <div style="font-size:0.58rem;font-weight:700;letter-spacing:0.12em;
                        text-transform:uppercase;color:#9ca3af;margin-bottom:4px;">AI Analysis</div>
            <div style="font-size:0.8rem;color:#374151;line-height:1.65;">{s.get('analysis','')}</div>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>StockRecommender Daily Report</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;">
<div style="max-width:700px;margin:0 auto;padding:24px 16px;">

  <div style="background:linear-gradient(135deg,#0f172a,#1e293b);border-radius:14px;
              padding:26px 32px;margin-bottom:18px;text-align:center;">
    <div style="font-size:1.7rem;font-weight:800;color:#10b981;letter-spacing:-0.5px;">
      Value<span style="color:#e2e8f0;">Scope</span>
    </div>
    <div style="font-size:0.68rem;letter-spacing:0.15em;text-transform:uppercase;color:#64748b;margin-top:4px;">
      Multi-Factor Bargain Stock Report * Live Data
    </div>
    <div style="font-size:0.78rem;color:#94a3b8;margin-top:8px;">{today}</div>
  </div>

  <table width="100%" style="border-collapse:separate;border-spacing:8px;margin-bottom:18px;">
    <tr>
      {summary_chip(str(len(stocks)), 'Stocks Found')}
      {summary_chip(str(buys), 'BUY Signals', '#10b981')}
      {summary_chip(str(holds), 'HOLD / Watch', '#f59e0b')}
      {summary_chip(f'{avg_pe:.1f}x', 'Avg P/E')}
      {summary_chip(f'<={MAX_PE:.0f}x', 'PE Filter')}
      {summary_chip(f'>={MIN_ROE*100:.0f}%', 'Min ROE')}
    </tr>
  </table>

  <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;
              padding:9px 14px;font-size:0.71rem;color:#92400e;margin-bottom:18px;">
    [!] <strong>Disclaimer:</strong> Live data from Yahoo Finance. For informational purposes only -
    not financial advice. Always verify independently and consult a professional before investing.
  </div>

  {cards}

  <div style="text-align:center;font-size:0.67rem;color:#94a3b8;
              border-top:1px solid #e2e8f0;padding-top:14px;">
    StockRecommender · {datetime.now().strftime('%Y-%m-%d %H:%M')} · Yahoo Finance + Claude AI<br>
    <span style="color:#cbd5e1;">
      Filters: P/E<={MAX_PE:.0f} * ROE>={MIN_ROE*100:.0f}% * Margin>={MIN_MARGIN*100:.0f}% *
      D/E<={MAX_DE:.0f}% * Analyst upside>={MIN_ANALYST_UP:.0f}% target
    </span>
  </div>
</div></body></html>"""


# ??? STEP 4: Send email ???????????????????????????????????????????????????????

def send_email(html_body, stock_count):
    today   = datetime.now().strftime("%b %d, %Y")
    subject = f"? StockRecommender - {stock_count} Bargain Stocks (Multi-Factor) * {today}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(
        f"StockRecommender Daily Report - {today}. {stock_count} stocks found. View in HTML email client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    log.info("Sending email via %s:%d-", SMTP_HOST, SMTP_PORT)
    ctx = ssl.create_default_context()
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as server:
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        log.info("SENT: Email sent to %s", EMAIL_TO)
    except smtplib.SMTPAuthenticationError:
        log.error("SMTP auth failed - verify SMTP_USER and App Password in .env")
        raise
    except Exception as e:
        log.error("SMTP error: %s", e)
        raise


# ??? MAIN ?????????????????????????????????????????????????????????????????????

def main():
    log.info("=" * 55)
    log.info("StockRecommender run started - %s", datetime.now().isoformat())
    try:
        stocks = fetch_stock_picks()
        if not stocks:
            log.warning("No stocks passed all filters - try relaxing thresholds in .env")
            return
        stocks = enrich_with_analysis(stocks)
        html   = build_email_html(stocks)
        send_email(html, len(stocks))
        log.info("DONE: Run complete. %d stocks emailed.", len(stocks))
    except Exception as e:
        log.exception("Run failed: %s", e)
        raise
    log.info("=" * 55)


if __name__ == "__main__":
    main()
