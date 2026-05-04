# Current Trade Watchlist: Hybrid Cash-Secured Puts

**Screening Window:** 30-45 days out from the current date, using the next available valid chain expiry in that band
**Moneyness:** 10-20% out of the money from the current underlying price

These names are the only ones to screen by default. Do not use fixed strikes or fixed expiries from this file. Instead, for each ticker, inspect the current IBKR option chain, find valid put contracts in the 30-45 DTE window, and select strikes that are 10-20% below the current underlying price. When several expiries qualify, use the next available valid expiry in that window.

## Watchlist
- `LRCX`
- `TER`
- `GLW`
- `ATI`
- `QS`

## Selection Rules
- Use the current underlying price from IBKR before choosing strikes.
- Only consider expiries that actually exist on the current option chain.
- Only consider strikes that actually exist on the current option chain.
- Prefer contracts with healthy open interest and volume.
- Reject contracts with wide bid/ask spreads or mid premiums below the system thresholds.
- If no contract in the 30-45 DTE, 10-20% OTM band survives the system rules, discard the ticker for now.

## Output Expectation
- Return only the best valid put candidates discovered from the live chain for these tickers.
- Rank by yield on capital after applying the liquidity and earnings filters.
- Use the current mid price for any staging preview.
