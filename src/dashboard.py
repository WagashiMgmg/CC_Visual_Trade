"""FastAPI dashboard routes."""

import os
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from src.config import settings
from collections import defaultdict

from src.database import Cycle, MagiVote, Trade, get_session

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
    from src.trader import get_live_position

    try:
        pos = get_live_position()
    except Exception:
        pos = None

    if not pos:
        return None

    now = datetime.utcnow()
    entry_time = pos["entry_time"]
    if entry_time:
        elapsed = now - entry_time
        close_at = entry_time + timedelta(hours=settings.position_max_hours)
        remaining = max(timedelta(0), close_at - now)
        entry_time_str = entry_time.strftime("%H:%M:%S UTC")
        elapsed_str = str(elapsed).split(".")[0]
        close_in_str = str(remaining).split(".")[0]
    else:
        entry_time_str = "—"
        elapsed_str = "—"
        close_in_str = "—"

    return {
        "id": pos["trade_id"],
        "coin": pos["coin"],
        "side": pos["side"],
        "entry_price": pos["entry_price"],
        "qty": pos["qty"],
        "size_usd": pos["size_usd"],
        "unrealized_pnl": pos["unrealized_pnl"],
        "entry_time": entry_time_str,
        "elapsed": elapsed_str,
        "close_in": close_in_str,
    }


