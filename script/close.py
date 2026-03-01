#!/usr/bin/env python3
"""Early close script. Called by Claude via Bash when EXIT is decided."""

import sys
sys.path.insert(0, "/app")

from datetime import datetime

from src.config import settings
from src.database import Trade, get_session
from src.trader import calc_pnl


def run():
    with get_session() as session:
        trade = session.query(Trade).filter(Trade.status == "open").first()
        if not trade:
            print("No open position to close.")
            return

        if settings.dry_run:
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
            info = Info(constants.MAINNET_API_URL, skip_ws=True)
            exit_price = float(info.all_mids()[trade.coin])
        else:
            from src.trader import _close_position
            exit_price = _close_position(trade)

        pnl = calc_pnl(trade.side, trade.entry_price, exit_price, trade.size_usd)
        trade.exit_price = exit_price
        trade.exit_time = datetime.utcnow()
        trade.pnl_usd = round(pnl, 4)
        trade.status = "closed"
        session.commit()
        prefix = "[DRY RUN] " if settings.dry_run else ""
        print(f"{prefix}Closed trade_id={trade.id} exit={exit_price:.2f} pnl={pnl:.2f}")


if __name__ == "__main__":
    run()
