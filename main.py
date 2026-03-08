"""
CC Visual Trade — entry point.
Starts:
  1. APScheduler: trading cycle (every 15 min) + position closer (every 30 sec)
  2. FastAPI: dashboard on port 8080
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.config import settings
from src.dashboard import router as dashboard_router
from src.discord_bot import start_bot

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
    from src.trader import get_live_position

    live_pos = get_live_position()
    if live_pos:
        logger.info(
            f"Open position found (trade_id={live_pos['trade_id']}, "
            f"side={live_pos['side']}, HL source), will ask Claude for EXIT/HOLD"
        )

    from src import state

    logger.info("=== Trading cycle start ===")
    try:
        charts, freshness = generate_multi_tf_charts(
            settings.trading_coin,
            entry_price=live_pos["entry_price"] if live_pos else None,
            entry_time=live_pos["entry_time"] if live_pos else None,
            side=live_pos["side"] if live_pos else None,
        )
        if not charts:
            logger.error("No charts generated, aborting cycle")
            return
    except Exception as e:
        logger.error(f"Chart generation failed: {e}")
        return

    from src.chart import format_cross_freshness
    freshness_text = format_cross_freshness(freshness)

    state.cycle_running = True
    try:
        result = run_cycle(charts, live_position=live_pos, freshness_text=freshness_text)
        logger.info(f"Cycle complete: {result['decision']} — {result['reason'][:60]}")
    except Exception as e:
        logger.error(f"Orchestrator failed: {e}")
    finally:
        state.cycle_running = False


def close_check():
    """Check for expired positions and DB/HL sync every 30 seconds."""
    from src.trader import close_expired_positions, sync_position_state
    try:
        close_expired_positions()
    except Exception as e:
        logger.error(f"Close check failed: {e}")
    try:
        sync_position_state()
    except Exception as e:
        logger.error(f"Position sync failed: {e}")


def emergency_check():
    """Monitor position thresholds and trigger emergency MAGI if breached."""
    from src.emergency import check_emergency
    try:
        check_emergency()
    except Exception as e:
        logger.error(f"Emergency check failed: {e}")


def hold_opportunity_check():
    """Check if any past HOLD decisions missed profitable opportunities."""
    from src.hold_reflection import check_pending_opportunities
    try:
        check_pending_opportunities()
    except Exception as e:
        logger.error(f"Hold opportunity check failed: {e}")


def early_exit_check():
    """Check if any early agent-EXIT decisions would have been better by holding."""
    from src.early_exit_reflection import check_pending_early_exits
    try:
        check_pending_early_exits()
    except Exception as e:
        logger.error(f"Early exit check failed: {e}")


def digest_curation():
    """Curate reflection digest (runs every 12 hours)."""
    from src.digest import curate_digest
    try:
        curate_digest()
    except Exception as e:
        logger.error(f"Digest curation failed: {e}")


def reflection_retry():
    """Retry failed reflections (runs every 30 minutes)."""
    from src.reflection_executor import retry_pending_reflections
    try:
        retry_pending_reflections()
    except Exception as e:
        logger.error(f"Reflection retry failed: {e}")


# ── FastAPI app ───────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone="UTC")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start scheduler
    # Trading cycle at :00, :15, :30, :45 of every hour
    scheduler.add_job(
        trading_cycle,
        IntervalTrigger(minutes=settings.cycle_interval_minutes),
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
    # Emergency position monitor every 30 seconds
    scheduler.add_job(
        emergency_check,
        "interval",
        seconds=30,
        id="emergency_check",
        name="Emergency Position Monitor",
        max_instances=1,
        coalesce=True,
    )
    # Missed-opportunity checker every 30 minutes
    scheduler.add_job(
        hold_opportunity_check,
        IntervalTrigger(minutes=30),
        id="hold_opportunity_check",
        name="Hold Opportunity Check",
        max_instances=1,
        coalesce=True,
    )
    # Early-exit analysis every 30 minutes
    scheduler.add_job(
        early_exit_check,
        IntervalTrigger(minutes=30),
        id="early_exit_check",
        name="Early Exit Analysis",
        max_instances=1,
        coalesce=True,
    )
    # Reflection retry every 30 minutes
    scheduler.add_job(
        reflection_retry,
        IntervalTrigger(minutes=30),
        id="reflection_retry",
        name="Reflection Retry",
        max_instances=1,
        coalesce=True,
    )
    # Reflection digest curation at 03:00 and 15:00 UTC
    scheduler.add_job(
        digest_curation,
        CronTrigger(hour="3,15", minute=0, timezone="UTC"),
        id="digest_curation",
        name="Reflection Digest Curation",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info(
        f"Scheduler started. Coin={settings.trading_coin} "
        f"Size=${settings.position_size_usd} Leverage={settings.leverage}x "
        f"DryRun={settings.dry_run}"
    )

    bot_task = asyncio.create_task(start_bot())

    yield

    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped.")
    bot_task.cancel()


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