def _get_latest_magi():
    """Return MAGI vote data for the latest cycle."""
    with get_session() as session:
        cycle = (
            session.query(Cycle)
            .filter(Cycle.ai_decision.isnot(None))
            .order_by(Cycle.id.desc())
            .first()
        )
        if not cycle:
            return None
        cycle_id = cycle.id
        consensus = cycle.ai_decision
        action    = cycle.action_taken

        # Fetch votes for this cycle, grouped by agent — take the latest round per agent
        all_votes = (
            session.query(MagiVote)
            .filter(MagiVote.cycle_id == cycle_id)
            .order_by(MagiVote.agent_name, MagiVote.round.desc())
            .all()
        )
        # Keep only the last (highest round) vote per agent
        by_agent: dict[str, MagiVote] = {}
        for v in all_votes:
            if v.agent_name not in by_agent:
                by_agent[v.agent_name] = v

        # Determine how many rounds were used
        rounds_used = max((v.round for v in all_votes), default=0) + 1

        # Count votes that agree with consensus
        def agent_data(name: str) -> dict | None:
            v = by_agent.get(name)
            if not v:
                return None
            agrees = (v.decision == consensus) if consensus else None
            return {
                "decision": v.decision,
                "reasoning": (v.reasoning or "")[:200],
                "agrees": agrees,
            }

        return {
            "cycle_id":  cycle_id,
            "timestamp": cycle.timestamp.strftime("%Y-%m-%d %H:%M UTC"),
            "consensus": consensus,
            "rounds": rounds_used,
            "action": action,
            "melchior":  agent_data("melchior"),
            "balthazar": agent_data("balthazar"),
            "caspar":    agent_data("caspar"),
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
            "reason": raw_reason or "—",
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


def _get_next_cycle_at() -> str | None:
    try:
        import sys
        main = sys.modules.get("__main__")
        if main:
            job = main.scheduler.get_job("trading_cycle")
            if job and job.next_run_time:
                return job.next_run_time.isoformat()
    except Exception:
        pass
    return None


_rules_cache: dict = {"mtime": 0.0, "result": None}


def _get_reflections():
    import markdown as md
    dir_path = Path("/app/data/reflections")
    if not dir_path.exists():
        return []
    trade_files = list(dir_path.glob("trade_*.md"))
    hold_files = list(dir_path.glob("hold_*.md"))
    files = sorted(
        trade_files + hold_files,
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    result = []
    for f in files:
        try:
            text = f.read_text()
            lines = text.strip().splitlines()
            title = lines[0].lstrip("# ").strip() if lines else f.stem
            html_content = md.markdown(text, extensions=["fenced_code", "tables"])
            result.append({"title": title, "html": html_content, "filename": f.name})
        except Exception:
            pass
    return result


def _get_rules():
    """Read AGENTS.md as raw HTML (cached by mtime)."""
    path = "/app/AGENTS.md"
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    if mtime == _rules_cache["mtime"]:
        return _rules_cache["result"]
    try:
        with open(path) as f:
            html = f.read()
    except OSError:
        return None
    updated = datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%d %H:%M UTC")
    result = {"html": html, "updated": updated}
    _rules_cache["mtime"] = mtime
    _rules_cache["result"] = result
    return result


def _get_all_cycles(limit=200):
    """Return full cycle data with votes, linked trades, and reflections."""
    with get_session() as session:
        from src.database import MagiVote, Reflection, Trade

        cycles = (
            session.query(Cycle)
            .order_by(Cycle.id.desc())
            .limit(limit)
            .all()
        )
        result = []
        for c in cycles:
            # Votes grouped by round
            all_votes = (
                session.query(MagiVote)
                .filter(MagiVote.cycle_id == c.id)
                .order_by(MagiVote.round, MagiVote.agent_name)
                .all()
            )
            rounds: dict[int, list] = {}
            for v in all_votes:
                rounds.setdefault(v.round, []).append({
                    "agent": v.agent_name,
                    "decision": v.decision,
                    "reasoning": v.reasoning or "",
                    "timestamp": v.timestamp.strftime("%H:%M:%S") if v.timestamp else "",
                })

            # Linked trade
            trade = session.query(Trade).filter(Trade.cycle_id == c.id).first()
            trade_data = None
            if trade:
                duration = ""
                if trade.exit_time and trade.entry_time:
                    duration = str(trade.exit_time - trade.entry_time).split(".")[0]
                # Reflection
                refl = session.query(Reflection).filter(Reflection.trade_id == trade.id).first()
                trade_data = {
                    "id": trade.id,
                    "side": trade.side,
                    "entry_price": trade.entry_price,
                    "exit_price": trade.exit_price,
                    "pnl_usd": trade.pnl_usd,
                    "status": trade.status,
                    "entry_time": trade.entry_time.strftime("%m/%d %H:%M") if trade.entry_time else "",
                    "duration": duration,
                    "reflection": refl.reflection_text if refl else None,
                }

            result.append({
                "id": c.id,
                "timestamp": c.timestamp.strftime("%Y-%m-%d %H:%M UTC") if c.timestamp else "",
                "coin": c.coin,
                "decision": c.ai_decision,
                "reasoning": c.ai_reasoning or "",
                "action": c.action_taken,
                "skip_reason": c.skip_reason or "",
                "rounds": rounds,
                "trade": trade_data,
            })
        return result


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


def _get_magi_analytics():
    """Compute all MAGI analytics in a single session."""
    agents = ["melchior", "balthazar", "caspar"]
    empty = {
        "total_cycles": 0,
        "dist_overall": {"LONG": 0, "SHORT": 0, "HOLD": 0, "EXIT": 0},
        "dist_per_agent": {a: {"LONG": 0, "SHORT": 0, "HOLD": 0, "EXIT": 0} for a in agents},
        "hold_rate": {"labels": [], "values": []},
        "rounds_dist": [0, 0, 0, 0],
        "initial_agreement": {a: 0 for a in agents},
        "consensus_count": 0,
        "master_override_count": 0,
        "agreement_matrix": {"mel_bal": 0, "mel_cas": 0, "bal_cas": 0},
        "winrate_by_agent": {a: 0 for a in agents},
        "trades_by_agent": {a: 0 for a in agents},
        "response_time": {"labels": [], "melchior": [], "balthazar": [], "caspar": []},
        "avg_response_time": {a: 0 for a in agents},
    }

    with get_session() as session:
        cycles = (
            session.query(Cycle)
            .filter(Cycle.ai_decision.isnot(None))
            .order_by(Cycle.timestamp)
            .all()
        )
        if not cycles:
            return empty

        all_votes = session.query(MagiVote).all()
        trades = (
            session.query(Trade)
            .filter(Trade.status == "closed", Trade.pnl_usd.isnot(None))
            .all()
        )

        # Index votes by cycle_id
        votes_by_cycle = defaultdict(list)
        for v in all_votes:
            votes_by_cycle[v.cycle_id].append(v)

        # Index trades by cycle_id
        trades_by_cycle = {}
        for t in trades:
            if t.cycle_id:
                trades_by_cycle[t.cycle_id] = t

        total_cycles = len(cycles)
        dist_overall = {"LONG": 0, "SHORT": 0, "HOLD": 0, "EXIT": 0}
        dist_per_agent = {a: {"LONG": 0, "SHORT": 0, "HOLD": 0, "EXIT": 0} for a in agents}
        rounds_dist = [0, 0, 0, 0]
        consensus_count = 0
        master_override_count = 0
        initial_agree = {a: 0 for a in agents}
        initial_total = {a: 0 for a in agents}
        # Agreement matrix: pairwise round-0 agreement
        pair_agree = {"mel_bal": 0, "mel_cas": 0, "bal_cas": 0}
        pair_total = {"mel_bal": 0, "mel_cas": 0, "bal_cas": 0}
        # Win rate by agent: when agent's round-0 vote == final decision and trade exists
        agent_wins = {a: 0 for a in agents}
        agent_trade_count = {a: 0 for a in agents}
        # Response time
        resp_labels = []
        resp_times = {a: [] for a in agents}
        resp_sums = {a: 0.0 for a in agents}
        resp_counts = {a: 0 for a in agents}
        # Hold rate rolling
        hold_flags = []

        for cycle in cycles:
            decision = cycle.ai_decision
            if decision in dist_overall:
                dist_overall[decision] += 1

            cvotes = votes_by_cycle.get(cycle.id, [])
            if not cvotes:
                hold_flags.append(1 if decision == "HOLD" else 0)
                continue

            # Max round for this cycle
            max_round = max(v.round for v in cvotes)
            if max_round < len(rounds_dist):
                rounds_dist[max_round] += 1

            # Round 0 votes per agent
            r0_by_agent = {}
            for v in cvotes:
                if v.round == 0 and v.agent_name in agents:
                    r0_by_agent[v.agent_name] = v
                    d = v.decision
                    if d in dist_per_agent[v.agent_name]:
                        dist_per_agent[v.agent_name][d] += 1

            # Initial agreement with final decision
            for a in agents:
                if a in r0_by_agent:
                    initial_total[a] += 1
                    if r0_by_agent[a].decision == decision:
                        initial_agree[a] += 1

            # Pairwise agreement (round 0)
            if "melchior" in r0_by_agent and "balthazar" in r0_by_agent:
                pair_total["mel_bal"] += 1
                if r0_by_agent["melchior"].decision == r0_by_agent["balthazar"].decision:
                    pair_agree["mel_bal"] += 1
            if "melchior" in r0_by_agent and "caspar" in r0_by_agent:
                pair_total["mel_cas"] += 1
                if r0_by_agent["melchior"].decision == r0_by_agent["caspar"].decision:
                    pair_agree["mel_cas"] += 1
            if "balthazar" in r0_by_agent and "caspar" in r0_by_agent:
                pair_total["bal_cas"] += 1
                if r0_by_agent["balthazar"].decision == r0_by_agent["caspar"].decision:
                    pair_agree["bal_cas"] += 1

            # Consensus vs master override
            final_round_votes = [v for v in cvotes if v.round == max_round]
            agree_count = sum(1 for v in final_round_votes if v.decision == decision)
            if agree_count > len(final_round_votes) / 2:
                consensus_count += 1
            else:
                master_override_count += 1

            # Win rate by agent (round 0 vote == final decision, trade closed)
            trade = trades_by_cycle.get(cycle.id)
            if trade:
                for a in agents:
                    if a in r0_by_agent and r0_by_agent[a].decision == decision:
                        agent_trade_count[a] += 1
                        if (trade.pnl_usd or 0) > 0:
                            agent_wins[a] += 1

            # Response time (round 0)
            if cycle.timestamp:
                label = cycle.timestamp.strftime("%m/%d %H:%M")
                resp_labels.append(label)
                for a in agents:
                    if a in r0_by_agent and r0_by_agent[a].timestamp and cycle.timestamp:
                        diff = (r0_by_agent[a].timestamp - cycle.timestamp).total_seconds()
                        diff = max(0, diff)
                        resp_times[a].append(round(diff, 1))
                        resp_sums[a] += diff
                        resp_counts[a] += 1
                    else:
                        resp_times[a].append(None)

            hold_flags.append(1 if decision == "HOLD" else 0)

        # Hold rate: 10-cycle rolling average
        hold_labels = []
        hold_values = []
        window = 10
        for i in range(len(cycles)):
            start = max(0, i - window + 1)
            window_flags = hold_flags[start:i + 1]
            rate = round(sum(window_flags) / len(window_flags) * 100, 1)
            hold_labels.append(cycles[i].timestamp.strftime("%m/%d %H:%M") if cycles[i].timestamp else str(i))
            hold_values.append(rate)

        return {
            "total_cycles": total_cycles,
            "dist_overall": dist_overall,
            "dist_per_agent": dist_per_agent,
            "hold_rate": {"labels": hold_labels, "values": hold_values},
            "rounds_dist": rounds_dist,
            "initial_agreement": {
                a: round(initial_agree[a] / initial_total[a] * 100, 1) if initial_total[a] else 0
                for a in agents
            },
            "consensus_count": consensus_count,
            "master_override_count": master_override_count,
            "agreement_matrix": {
                k: round(pair_agree[k] / pair_total[k] * 100, 1) if pair_total[k] else 0
                for k in pair_agree
            },
            "winrate_by_agent": {
                a: round(agent_wins[a] / agent_trade_count[a] * 100, 1) if agent_trade_count[a] else 0
                for a in agents
            },
            "trades_by_agent": agent_trade_count,
            "response_time": {
                "labels": resp_labels,
                "melchior": resp_times["melchior"],
                "balthazar": resp_times["balthazar"],
                "caspar": resp_times["caspar"],
            },
            "avg_response_time": {
                a: round(resp_sums[a] / resp_counts[a], 1) if resp_counts[a] else 0
                for a in agents
            },
        }


# ── routes ───────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    from src import state
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "open_trade": _get_open_trade(),
            "latest_cycle": _get_latest_cycle(),
            "magi": _get_latest_magi(),
            "all_charts": _get_all_chart_urls(),
            "stats": _get_stats(),
            "recent_trades": _get_recent_trades(10),
            "recent_cycles": _get_recent_cycles(10),
            "pnl_series": _get_pnl_series(),
            "cycle_running": state.cycle_running,
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


@router.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request):
    return templates.TemplateResponse(
        "rules.html",
        {"request": request, "rules": _get_rules()},
    )


@router.get("/reflections", response_class=HTMLResponse)
async def reflections_page(request: Request):
    return templates.TemplateResponse(
        "reflections.html",
        {"request": request, "reflections": _get_reflections()},
    )


@router.get("/cycles", response_class=HTMLResponse)
async def cycles_page(request: Request):
    return templates.TemplateResponse(
        "cycles.html",
        {
            "request": request,
            "cycles": _get_all_cycles(200),
        },
    )


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    return templates.TemplateResponse(
        "stats.html",
        {"request": request, "analytics": _get_magi_analytics(), "page": "stats"},
    )


@router.get("/api/status")
async def api_status():
    """JSON endpoint for polling updates."""
    from src import state
    return {
        "open_trade": _get_open_trade(),
        "latest_cycle": _get_latest_cycle(),
        "magi": _get_latest_magi(),
        "stats": _get_stats(),
        "all_charts": _get_all_chart_urls(),
        "next_cycle_at": _get_next_cycle_at(),
        "cycle_running": state.cycle_running,
    }
