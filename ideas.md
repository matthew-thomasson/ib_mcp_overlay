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
- `AMZN`
- `NOW`
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

---

## Speculative / Opportunistic Ideas

These ideas were researched at market close and should be re-evaluated at market open for live Bid/Ask pricing before staging any orders.

### AMZN — Cash-Secured Put (Sleep-Well-at-Night Trade)
- **Strategy:** Sell Put — Cash-Secured
- **Strike:** $220.00
- **Expiry:** 2026-07-17 (~73 DTE)
- **AMZN Price at Research:** ~$273.75
- **OTM Buffer:** ~19.6% — market must fall nearly 20% before this strike is challenged
- **Capital Required:** $22,000 (~£16,900) — well within 5% portfolio sizing limit
- **Open Interest at Research:** 12,126 ✅ (passes liquidity floor of 100)
- **Earnings Check:** PASS — no AMZN earnings between 2026-05-05 and 2026-07-17
- **Rationale:** Deploy idle cash from the sweep account to generate yield in excess of the current 3.8% cash rate, with a substantial OTM cushion against an ATH correction. Conservative, high-quality underlying. Only to be staged once live Bid/Ask confirms a Mid-price above $0.20 per share minimum premium threshold.
- **Action:** Re-run `find_cash_secured_put_opportunities` at market open. If Mid-price ≥ $0.20 and spread ≤ 20% of Mid, proceed to Gate 6 staging.
