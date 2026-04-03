# StockRecommender — AI-Powered Bargain Stock Screener

A full-stack web app that dynamically screens the US stock market using Yahoo Finance, applies multi-factor value investing filters, and displays results with AI-generated analysis. Works on desktop and mobile.

---

## Features

- **Fully dynamic screening** — uses Yahoo Finance `EquityQuery` to scan the entire US market at runtime
- **Interactive UI** — sliders and toggles for all screening criteria, works on any device
- **Real-time progress** — live streaming updates as stocks are scanned
- **Multi-factor scoring** — ranks stocks across 8 weighted indicators
- **AI analysis** — Claude AI writes a 2-sentence bargain thesis + key risk per stock
- **Responsive design** — optimized for desktop, tablet, and mobile
- **Daily email mode** — also includes `stock_screener.py` for scheduled email delivery

---

## Project Structure

```
valuescope/
├── app.py                  # Flask web server
├── stock_screener.py       # Standalone daily email script
├── templates/
│   └── index.html          # Responsive frontend UI
├── .env.example            # Environment variable template
├── requirements.txt        # Python dependencies
├── .gitignore
└── README.md
```

---

## Screening Indicators

| Indicator | Control | Description |
|---|---|---|
| P/E Ratio | Slider (5–50) | Price to earnings — core value metric |
| ROE | Slider (0–40%) | Return on equity — quality filter |
| Profit Margin | Slider (0–30%) | Net margin — confirms profitability |
| Debt/Equity | Slider (0–400%) | Leverage — avoids overleveraged traps |
| Dividend payers | Toggle | Only show stocks with dividends |
| Positive EPS growth | Toggle | Confirms recovery, not decline |
| Analyst upside ≥10% | Toggle | Consensus Wall St. conviction |
| Stocks in report | 3/4/6/8/10 | How many top picks to show |

## Scoring (0–100)

| Factor | Weight |
|---|---|
| 52W discount from high | 25 pts |
| Analyst upside | 20 pts |
| Return on equity | 15 pts |
| P/E cheapness | 15 pts |
| Profit margin | 10 pts |
| P/B cheapness | 10 pts |
| Dividend yield | 5 pts |

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/valuescope.git
cd valuescope
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
ANTHROPIC_API_KEY=YOUR_ANTHROPIC_API_KEY_HERE
# Email settings (for daily email script only)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_USER=your@gmail.com
SMTP_PASSWORD=YOUR_APP_PASSWORD_HERE
EMAIL_FROM=your@gmail.com
EMAIL_TO=recipient@email.com
```

### 4. Run the web app

```bash
python app.py
```

Then open **http://localhost:5000** in your browser.

### 5. Run on mobile (same network)

Find your computer's local IP:
```bash
# Mac/Linux
ipconfig getifaddr en0

# Windows
ipconfig
```

Then open `http://YOUR_IP:5000` on your phone.

---

## Daily Email (optional)

To also send a daily email report, schedule `stock_screener.py`:

**Mac/Linux (cron):**
```bash
crontab -e
0 12 * * * cd /path/to/valuescope && python3 stock_screener.py
```

**Windows (Task Scheduler):**
Daily → 12:00 PM → run `stock_screener.py`

---

## Disclaimer

For informational and educational purposes only. Not financial advice. Always verify data independently and consult a qualified financial advisor before making investment decisions.

---

## Tech Stack

- [Flask](https://flask.palletsprojects.com/) — Web framework
- [yfinance](https://github.com/ranaroussi/yfinance) — Live market data + EquityQuery screener
- [Anthropic Claude](https://anthropic.com) — AI analysis commentary
- Vanilla JS + SSE — Real-time streaming UI (no heavy frontend framework needed)

---

## License

MIT License
