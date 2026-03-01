# Trading System Rules

## Position Constraints
- **Hold time: maximum 1 hour.** All positions are forcibly closed after 1 hour regardless of P&L.
- Only **one position** can be open at a time.
- Entry size: $100 USD at 3x leverage (Hyperliquid perpetuals).

## Chart Indicators
Each chart shows:
- **Candlesticks** (OHLCV)
- **SMA20** (orange) — short-term trend
- **SMA50** (blue) — medium-term trend
- **RSI** (purple, lower panel) — overbought >70, oversold <30
- **Volume** (middle panel)

## Decision Framework
Given the **1-hour maximum hold**, focus on:
- Immediate momentum signals (15m / 30m charts are most actionable)
- Higher timeframes (1h, 1d, 1w, 1M) for overall directional bias
- RSI extremes on short timeframes for potential mean-reversion entries

Prefer **HOLD** when:
- No clear directional momentum exists
- Price is in consolidation / range
- Signals across timeframes conflict strongly
