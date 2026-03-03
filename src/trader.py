"""
Position manager: closes positions that have been open for >= 1 hour.
Strategy: try limit order first, fall back to market order.
"""

import logging
import time
from datetime import datetime, timedelta

from src.config import settings
from src.database import Trade, get_session
from src.reflection import trigger_reflection

logger = logging.getLogger(__name__)


def calc_pnl(side: str, entry_price: float, exit_price: float, size_usd: float) -> float:
    """Calculate P&L in USD for a closed position."""
    if side == "long":
        return (exit_price - entry_price) / entry_price * size_usd
    return (entry_price - exit_price) / entry_price * size_usd


def get_open_trade():
    """Return the currently open Trade, or None."""
    with get_session() as session:
        trade = session.query(Trade).filter(Trade.status == "open").first()
        if trade:
            # Detach from session by accessing needed fields
            session.expunge(trade)
        return trade


def close_expired_positions():
    """
    Check for positions open >= 1 hour and close them.
    Called by APScheduler every 30 seconds.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=settings.position_max_duration_secs)
    closed_trades = []

    with get_session() as session:
        expired = (
            session.query(Trade)
            .filter(Trade.status == "open", Trade.entry_time <= cutoff)
            .all()
        )

        for trade in expired:
            age_mins = (datetime.utcnow() - trade.entry_time).seconds // 60
            logger.info(
                f"Closing expired position: trade_id={trade.id} "
                f"side={trade.side} age={age_mins}m"
            )
            try:
                if settings.dry_run:
                    exit_price = trade.entry_price  # Simulate no P&L
                    pnl = 0.0
                    logger.info(f"[DRY RUN] Would close trade_id={trade.id}")
                else:
                    exit_price = _close_position(trade)
                    pnl = calc_pnl(trade.side, trade.entry_price, exit_price, trade.size_usd)

                exit_time = datetime.utcnow()
                trade.exit_price = exit_price
                trade.exit_time = exit_time
                trade.pnl_usd = pnl
                trade.status = "closed"
                session.commit()
                logger.info(f"Closed trade_id={trade.id} exit_price={exit_price:.2f} pnl={pnl:.2f}")

                closed_trades.append({
                    "trade_id": trade.id,
                    "coin": trade.coin,
                    "side": trade.side,
                    "entry_price": trade.entry_price,
                    "exit_price": exit_price,
                    "pnl_usd": pnl,
                    "entry_time": trade.entry_time,
                    "exit_time": exit_time,
                    "archive_dir": f"/app/charts/trade_{trade.id}",
                })

            except RuntimeError as e:
                # Position partially/not closed — keep as "open" so next cycle retries
                logger.error(f"Close incomplete for trade_id={trade.id}: {e}")
                session.commit()
            except Exception as e:
                logger.error(f"Failed to close trade_id={trade.id}: {e}")
                trade.status = "error"
                session.commit()

    for trade_info in closed_trades:
        trigger_reflection(trade_info)


def _get_remaining_position(info, coin: str) -> float | None:
    """Return the absolute remaining position size on Hyperliquid, or None if no position."""
    state = info.user_state(settings.hyperliquid_main_address)
    positions = state.get("assetPositions", [])
    pos = next(
        (p["position"] for p in positions if p["position"]["coin"] == coin),
        None,
    )
    if pos is None:
        return None
    szi = float(pos.get("szi", "0"))
    return abs(szi) if szi != 0 else None


def _close_position(trade: Trade) -> float:
    """
    Execute a close order on Hyperliquid.
    1. Try limit order at current mid for up to close_limit_timeout_secs.
    2. Fall back to market_close().
    3. Verify position is actually closed; retry market close if not.
    Returns the exit price.
    Raises RuntimeError if position cannot be fully closed.
    """
    import eth_account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    account = eth_account.Account.from_key(settings.hyperliquid_private_key)
    info = Info(settings.api_url, skip_ws=True)
    exchange = Exchange(
        account,
        settings.api_url,
        account_address=settings.hyperliquid_main_address,
    )

    coin = trade.coin
    qty = trade.qty
    is_buy_to_close = trade.side == "short"  # Close short = buy; close long = sell

    # Get current mid price for limit order
    mids = info.all_mids()
    mid = round(float(mids[coin]), 1)
    exit_price = mid  # Track best known exit price

    logger.info(f"Placing limit close: {coin} is_buy={is_buy_to_close} qty={qty} px={mid}")

    order_result = exchange.order(
        coin,
        is_buy_to_close,
        qty,
        mid,
        {"limit": {"tif": "Gtc"}},
        reduce_only=True,
    )

    oid = None
    statuses = (
        order_result.get("response", {}).get("data", {}).get("statuses", [])
    )
    if statuses:
        s = statuses[0]
        if "filled" in s:
            exit_price = float(s["filled"]["avgPx"])
        elif "resting" in s:
            oid = s["resting"]["oid"]

    # Wait for limit order fill
    if oid:
        deadline = time.time() + settings.close_limit_timeout_secs
        while time.time() < deadline:
            time.sleep(5)
            open_orders = info.open_orders(settings.hyperliquid_main_address)
            if not any(o.get("oid") == oid for o in open_orders):
                logger.info("Limit close order no longer resting.")
                mids2 = info.all_mids()
                exit_price = float(mids2[coin])
                break
        else:
            # Not filled in time — cancel
            logger.info("Limit close timeout — cancelling...")
            exchange.cancel(coin, oid)
            time.sleep(1)

    # Check if position is actually closed
    remaining = _get_remaining_position(info, coin)
    if remaining is not None:
        logger.warning(
            f"Position still open after limit close: {coin} remaining={remaining}"
        )
        # Retry with market close (up to 3 attempts with increasing slippage)
        for attempt, slippage in enumerate([0.01, 0.03, 0.05], 1):
            logger.info(
                f"Market close retry {attempt}/3: {coin} sz={remaining} slippage={slippage}"
            )
            market_result = exchange.market_close(coin, sz=remaining, slippage=slippage)
            statuses = (
                market_result.get("response", {}).get("data", {}).get("statuses", [])
            )
            if statuses and "filled" in statuses[0]:
                exit_price = float(statuses[0]["filled"]["avgPx"])

            time.sleep(2)
            remaining = _get_remaining_position(info, coin)
            if remaining is None:
                logger.info(f"Position fully closed on attempt {attempt}.")
                break
        else:
            # All retries exhausted — position still open
            raise RuntimeError(
                f"Failed to fully close {coin} position after 3 market-close retries. "
                f"Remaining size: {remaining}"
            )

    return exit_price
