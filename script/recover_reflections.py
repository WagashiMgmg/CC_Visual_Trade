"""
One-shot recovery script for reflections broken by the callable|None bug.

Fixes:
  - Hold opportunities stuck in 'checked' without a reflection file → trigger directly
    (does NOT reset to 'pending' to avoid 23 simultaneous scheduler launches)
  - Closed trades with chart archives but no reflection file → trigger_reflection

Run inside Docker:
  docker compose exec -w /app app python script/recover_reflections.py
"""

import logging
import os
import sys
import time

sys.path.insert(0, "/app")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("recover_reflections")

REFLECTIONS_DIR = "/app/data/reflections"
CHARTS_DIR = "/app/charts"


def recover_holds():
    """Reset 'checked' holds without reflection files back to 'pending'.

    Note: the reflection_executor semaphore (max 2 concurrent) prevents
    rate limiting even when the scheduler processes many holds at once.
    'pending' holds are left for the scheduler to process naturally.
    """
    from src.database import get_session, HoldOpportunity

    with get_session() as session:
        checked = (
            session.query(HoldOpportunity)
            .filter(HoldOpportunity.status == "checked")
            .order_by(HoldOpportunity.id)
            .all()
        )
        reset_ids = []
        for opp in checked:
            reflection_path = f"{REFLECTIONS_DIR}/hold_{opp.id}.md"
            if not os.path.exists(reflection_path):
                opp.status = "pending"
                reset_ids.append(opp.id)
        session.commit()

    if reset_ids:
        logger.info(f"Reset {len(reset_ids)} hold(s) to 'pending': {reset_ids}")
    else:
        logger.info("No stranded hold reflections found")


def recover_trades():
    import threading
    from src.database import get_session, Trade
    from src.reflection import _build_reflection_prompt, _lookup_all_cycles, REFLECTIONS_DIR as REFL_DIR
    from src.reflection_executor import _run_reflection
    from src.trader import get_fee_rate_pct

    with get_session() as session:
        closed_trades = (
            session.query(Trade)
            .filter(Trade.status == "closed")
            .order_by(Trade.id)
            .all()
        )
        stranded = []
        for t in closed_trades:
            archive_dir = f"{CHARTS_DIR}/trade_{t.id}"
            reflection_path = f"{REFLECTIONS_DIR}/trade_{t.id}.md"
            if os.path.isdir(archive_dir) and not os.path.exists(reflection_path):
                stranded.append({
                    "trade_id": t.id,
                    "coin": t.coin,
                    "side": t.side,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "pnl_usd": t.pnl_usd,
                    "size_usd": t.size_usd,
                    "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                    "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                    "archive_dir": archive_dir,
                    "entry_fee": t.entry_fee,
                    "exit_fee": t.exit_fee,
                })

    if not stranded:
        logger.info("No stranded trade reflections found")
        return

    logger.info(f"Found {len(stranded)} stranded trade(s): {[t['trade_id'] for t in stranded]}")
    fee_rate_pct = get_fee_rate_pct()
    threads = []
    for trade_info in stranded:
        trade_id = trade_info["trade_id"]
        archive_dir = trade_info["archive_dir"]
        cycle_history = _lookup_all_cycles(trade_id)
        prompt = _build_reflection_prompt(trade_info, cycle_history, fee_rate_pct)
        chart_paths = [
            f"{archive_dir}/{f}" for f in os.listdir(archive_dir) if f.endswith(".png")
        ] if os.path.isdir(archive_dir) else []

        logger.info(f"Launching reflection thread for trade_{trade_id}")
        t = threading.Thread(
            target=_run_reflection,
            args=(
                "trade",
                f"trade_{trade_id}",
                prompt,
                f"{REFLECTIONS_DIR}/trade_{trade_id}.md",
                archive_dir,
                trade_info,
                chart_paths,
                None,
            ),
            daemon=False,  # non-daemon so process stays alive until done
        )
        t.start()
        threads.append(t)
        time.sleep(1)  # stagger

    logger.info(f"Waiting for {len(threads)} reflection thread(s) to complete...")
    for t in threads:
        t.join(timeout=600)  # up to 10 minutes per thread
    logger.info("All trade reflection threads finished")


if __name__ == "__main__":
    logger.info("=== Starting reflection recovery ===")
    recover_holds()
    recover_trades()
    logger.info("=== Recovery dispatch complete. ===")
