# Trading System Rules

## Position Constraints
- **Minimum hold: {position_min_hours} hours.** Do not choose EXIT until at least {position_min_hours} hours have elapsed since entry, unless there is an extreme emergency.
- **Maximum hold: {position_max_hours} hours.** All positions are forcibly closed after {position_max_hours} hours regardless of P&L.
- **Analysis interval: every {cycle_interval_minutes} minutes.**
- Only **one position** can be open at a time.
- Entry size: ${position_size_usd} USD notional (ATR-based dynamic sizing: equity × {max_risk_pct}% / (ATR% × {atr_multiplier})). Leverage only affects margin requirement, not position size.
- PnL is evaluated as **price change %** (= position return %). Size-independent.
- **Round-trip fee rate**: ~{fee_rate_pct}% — use this value when rule.html says "往復フィー".
- When already in a position, you will be shown current position info. Choose **EXIT** to close early or **HOLD** to keep it.

## Chart Indicators
Each chart shows:
- **Candlesticks** (OHLCV)
- **SMA20** (orange) — short-term trend
- **SMA50** (blue) — medium-term trend
- **VRVP** (right side of price panel) — Volume Range Visible Profile. Green=bullish volume, Red=bearish volume. The highlighted row is the **POC (Point of Control)** = highest-volume price level (yellow dashed line). POC acts as strong support/resistance; high-volume nodes attract price, low-volume nodes let price pass quickly.
- **Volume** (middle panel)
- **RSI** (purple, lower panel) — overbought >70, oversold <30
- **ATR(14)** (yellow, bottom panel) — Average True Range. Measures volatility in price units. Use to assess whether expected move covers fees and to gauge current volatility regime.

## Technical Summary (auto-generated, included in prompt)
Per-timeframe numeric values: SMA state, RSI, ATR, **SMA Spread (%)** = (SMA20−SMA50)/SMA50×100. Spread near 0% = SMA convergence = momentum exhaustion. Use with ATR to detect low-conviction ranging conditions.
