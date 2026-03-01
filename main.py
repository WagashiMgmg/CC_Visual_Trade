"""
CC Visual Trade — entry point.
Starts:
  1. APScheduler: trading cycle (every 15 min) + position closer (every 30 sec)
  2. FastAPI: dashboard on port 8080
"""

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.config import settings
from src.dashboard import router as dashboard_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

os.makedirs("/app/charts", exist_ok=True)
os.makedirs("/app/data", exist_ok=True)


# ── Trading jobs ──────────────────────────────────────────────────────────────

def trading_cycle():
    """
    Main trading cycle (runs every 15 minutes):
    1. Generate chart
    2. Call Claude Code CLI → decision
    3. (Claude executes long/short script internally via Bash tool)
    4. Record cycle in DB
    """
    from src.chart import generate_multi_tf_charts
    from src.orchestrator import run_cycle
    from src.trader import get_open_trade

    open_trade = get_open_trade()
    if open_trade:
        logger.info(f"Open position found (trade_id={open_trade.id}), will ask Claude for EXIT/HOLD")

    logger.info("=== Trading cycle start ===")
    try:
        charts = generate_multi_tf_charts(settings.trading_coin)
        if not charts:
            logger.error("No charts generated, aborting cycle")
            return
    except Exception as e:
        logger.error(f"Chart generation failed: {e}")
        return

    try:
        result = run_cycle(charts, open_trade=open_trade)
        logger.info(f"Cycle complete: {result['decision']} — {result['reason'][:60]}")
    except Exception as e:
        logger.error(f"Orchestrator failed: {e}")


def close_check():
    """Check for expired positions every 30 seconds."""
    from src.trader import close_expired_positions
    try:
        close_expired_positions()
    except Exception as e:
        logger.error(f"Close check failed: {e}")


# ── FastAPI app ───────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start scheduler
    # Trading cycle at :00, :15, :30, :45 of every hour
    scheduler.add_job(
        trading_cycle,
        CronTrigger(minute="0,15,30,45"),
        id="trading_cycle",
        name="Trading Cycle",
        max_instances=1,
        coalesce=True,
    )
    # Position close checker every 30 seconds
    scheduler.add_job(
        close_check,
        "interval",
        seconds=30,
        id="close_check",
        name="Position Close Check",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        f"Scheduler started. Coin={settings.trading_coin} "
        f"Size=${settings.position_size_usd} Leverage={settings.leverage}x "
        f"DryRun={settings.dry_run}"
    )

    yield

    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped.")


app = FastAPI(title="CC Visual Trade", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")
app.include_router(dashboard_router)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.dashboard_port,
        log_level="info",
    )
