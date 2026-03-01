"""FastAPI dashboard routes."""

import os
from datetime import datetime, timedelta
from pathlib import Path

import markdown as md
from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from src.database import Cycle, Trade, get_session

router = APIRouter()
templates = Jinja2Templates(directory="/app/templates")


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_stats():
    with get_session() as session:
        closed = session.query(Trade).filter(Trade.status == "closed").all()
        total = len(closed)
        wins = sum(1 for t in closed if (t.pnl_usd or 0) > 0)
        total_pnl = sum(t.pnl_usd or 0 for t in closed)
        win_rate = round(wins / total * 100, 1) if total else 0
        return {
            "total_trades": total,
            "wins": wins,
            "win_rate": win_rate,
            "total_pnl": round(total_pnl, 2),
        }


def _get_open_trade():
    with get_session() as session:
        t = session.query(Trade).filter(Trade.status == "open").first()
        if not t:
            return None
        now = datetime.utcnow()
        elapsed = now - t.entry_time
        close_at = t.entry_time + timedelta(hours=1)
        remaining = max(timedelta(0), close_at - now)
        return {
            "id": t.id,
            "coin": t.coin,
            "side": t.side,
            "entry_price": t.entry_price,
            "qty": t.qty,
            "size_usd": t.size_usd,
            "entry_time": t.entry_time.strftime("%H:%M:%S UTC"),
            "elapsed": str(elapsed).split(".")[0],
            "close_in": str(remaining).split(".")[0],
        }


def _get_latest_cycle():
    with get_session() as session:
        c = session.query(Cycle).order_by(Cycle.id.desc()).first()
        if not c:
            return None
        raw_reason = c.ai_reasoning or ""
        return {
            "timestamp": c.timestamp.strftime("%Y-%m-%d %H:%M UTC"),
            "decision": c.ai_decision,
            "reason": md.markdown(raw_reason) if raw_reason else "—",
            "action": c.action_taken,
            "chart_path": c.chart_path,
        }


def _get_recent_trades(limit=20):
    with get_session() as session:
        trades = (
            session.query(Trade)
            .order_by(Trade.id.desc())
            .limit(limit)
            .all()
        )
        result = []
        for t in trades:
            duration = ""
            if t.exit_time and t.entry_time:
                duration = str(t.exit_time - t.entry_time).split(".")[0]
            result.append({
                "id": t.id,
                "coin": t.coin,
                "side": t.side,
                "size_usd": t.size_usd,
                "qty": t.qty,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl_usd": t.pnl_usd,
                "status": t.status,
                "entry_time": t.entry_time.strftime("%m/%d %H:%M") if t.entry_time else "",
                "duration": duration,
            })
        return result


def _get_pnl_series():
    cumulative = 0.0
    labels = ["Start"]
    values = [0.0]
    with get_session() as session:
        rows = (
            session.query(Trade.pnl_usd, Trade.exit_time)
            .filter(Trade.status == "closed", Trade.pnl_usd.isnot(None))
            .order_by(Trade.exit_time)
            .all()
        )
        for pnl, exit_time in rows:
            cumulative = round(cumulative + (pnl or 0), 2)
            labels.append(exit_time.strftime("%m/%d %H:%M") if exit_time else "")
            values.append(cumulative)
    return {"labels": labels, "values": values}


_TF_ORDER = ["1m", "5m", "15m", "30m", "1h", "1d", "1w", "1M"]


def _get_all_chart_urls() -> list[dict]:
    """Return charts from the latest cycle, ordered by timeframe."""
    charts_dir = Path("/app/charts")
    pngs = sorted(charts_dir.glob("*.png"), key=os.path.getmtime, reverse=True)
    if not pngs:
        return []
    latest_ts = os.path.getmtime(pngs[0])
    cycle: dict[str, str] = {}
    for png in pngs:
        if os.path.getmtime(png) >= latest_ts - 60:
            for tf in _TF_ORDER:
                if f"_{tf}_" in png.name and tf not in cycle:
                    cycle[tf] = f"/charts/{png.name}"
    return [{"interval": tf, "url": cycle[tf]} for tf in _TF_ORDER if tf in cycle]


def _get_latest_chart_url():
    urls = _get_all_chart_urls()
    return urls[0]["url"] if urls else None


def _get_recent_cycles(limit=10):
    with get_session() as session:
        cycles = (
            session.query(Cycle)
            .order_by(Cycle.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "timestamp": c.timestamp.strftime("%m/%d %H:%M"),
                "decision": c.ai_decision,
                "action": c.action_taken,
                "reason": (c.ai_reasoning or "")[:120],
            }
            for c in cycles
        ]


# ── routes ───────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "open_trade": _get_open_trade(),
            "latest_cycle": _get_latest_cycle(),
            "all_charts": _get_all_chart_urls(),
            "stats": _get_stats(),
            "recent_trades": _get_recent_trades(10),
            "recent_cycles": _get_recent_cycles(10),
            "pnl_series": _get_pnl_series(),
        },
    )


@router.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request):
    return templates.TemplateResponse(
        "trades.html",
        {
            "request": request,
            "trades": _get_recent_trades(100),
        },
    )


@router.get("/charts/{filename}")
async def serve_chart(filename: str):
    path = f"/app/charts/{filename}"
    if os.path.isfile(path):
        return FileResponse(path, media_type="image/png")
    return HTMLResponse("Not found", status_code=404)


@router.get("/api/pnl")
async def api_pnl():
    return _get_pnl_series()


@router.get("/api/status")
async def api_status():
    """JSON endpoint for polling updates."""
    return {
        "open_trade": _get_open_trade(),
        "latest_cycle": _get_latest_cycle(),
        "stats": _get_stats(),
        "all_charts": _get_all_chart_urls(),
    }
