# Trading System Rules

## Position Constraints
- **Hold time: maximum {position_max_hours} hours.** All positions are forcibly closed after {position_max_hours} hours regardless of P&L.
- **Analysis interval: every {cycle_interval_minutes} minutes.**
- Only **one position** can be open at a time.
- Entry size: $100 USD at 3x leverage (Hyperliquid perpetuals).
- When already in a position, you will be shown current position info. Choose **EXIT** to close early or **HOLD** to keep it.

## Chart Indicators
Each chart shows:
- **Candlesticks** (OHLCV)
- **SMA20** (orange) — short-term trend
- **SMA50** (blue) — medium-term trend
- **RSI** (purple, lower panel) — overbought >70, oversold <30
- **Volume** (middle panel)
