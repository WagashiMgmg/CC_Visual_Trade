"""
Position manager: closes positions that have been open for >= 1 hour.
Strategy: try limit order first, fall back to market order.
"""

import logging
import time
from datetime import datetime, timedelta

from src.config import settings
from src.database import Trade, get_session

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

                trade.exit_price = exit_price
                trade.exit_time = datetime.utcnow()
                trade.pnl_usd = pnl
                trade.status = "closed"
                session.commit()
                logger.info(f"Closed trade_id={trade.id} exit_price={exit_price:.2f} pnl={pnl:.2f}")

            except Exception as e:
                logger.error(f"Failed to close trade_id={trade.id}: {e}")
                trade.status = "error"
                session.commit()


def _close_position(trade: Trade) -> float:
    """
    Execute a close order on Hyperliquid.
    1. Try limit order at current mid for up to close_limit_timeout_secs.
    2. Fall back to market_close().
    Returns the exit price.
    """
    import eth_account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants

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
            return float(s["filled"]["avgPx"])
        elif "resting" in s:
            oid = s["resting"]["oid"]

    # Wait for fill
    if oid:
        deadline = time.time() + settings.close_limit_timeout_secs
        while time.time() < deadline:
            time.sleep(5)
            open_orders = info.open_orders(settings.hyperliquid_main_address)
            if not any(o.get("oid") == oid for o in open_orders):
                logger.info("Limit close filled!")
                # Approximate fill price (actual fill may differ slightly)
                mids2 = info.all_mids()
                return float(mids2[coin])

        # Not filled — cancel and use market close
        logger.info("Limit close timeout — switching to market close...")
        exchange.cancel(coin, oid)
        time.sleep(1)

    market_result = exchange.market_close(coin, sz=qty, slippage=0.01)
    statuses = (
        market_result.get("response", {}).get("data", {}).get("statuses", [])
    )
    if statuses and "filled" in statuses[0]:
        return float(statuses[0]["filled"]["avgPx"])

    # Ultimate fallback: return current mid
    mids3 = info.all_mids()
    return float(mids3[coin])
