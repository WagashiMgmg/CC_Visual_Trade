"""Fetch 15-minute OHLCV candles from Hyperliquid and generate a PNG chart."""

import logging
import os
from datetime import datetime, timezone

import matplotlib
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import requests

matplotlib.use("Agg")

logger = logging.getLogger(__name__)

HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate RSI without external ta libraries."""
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def fetch_candles(coin: str, interval: str = "15m", count: int = 100) -> pd.DataFrame:
    """Fetch OHLCV candles from Hyperliquid REST API."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # Request extra candles to ensure we have enough after filtering
    start_ms = end_ms - (count + 20) * 15 * 60 * 1000

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }

    resp = requests.post(HYPERLIQUID_API, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    rows = []
    for c in data:
        rows.append(
            {
                "Date": pd.Timestamp(c["t"], unit="ms", tz="UTC"),
                "Open": float(c["o"]),
                "High": float(c["h"]),
                "Low": float(c["l"]),
                "Close": float(c["c"]),
                "Volume": float(c["v"]),
            }
        )

    df = pd.DataFrame(rows).set_index("Date").sort_index()
    logger.info(f"Fetched {len(df)} candles for {coin}")
    return df.tail(count)


def generate_chart(coin: str, count: int = 100) -> str:
    """Generate a candlestick PNG chart and return the file path."""
    df = fetch_candles(coin, "15m", count)

    # Technical indicators
    df["SMA20"] = df["Close"].rolling(20).mean()
    df["SMA50"] = df["Close"].rolling(50).mean()
    df["RSI"] = _rsi(df["Close"], 14)

    os.makedirs("/app/charts", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"/app/charts/{coin}_{ts}.png"

    # Additional plots
    rsi_series = df["RSI"].copy()
    add_plots = [
        mpf.make_addplot(df["SMA20"], color="#ff9900", width=1.5, label="SMA20"),
        mpf.make_addplot(df["SMA50"], color="#58a6ff", width=1.5, label="SMA50"),
        mpf.make_addplot(
            rsi_series, panel=2, color="#bc8cff", width=1.2,
            ylabel="RSI", ylim=(0, 100),
        ),
        mpf.make_addplot(
            [70] * len(df), panel=2, color="#f85149",
            linestyle="--", width=0.8,
        ),
        mpf.make_addplot(
            [30] * len(df), panel=2, color="#3fb950",
            linestyle="--", width=0.8,
        ),
    ]

    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        facecolor="#0d1117",
        edgecolor="#30363d",
        gridcolor="#21262d",
        gridstyle="--",
        gridaxis="both",
        rc={"font.size": 9},
    )

    fig, _ = mpf.plot(
        df,
        type="candle",
        style=style,
        title=f"\n{coin}/USD  15m  ({count} candles)",
        volume=True,
        addplot=add_plots,
        panel_ratios=(3, 1, 1),
        figsize=(16, 10),
        returnfig=True,
        tight_layout=True,
    )

    fig.savefig(out_path, dpi=120, bbox_inches="tight", facecolor="#0d1117")
    plt.close(fig)

    logger.info(f"Chart saved: {out_path}")
    return out_path
