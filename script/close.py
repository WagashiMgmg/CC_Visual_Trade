#!/usr/bin/env python3
"""Early close script. Called by Claude via Bash when EXIT is decided."""

import sys
sys.path.insert(0, "/app")

from datetime import datetime

from src.config import settings
from src.database import Trade, get_session
from src.early_exit_reflection import record_early_exit
from src.late_exit_reflection import check_and_trigger_late_exit
from src.reflection import trigger_reflection
from src.trader import calc_pnl


def _close_hl_orphan() -> None:
    """
    Case B: DBにopen tradeなし、でもHLにポジションがある場合に直接クローズする。
    DBレコードは作成しない（元々存在しないため）。
    """
    from src.trader import _get_remaining_position, get_live_position
    import time

    live = get_live_position()
    if not live:
        print("No open position to close.")
        return

    coin = live["coin"]
    print(f"[Case B] No DB trade found. HL has {coin} {live['side']} qty={live['qty']}. Closing...")

    if settings.dry_run:
        print(f"[DRY RUN] Would market-close {coin}")
        return

    import eth_account
    from src.config import make_info, make_exchange

    account  = eth_account.Account.from_key(settings.hyperliquid_private_key)
    info     = make_info()
    exchange = make_exchange(account)

    for attempt, slippage in enumerate([0.01, 0.03, 0.05], 1):
        result = exchange.market_close(coin, slippage=slippage)
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if statuses and "filled" in statuses[0]:
            exit_price = float(statuses[0]["filled"]["avgPx"])
            print(f"[Case B] Closed {coin} at {exit_price:.2f} (attempt {attempt})")
            return
        time.sleep(2)
        if _get_remaining_position(info, coin) is None:
            print(f"[Case B] Position fully closed (attempt {attempt})")
            return

    print(f"[Case B] WARNING: Could not fully close {coin} — check HL manually")


def run():
    trade_info = None

    with get_session() as session:
        trade = session.query(Trade).filter(Trade.status == "open").first()
        if not trade:
            _close_hl_orphan()
            return

        if settings.dry_run:
            from src.config import make_info
            info = make_info()
            exit_price = float(info.all_mids()[trade.coin])
        else:
            from src.trader import _close_position
            try:
                exit_price = _close_position(trade)
            except Exception as e:
                print(f"ERROR: Close incomplete for trade_id={trade.id}: {e}")
                print("Position kept as 'open' — will retry on next cycle.")
                return

        pnl = calc_pnl(trade.side, trade.entry_price, exit_price, trade.size_usd)
        exit_time = datetime.utcnow()
        trade.exit_price = exit_price
        trade.exit_time = exit_time
        trade.pnl_usd = round(pnl, 4)
        trade.status = "closed"
        if not settings.dry_run:
            from src.trader import get_fill_fee
            trade.exit_fee = get_fill_fee(trade.coin)
        session.commit()

        trade_info = {
            "trade_id": trade.id,
            "coin": trade.coin,
            "side": trade.side,
            "entry_price": trade.entry_price,
            "exit_price": exit_price,
            "pnl_usd": round(pnl, 4),
            "size_usd": trade.size_usd,
            "entry_time": trade.entry_time,
            "exit_time": exit_time,
            "archive_dir": f"/app/charts/trade_{trade.id}",
        }

        prefix = "[DRY RUN] " if settings.dry_run else ""
        print(f"{prefix}Closed trade_id={trade.id} exit={exit_price:.2f} pnl={pnl:.2f}")

    if trade_info:
        trigger_reflection(trade_info)
        record_early_exit(trade_info)
        check_and_trigger_late_exit(trade_info)


if __name__ == "__main__":
    run()
