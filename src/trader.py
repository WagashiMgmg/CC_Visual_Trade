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
    """Return the currently open Trade from DB, or None."""
    with get_session() as session:
        trade = session.query(Trade).filter(Trade.status == "open").first()
        if trade:
            # Detach from session by accessing needed fields
            session.expunge(trade)
        return trade


def get_live_position() -> dict | None:
    """
    Return the current position using Hyperliquid as the source of truth,
    enriched with DB metadata (trade_id, entry_time).
    Returns None if no position exists on HL.
    Falls back to DB-only in dry_run mode.

    Keys: coin, side, qty, entry_price, size_usd, unrealized_pnl,
          trade_id, entry_time  (last two from DB, may be None)
    """
    if settings.dry_run:
        # dry_run: no real HL position, use DB
        trade = get_open_trade()
        if trade is None:
            return None
        return {
            "coin": trade.coin,
            "side": trade.side,
            "qty": trade.qty,
            "entry_price": trade.entry_price,
            "size_usd": trade.size_usd,
            "unrealized_pnl": None,
            "trade_id": trade.id,
            "entry_time": trade.entry_time,
        }

    from hyperliquid.info import Info

    coin = settings.trading_coin
    info = Info(settings.api_url, skip_ws=True)
    hl_pos = _get_hl_position(info, coin)

    if hl_pos is None:
        return None

    szi = float(hl_pos["szi"])
    side = "long" if szi > 0 else "short"
    qty = abs(szi)
    entry_price = float(hl_pos.get("entryPx", 0))
    unrealized_pnl = float(hl_pos.get("unrealizedPnl", 0))

    # Enrich with DB metadata
    trade = get_open_trade()
    trade_id = trade.id if trade else None
    entry_time = trade.entry_time if trade else None
    size_usd = trade.size_usd if trade else (entry_price * qty)

    return {
        "coin": coin,
        "side": side,
        "qty": qty,
        "entry_price": entry_price,
        "size_usd": size_usd,
        "unrealized_pnl": unrealized_pnl,
        "trade_id": trade_id,
        "entry_time": entry_time,
    }


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


def _get_hl_position(info, coin: str) -> dict | None:
    """
    Return Hyperliquid position dict for *coin*, or None if flat.
    Keys: szi (signed size string), entryPx, unrealizedPnl, ...
    """
    state = info.user_state(settings.hyperliquid_main_address)
    positions = state.get("assetPositions", [])
    pos = next(
        (p["position"] for p in positions if p["position"]["coin"] == coin),
        None,
    )
    if pos is None:
        return None
    if float(pos.get("szi", "0")) == 0:
        return None
    return pos


def _get_remaining_position(info, coin: str) -> float | None:
    """Return the absolute remaining position size on Hyperliquid, or None if no position."""
    pos = _get_hl_position(info, coin)
    if pos is None:
        return None
    return abs(float(pos["szi"]))


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


def sync_position_state():
    """
    Reconcile DB trade status with actual Hyperliquid position.
    Called every 30 seconds alongside close_expired_positions.

    Handles two mismatch cases:
      A) DB=open but HL=flat  → mark trade as closed (liquidated / manually closed)
      B) DB=no open but HL=has position → orphaned position, attempt market close
    """
    if settings.dry_run:
        return

    from hyperliquid.info import Info

    coin = settings.trading_coin
    info = Info(settings.api_url, skip_ws=True)
    hl_pos = _get_hl_position(info, coin)

    with get_session() as session:
        db_trade = session.query(Trade).filter(Trade.status == "open").first()

        # ── Case A: DB says open, but HL is flat ──
        if db_trade and hl_pos is None:
            mids = info.all_mids()
            exit_price = float(mids[coin])
            pnl = calc_pnl(db_trade.side, db_trade.entry_price, exit_price, db_trade.size_usd)
            exit_time = datetime.utcnow()

            db_trade.exit_price = exit_price
            db_trade.exit_time = exit_time
            db_trade.pnl_usd = pnl
            db_trade.status = "closed"
            session.commit()

            logger.warning(
                f"[SYNC] DB had open trade_id={db_trade.id} but HL position is flat. "
                f"Marked closed (exit≈{exit_price:.2f}, pnl≈{pnl:.2f}). "
                f"Likely liquidated or manually closed."
            )

            trigger_reflection({
                "trade_id": db_trade.id,
                "coin": db_trade.coin,
                "side": db_trade.side,
                "entry_price": db_trade.entry_price,
                "exit_price": exit_price,
                "pnl_usd": pnl,
                "entry_time": db_trade.entry_time,
                "exit_time": exit_time,
                "archive_dir": f"/app/charts/trade_{db_trade.id}",
            })
            return

        # ── Case B: DB has no open trade, but HL has a position ──
        if db_trade is None and hl_pos is not None:
            remaining = abs(float(hl_pos["szi"]))
            logger.warning(
                f"[SYNC] No open trade in DB but HL has {coin} position "
                f"(size={hl_pos['szi']}). Attempting to close orphaned position."
            )

            import eth_account
            from hyperliquid.exchange import Exchange

            account = eth_account.Account.from_key(settings.hyperliquid_private_key)
            exchange = Exchange(
                account,
                settings.api_url,
                account_address=settings.hyperliquid_main_address,
            )

            try:
                result = exchange.market_close(coin, sz=remaining, slippage=0.05)
                statuses = (
                    result.get("response", {}).get("data", {}).get("statuses", [])
                )
                filled = statuses and "filled" in statuses[0]
                logger.info(
                    f"[SYNC] Orphaned position close: filled={filled} result={statuses}"
                )
            except Exception as e:
                logger.error(f"[SYNC] Failed to close orphaned {coin} position: {e}")
