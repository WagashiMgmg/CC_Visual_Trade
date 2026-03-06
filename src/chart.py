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


def _entry_marker(
    df: pd.DataFrame, entry_time: datetime, entry_price: float, side: str
) -> tuple[pd.Series | None, pd.Series | None]:
    """Return (scatter_series, hline_series) for the entry point, or (None, None) if out of range."""
    et = entry_time if entry_time.tzinfo else entry_time.replace(tzinfo=timezone.utc)
    if et < df.index[0]:
        return None, None
    if et > df.index[-1]:
        idx = len(df) - 1  # entry is in the latest (still-forming) candle
    else:
        idx = df.index.get_indexer([et], method="nearest")[0]
    marker = pd.Series(float("nan"), index=df.index)
    marker.iloc[idx] = entry_price
    hline = pd.Series(entry_price, index=df.index)
    return marker, hline


def _sma_cross_markers(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Return golden cross (SMA20 crosses above SMA50) and dead cross markers at Close price."""
    if df["SMA50"].isna().all():
        nan = pd.Series(float("nan"), index=df.index)
        return nan, nan
    prev20 = df["SMA20"].shift(1)
    prev50 = df["SMA50"].shift(1)
    golden = (prev20 < prev50) & (df["SMA20"] >= df["SMA50"])
    dead   = (prev20 > prev50) & (df["SMA20"] <= df["SMA50"])
    gc_markers = df["SMA20"].where(golden)
    dc_markers = df["SMA20"].where(dead)
    return gc_markers, dc_markers


def _plot_chart(
    df: pd.DataFrame, coin: str, title: str, out_path: str,
    entry_price: float | None = None, entry_time: datetime | None = None, side: str | None = None,
) -> None:
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
        # GC/DC markers drawn via annotate after plot (emoji support)
    # Entry point marker
    entry_in_range = False
    if entry_price is not None and entry_time is not None and side is not None:
        e_marker, e_hline = _entry_marker(df, entry_time, entry_price, side)
        if e_marker is not None:
            entry_in_range = True
            e_color = "#3fb950" if side == "long" else "#f85149"
            e_symbol = "^" if side == "long" else "v"
            add_plots.append(mpf.make_addplot(
                e_hline, color=e_color, linestyle="--", width=1.0,
            ))
            add_plots.append(mpf.make_addplot(
                e_marker, type="scatter", markersize=200, marker=e_symbol, color=e_color,
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

    fig, axes = mpf.plot(
        df, type="candle", style=style,
        title=f"\n{title}",
        volume=True, addplot=add_plots,
        panel_ratios=(3, 1, 1), figsize=(16, 10),
        returnfig=True, tight_layout=True,
    )

    ax = axes[0]

    # Embed emoji behind candlesticks using AnnotationBbox (zorder < candle zorder ~3)
    if df["SMA50"].notna().any():
        try:
            import numpy as np
            from PIL import Image as PILImage, ImageDraw, ImageFont
            from matplotlib.offsetbox import AnnotationBbox, OffsetImage

            def _make_emoji_img(char: str, size: int = 48) -> "np.ndarray":
                efont = ImageFont.truetype(
                    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf", 109)
                # Draw on larger canvas to avoid clipping, then crop to content
                canvas = PILImage.new("RGBA", (160, 160), (0, 0, 0, 0))
                ImageDraw.Draw(canvas).text((25, 25), char, font=efont, embedded_color=True)
                bbox = canvas.getbbox()
                if bbox:
                    canvas = canvas.crop(bbox)
                return np.array(canvas.resize((size, size), PILImage.LANCZOS))

            gc_img = _make_emoji_img("🚀")
            dc_img = _make_emoji_img("🔨")
            for idx, val in gc_markers.items():
                if not pd.isna(val):
                    xi = df.index.get_loc(idx)
                    ab = AnnotationBbox(
                        OffsetImage(gc_img, zoom=1.0),
                        (xi, val), frameon=False, zorder=1.5,
                        box_alignment=(0.5, 1.0),  # top of box at SMA → rocket below
                    )
                    ab.set_clip_on(False)
                    ax.add_artist(ab)
            for idx, val in dc_markers.items():
                if not pd.isna(val):
                    xi = df.index.get_loc(idx)
                    ab = AnnotationBbox(
                        OffsetImage(dc_img, zoom=1.0),
                        (xi, val), frameon=False, zorder=1.5,
                        box_alignment=(0.5, 0.0),  # bottom of box at SMA → hammer above
                    )
                    ab.set_clip_on(False)
                    ax.add_artist(ab)
        except Exception as e:
            logger.warning(f"Emoji annotation failed: {e}")

    # Legend
    legend_handles = [
        plt.Line2D([0], [0], color="#ff9900", linewidth=2, label="SMA20"),
        plt.Line2D([0], [0], color="#58a6ff", linewidth=2, label="SMA50"),
        plt.Line2D([0], [0], marker="^", color="#3fb950", markersize=10,
                   linestyle="none", label="GC (Golden Cross)"),
        plt.Line2D([0], [0], marker="v", color="#f85149", markersize=10,
                   linestyle="none", label="DC (Dead Cross)"),
    ]
    if entry_in_range:
        side_label = "Long" if side == "long" else "Short"
        e_label = f"Entry ({side_label}) ${entry_price:,.0f}"
        e_color = "#3fb950" if side == "long" else "#f85149"
        e_symbol = "^" if side == "long" else "v"
        legend_handles.append(
            plt.Line2D([0], [0], marker=e_symbol, color=e_color, markersize=10,
                       linestyle="none", label=e_label)
        )
    ax.legend(handles=legend_handles, loc="upper left",
              facecolor="#161b22", edgecolor="#30363d", labelcolor="white", fontsize=10)

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


def _cross_freshness(df: pd.DataFrame, interval: str) -> dict:
    """Extract GC/DC freshness info: bars since last cross, minutes, current SMA state, RSI."""
    result: dict = {"interval": interval, "sma_state": "neutral", "rsi": None}

    # Current RSI
    rsi_val = df["RSI"].iloc[-1]
    if not pd.isna(rsi_val):
        result["rsi"] = round(rsi_val, 1)

    # SMA state
    sma20 = df["SMA20"].iloc[-1]
    sma50 = df["SMA50"].iloc[-1]
    if not pd.isna(sma20) and not pd.isna(sma50):
        result["sma_state"] = "GC" if sma20 > sma50 else "DC"

    # Find last GC/DC
    gc_markers, dc_markers = _sma_cross_markers(df)
    interval_minutes = _INTERVAL_MS.get(interval, 15 * 60 * 1000) / 60000
    total_bars = len(df)

    for label, markers in [("last_gc", gc_markers), ("last_dc", dc_markers)]:
        valid = markers.dropna()
        if not valid.empty:
            last_idx = df.index.get_loc(valid.index[-1])
            bars_ago = total_bars - 1 - last_idx
            minutes_ago = int(bars_ago * interval_minutes)
            result[label] = {"bars_ago": bars_ago, "minutes_ago": minutes_ago}

    return result


def format_cross_freshness(freshness_list: list[dict]) -> str:
    """Format cross freshness data as structured text for MAGI prompt."""
    # Only include short-term timeframes relevant for signal freshness
    target_intervals = {"1m", "5m", "15m", "30m", "1h"}
    lines = ["## テクニカル指標サマリー（自動生成）"]

    for f in freshness_list:
        if f["interval"] not in target_intervals:
            continue
        parts = [f"**{f['interval']}**: SMA状態={f['sma_state']}"]
        if f.get("rsi") is not None:
            parts.append(f"RSI={f['rsi']}")
        if "last_gc" in f:
            gc = f["last_gc"]
            parts.append(f"最終GC={gc['bars_ago']}本前({gc['minutes_ago']}分前)")
        if "last_dc" in f:
            dc = f["last_dc"]
            parts.append(f"最終DC={dc['bars_ago']}本前({dc['minutes_ago']}分前)")
        if "last_gc" not in f and "last_dc" not in f:
            parts.append("GC/DC=表示範囲内になし")
        lines.append("- " + ", ".join(parts))

    return "\n".join(lines)


def generate_multi_tf_charts(
    coin: str,
    entry_price: float | None = None,
    entry_time: datetime | None = None,
    side: str | None = None,
) -> tuple[list[tuple[str, str, str]], list[dict]]:
    """
    Generate charts for all timeframes.
    Returns (chart_list, freshness_list) where:
        chart_list: list of (interval, label, file_path)
        freshness_list: list of cross freshness dicts per timeframe
    """
    os.makedirs("/app/charts", exist_ok=True)
    _cleanup_old_charts(coin)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []
    freshness = []

    for interval, count, label in TIMEFRAMES:
        try:
            df = fetch_candles(coin, interval, count)
            if df.empty or len(df) < 20:
                logger.warning(f"Not enough data for {interval} ({len(df)} candles), skipping")
                continue
            out_path = f"/app/charts/{coin}_{interval}_{ts}.png"
            title = f"{coin}/USD  {label}  |  SMA20 (orange) SMA50 (blue) RSI (purple)"
            _plot_chart(df, coin, title, out_path,
                        entry_price=entry_price, entry_time=entry_time, side=side)
            logger.info(f"Chart saved: {out_path}")
            results.append((interval, label, out_path))
            freshness.append(_cross_freshness(df, interval))
        except Exception as e:
            logger.error(f"Failed to generate {interval} chart: {e}")

    return results, freshness
