#!/usr/bin/env python3
"""
Short entry script.
Places a limit sell order, waits for fill, falls back to market order.
Records the open position in DB.
Called by Claude Code via the /short skill or directly.
"""

import logging
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, "/app")

from src.config import settings
from src.database import Trade, get_session
from src.reflection import archive_charts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SHORT] %(message)s",
)
logger = logging.getLogger(__name__)


def run():
    coin = settings.trading_coin
    size_usd = settings.position_size_usd

    cycle_id_env = os.environ.get("CYCLE_ID")
    cycle_id = int(cycle_id_env) if cycle_id_env else None

    if settings.dry_run:
        import eth_account
        from src.config import make_info
        info = make_info()
        mid = float(info.all_mids()[coin])
        sz_decimals = next((a["szDecimals"] for a in info.meta()["universe"] if a["name"] == coin), 3)
        qty = round(size_usd / mid, sz_decimals)
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
                cycle_id=cycle_id,
            )
            session.add(trade)
            session.commit()
            trade_id = trade.id
        archive_charts(trade_id, coin)
        print(f"SHORT recorded (DRY RUN): entry_price={mid} trade_id={trade_id}")
        return

    import eth_account
    from src.config import make_info, make_exchange

    account = eth_account.Account.from_key(settings.hyperliquid_private_key)
    info = make_info()
    exchange = make_exchange(account)

    # Set leverage
    exchange.update_leverage(settings.leverage, coin, is_cross=True)

    # Get mid price and sz_decimals for proper rounding
    mids = info.all_mids()
    mid = float(mids[coin])
    sz_decimals = next((a["szDecimals"] for a in info.meta()["universe"] if a["name"] == coin), 3)
    qty = round(size_usd / mid, sz_decimals)
    limit_px = round(mid, 0)

    logger.info(f"Placing SHORT limit: {coin} qty={qty} (sz_decimals={sz_decimals}) px={limit_px}")
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

    # Get entry fee from fill
    from src.trader import get_fill_fee
    entry_fee = get_fill_fee(coin, oid)

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
            cycle_id=cycle_id,
            entry_fee=entry_fee,
        )
        session.add(trade)
        session.commit()
        trade_id = trade.id

    archive_charts(trade_id, coin)
    print(f"SHORT executed: entry_price={entry_price} qty={qty} order_id={oid} trade_id={trade_id} fee={entry_fee}")
    logger.info(f"Trade recorded: id={trade_id}")


if __name__ == "__main__":
    run()
