"""
Emergency position monitor.

Runs every 30 seconds (piggy-backed on close_check interval).
Triggers an urgent MAGI session when any of these thresholds are breached:
  1. Unrealized loss exceeds EMERGENCY_LOSS_PCT of size_usd
  2. Unrealized profit exceeds EMERGENCY_PROFIT_PCT of size_usd
  3. Price moves ±EMERGENCY_PRICE_MOVE_PCT within EMERGENCY_PRICE_MOVE_MINUTES
"""

import logging
import threading
from datetime import datetime, timedelta

from src import state
from src.config import settings
from src.notify import send_discord
from src.trader import get_live_position

logger = logging.getLogger(__name__)

# Lock to prevent overlapping emergency cycles
_emergency_lock = threading.Lock()


def _fetch_mid(coin: str) -> float:
    from src.config import make_info
    info = make_info()
    return float(info.all_mids()[coin])


def _record_price(coin: str) -> float | None:
    """Fetch current mid price and append to price_history. Returns the price."""
    if settings.dry_run:
        return None
    try:
        price = _fetch_mid(coin)
        state.price_history.append((datetime.utcnow(), price))
        return price
    except Exception as e:
        logger.debug(f"Failed to fetch price for history: {e}")
        return None


def _check_rapid_move() -> tuple[bool, float | None]:
    """
    Check if price moved ±EMERGENCY_PRICE_MOVE_PCT within the configured window.
    Returns (triggered: bool, move_pct: float | None).
    """
    if len(state.price_history) < 2:
        return False, None

    now = datetime.utcnow()
    window_start = now - timedelta(minutes=settings.emergency_price_move_minutes)
    current_price = state.price_history[-1][1]

    # Find the oldest price within the window
    oldest_in_window = None
    for ts, px in state.price_history:
        if ts >= window_start:
            oldest_in_window = px
            break

    if oldest_in_window is None or oldest_in_window == 0:
        return False, None

    move_pct = (current_price - oldest_in_window) / oldest_in_window * 100
    if abs(move_pct) >= settings.emergency_price_move_pct:
        return True, move_pct

    return False, None


def _is_on_cooldown() -> bool:
    """Check if we're still within the cooldown window from the last emergency."""
    if state.last_emergency_at is None:
        return False
    elapsed = datetime.utcnow() - state.last_emergency_at
    return elapsed < timedelta(minutes=settings.emergency_cooldown_minutes)


def _run_emergency_cycle(reason: str, details: str):
    """Launch an emergency MAGI cycle in the current thread (under lock)."""
    if not _emergency_lock.acquire(blocking=False):
        logger.info("[EMERGENCY] Another emergency cycle already running, skipping")
        return

    try:
        # Avoid running if a normal cycle is already active
        if state.cycle_running:
            logger.info("[EMERGENCY] Normal cycle already running, skipping emergency")
            return

        from src.chart import generate_multi_tf_charts
        from src.orchestrator import run_cycle

        logger.warning(f"[EMERGENCY] Triggering urgent MAGI session: {reason}")

        send_discord(
            title="🚨 緊急MAGI集会 発動",
            message=f"**トリガー**: {reason}\n{details}",
            color=0xFF4500,
        )

        state.last_emergency_at = datetime.utcnow()

        live_pos = get_live_position()
        charts, freshness = generate_multi_tf_charts(
            settings.trading_coin,
            entry_price=live_pos["entry_price"] if live_pos else None,
            entry_time=live_pos["entry_time"] if live_pos else None,
            side=live_pos["side"] if live_pos else None,
        )
        if not charts:
            logger.error("[EMERGENCY] No charts generated, aborting emergency cycle")
            return
        from src.chart import format_cross_freshness
        freshness_text = format_cross_freshness(freshness)

        state.cycle_running = True
        try:
            result = run_cycle(
                charts,
                live_position=live_pos,
                emergency=reason,
                freshness_text=freshness_text,
            )
            logger.info(
                f"[EMERGENCY] Cycle complete: {result['decision']} — "
                f"{result['reason'][:80]}"
            )
            send_discord(
                title="🚨 緊急MAGI集会 結果",
                message=(
                    f"**トリガー**: {reason}\n"
                    f"**判断**: {result['decision']}\n"
                    f"**理由**: {result['reason'][:200]}"
                ),
                color=0xFF4500,
            )
        except Exception as e:
            logger.error(f"[EMERGENCY] Cycle failed: {e}")
        finally:
            state.cycle_running = False
    finally:
        _emergency_lock.release()


def check_emergency():
    """
    Main entry point — called every 30 seconds by the scheduler.
    Checks all emergency thresholds and triggers MAGI if breached.
    """
    if settings.dry_run:
        return

    coin = settings.trading_coin

    # Always record price (even without position, for rapid-move detection)
    current_price = _record_price(coin)

    # Skip if on cooldown
    if _is_on_cooldown():
        return

    live_pos = get_live_position()

    # ── Threshold 1 & 2: Unrealized P&L ──
    if live_pos and live_pos.get("unrealized_pnl") is not None:
        pnl = live_pos["unrealized_pnl"]
        size_usd = live_pos["size_usd"]

        loss_threshold = -(settings.emergency_loss_pct / 100) * size_usd
        profit_threshold = (settings.emergency_profit_pct / 100) * size_usd

        if pnl <= loss_threshold:
            _run_emergency_cycle(
                reason=f"含み損アラート: ${pnl:.2f} (閾値: ${loss_threshold:.2f})",
                details=(
                    f"サイド: {live_pos['side'].upper()}\n"
                    f"エントリー: ${live_pos['entry_price']:,.2f}\n"
                    f"含み損益: ${pnl:.2f} ({pnl / size_usd * 100:.1f}%)"
                ),
            )
            return

        if pnl >= profit_threshold:
            _run_emergency_cycle(
                reason=f"含み益アラート: +${pnl:.2f} (閾値: +${profit_threshold:.2f})",
                details=(
                    f"サイド: {live_pos['side'].upper()}\n"
                    f"エントリー: ${live_pos['entry_price']:,.2f}\n"
                    f"含み損益: +${pnl:.2f} ({pnl / size_usd * 100:.1f}%)"
                ),
            )
            return

    # ── Threshold 3: Rapid price movement (only while in position) ──
    if live_pos and current_price is not None:
        triggered, move_pct = _check_rapid_move()
        if triggered:
            _run_emergency_cycle(
                reason=f"価格急変動: {move_pct:+.2f}% ({settings.emergency_price_move_minutes}分間)",
                details=(
                    f"現在価格: ${current_price:,.2f}\n"
                    f"サイド: {live_pos['side'].upper()}\n"
                    f"含み損益: ${live_pos.get('unrealized_pnl', 0):.2f}"
                ),
            )
