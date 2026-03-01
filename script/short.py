#!/usr/bin/env python3
"""
Short entry script.
Places a limit sell order, waits for fill, falls back to market order.
Records the open position in DB.
Called by Claude Code via the /short skill or directly.
"""

import logging
import sys
import time
from datetime import datetime

sys.path.insert(0, "/app")

from src.config import settings
from src.database import Trade, get_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SHORT] %(message)s",
)
logger = logging.getLogger(__name__)


def run():
    coin = settings.trading_coin
    size_usd = settings.position_size_usd

    if settings.dry_run:
        import eth_account
        from hyperliquid.info import Info
        info = Info(settings.api_url, skip_ws=True)
        mid = float(info.all_mids()[coin])
        qty = round(size_usd / mid, 6)
        logger.info(f"[DRY RUN] Would place SHORT {coin} ${size_usd} @ {mid}")
        with get_session() as session:
            trade = Trade(
                coin=coin,
                side="short",
                size_usd=size_usd,
                qty=qty,
                entry_price=mid,
                entry_order_id=None,
                entry_time=datetime.utcnow(),
                status="open",
            )
            session.add(trade)
            session.commit()
        print(f"SHORT recorded (DRY RUN): entry_price={mid} trade_id={trade.id}")
        return

    import eth_account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    account = eth_account.Account.from_key(settings.hyperliquid_private_key)
    info = Info(settings.api_url, skip_ws=True)
    exchange = Exchange(account, settings.api_url,
                        account_address=settings.hyperliquid_main_address)

    # Set leverage
    exchange.update_leverage(settings.leverage, coin, is_cross=True)

    # Get mid price
    mids = info.all_mids()
    mid = float(mids[coin])
    qty = round(size_usd / mid, 6)
    limit_px = round(mid, 1)

    logger.info(f"Placing SHORT limit: {coin} qty={qty} px={limit_px}")
    order_result = exchange.order(coin, False, qty, limit_px, {"limit": {"tif": "Gtc"}})
    logger.info(f"Order result: {order_result}")

    oid = None
    entry_price = limit_px
    filled = False

    statuses = (
        order_result.get("response", {})
        .get("data", {})
        .get("statuses", [])
    )
    if statuses:
        s = statuses[0]
        if "filled" in s:
            filled = True
            oid = s["filled"]["oid"]
            entry_price = float(s["filled"]["avgPx"])
        elif "resting" in s:
            oid = s["resting"]["oid"]

    if not filled and oid:
        logger.info(f"Waiting for limit fill (oid={oid}, timeout={settings.limit_order_timeout_secs}s)...")
        deadline = time.time() + settings.limit_order_timeout_secs
        while time.time() < deadline:
            time.sleep(3)
            open_orders = info.open_orders(settings.hyperliquid_main_address)
            if not any(o.get("oid") == oid for o in open_orders):
                filled = True
                logger.info("Limit order filled!")
                break

        if not filled:
            logger.info("Limit not filled — cancelling, switching to market order...")
            exchange.cancel(coin, oid)
            time.sleep(1)
            market_result = exchange.market_open(coin, False, qty, None, slippage=0.01)
            statuses = (
                market_result.get("response", {})
                .get("data", {})
                .get("statuses", [])
            )
            if statuses and "filled" in statuses[0]:
                entry_price = float(statuses[0]["filled"]["avgPx"])
                oid = statuses[0]["filled"]["oid"]
                filled = True
            logger.info(f"Market order filled at {entry_price}")

    if not filled:
        logger.error("Failed to fill order — aborting.")
        sys.exit(1)

    with get_session() as session:
        trade = Trade(
            coin=coin,
            side="short",
            size_usd=size_usd,
            qty=qty,
            entry_price=entry_price,
            entry_order_id=oid,
            entry_time=datetime.utcnow(),
            status="open",
        )
        session.add(trade)
        session.commit()
        trade_id = trade.id

    print(f"SHORT executed: entry_price={entry_price} qty={qty} order_id={oid} trade_id={trade_id}")
    logger.info(f"Trade recorded: id={trade_id}")


if __name__ == "__main__":
    run()
