"""Fetch OHLCV candles from Hyperliquid and generate PNG charts."""

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

def _api_url() -> str:
    from src.config import settings
    return settings.api_url + "/info"

# Timeframes to generate: (interval, candle_count, label)
TIMEFRAMES = [
    ("1m",   120, "1min  (~2h)"),
    ("5m",   120, "5min  (~10h)"),
    ("15m",  120, "15min (~30h)"),
    ("30m",  120, "30min (~2.5d)"),
    ("1h",    96, "1h    (~4d)"),
    ("1d",    90, "1d    (~3mo)"),
    ("1w",    60, "1w    (~1.2yr)"),
    ("1M",    36, "1M    (~3yr)"),
]

# Interval string → milliseconds per candle
_INTERVAL_MS = {
    "1m":   1 * 60 * 1000,
    "5m":   5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "30m": 30 * 60 * 1000,
    "1h":  60 * 60 * 1000,
    "1d":  24 * 60 * 60 * 1000,
    "1w":   7 * 24 * 60 * 60 * 1000,
    "1M":  30 * 24 * 60 * 60 * 1000,
}


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def fetch_candles(coin: str, interval: str, count: int) -> pd.DataFrame:
    """Fetch OHLCV candles from Hyperliquid REST API."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    interval_ms = _INTERVAL_MS.get(interval, 15 * 60 * 1000)
    start_ms = end_ms - (count + 20) * interval_ms

    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms},
    }
    resp = requests.post(_api_url(), json=payload, timeout=15)
    resp.raise_for_status()

    rows = [
        {
            "Date":   pd.Timestamp(c["t"], unit="ms", tz="UTC"),
            "Open":   float(c["o"]),
            "High":   float(c["h"]),
            "Low":    float(c["l"]),
            "Close":  float(c["c"]),
            "Volume": float(c["v"]),
        }
        for c in resp.json()
    ]
    df = pd.DataFrame(rows).set_index("Date").sort_index()
    return df.tail(count)


def _sma_cross_markers(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Return golden cross (SMA20 crosses above SMA50) and dead cross markers at Close price."""
    if df["SMA50"].isna().all():
        nan = pd.Series(float("nan"), index=df.index)
        return nan, nan
    prev20 = df["SMA20"].shift(1)
    prev50 = df["SMA50"].shift(1)
    golden = (prev20 < prev50) & (df["SMA20"] >= df["SMA50"])
    dead   = (prev20 > prev50) & (df["SMA20"] <= df["SMA50"])
    gc_markers = df["Close"].where(golden)
    dc_markers = df["Close"].where(dead)
    return gc_markers, dc_markers


def _plot_chart(df: pd.DataFrame, coin: str, title: str, out_path: str) -> None:
    """Render and save a single candlestick chart."""
    df["SMA20"] = df["Close"].rolling(20).mean()
    df["SMA50"] = df["Close"].rolling(50).mean()
    df["RSI"]   = _rsi(df["Close"], 14)

    gc_markers, dc_markers = _sma_cross_markers(df)

    add_plots = [
        mpf.make_addplot(df["SMA20"], color="#ff9900", width=1.5, label="SMA20"),
    ]
    if df["SMA50"].notna().any():
        add_plots.append(mpf.make_addplot(df["SMA50"], color="#58a6ff", width=1.5, label="SMA50"))
        if gc_markers.notna().any():
            add_plots.append(mpf.make_addplot(
                gc_markers, type="scatter", markersize=120, marker="x",
                color="#3fb950",  # green = golden cross
            ))
        if dc_markers.notna().any():
            add_plots.append(mpf.make_addplot(
                dc_markers, type="scatter", markersize=120, marker="x",
                color="#f85149",  # red = dead cross
            ))
    add_plots += [
        mpf.make_addplot(df["RSI"], panel=2, color="#bc8cff", width=1.2,
                         ylabel="RSI", ylim=(0, 100)),
        mpf.make_addplot([70] * len(df), panel=2, color="#f85149", linestyle="--", width=0.8),
        mpf.make_addplot([30] * len(df), panel=2, color="#3fb950", linestyle="--", width=0.8),
    ]

    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds",
        facecolor="#0d1117", edgecolor="#30363d",
        gridcolor="#21262d", gridstyle="--", gridaxis="both",
        rc={"font.size": 11},
    )

    fig, _ = mpf.plot(
        df, type="candle", style=style,
        title=f"\n{title}",
        volume=True, addplot=add_plots,
        panel_ratios=(3, 1, 1), figsize=(16, 10),
        returnfig=True, tight_layout=True,
    )
    fig.savefig(out_path, dpi=120, bbox_inches="tight", facecolor="#0d1117")
    plt.close(fig)


def _cleanup_old_charts(coin: str) -> None:
    """Delete previous cycle's chart PNGs for this coin."""
    charts_dir = "/app/charts"
    for fname in os.listdir(charts_dir):
        if fname.startswith(f"{coin}_") and fname.endswith(".png"):
            try:
                os.remove(os.path.join(charts_dir, fname))
            except OSError:
                pass


def generate_multi_tf_charts(coin: str) -> list[tuple[str, str, str]]:
    """
    Generate charts for all timeframes.
    Returns list of (interval, label, file_path).
    """
    os.makedirs("/app/charts", exist_ok=True)
    _cleanup_old_charts(coin)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []

    for interval, count, label in TIMEFRAMES:
        try:
            df = fetch_candles(coin, interval, count)
            if df.empty or len(df) < 20:
                logger.warning(f"Not enough data for {interval} ({len(df)} candles), skipping")
                continue
            out_path = f"/app/charts/{coin}_{interval}_{ts}.png"
            title = f"{coin}/USD  {label}  |  SMA20 (orange) SMA50 (blue) RSI (purple)"
            _plot_chart(df, coin, title, out_path)
            logger.info(f"Chart saved: {out_path}")
            results.append((interval, label, out_path))
        except Exception as e:
            logger.error(f"Failed to generate {interval} chart: {e}")

    return results
