# Alpaca Auto Trader (Skeleton)

- Minute bars via Alpaca IEX (free) with **yfinance fallback**.
- Momentum scanner (intraday change + volume spike).
- Value filter (market cap < $5B, PER vs group, EPS growth).
- Circuit-breaker (SPY -7% intraday) & trading window guard.
- Paper broker (no API keys) fallback.

## Quickstart
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.paper.example .env.paper  # fill API keys
MODE=paper python main.py --minutes 600 --loop
```
# authTrade
