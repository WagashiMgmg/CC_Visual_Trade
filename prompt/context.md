# Trading System Rules

## Position Constraints
- **Minimum hold: {position_min_hours} hours.** Do not choose EXIT until at least {position_min_hours} hours have elapsed since entry, unless there is an extreme emergency.
- **Maximum hold: {position_max_hours} hours.** All positions are forcibly closed after {position_max_hours} hours regardless of P&L.
- **Analysis interval: every {cycle_interval_minutes} minutes.**
- Only **one position** can be open at a time.
- Entry size: $100 USD notional (Hyperliquid perpetuals). Leverage only affects margin requirement, not position size. PnL = $100 × price change %.
- **Round-trip fee (actual)**: {round_trip_fee} — use this value when rule.html says "往復フィー".
- When already in a position, you will be shown current position info. Choose **EXIT** to close early or **HOLD** to keep it.

## Chart Indicators
Each chart shows:
- **Candlesticks** (OHLCV)
- **SMA20** (orange) — short-term trend
- **SMA50** (blue) — medium-term trend
- **RSI** (purple, lower panel) — overbought >70, oversold <30
- **Volume** (middle panel)
