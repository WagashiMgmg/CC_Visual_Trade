"""
Orchestrator: coordinates MAGI multi-agent voting and trade execution.

Claude Code reads charts via ClaudeAgent; Gemini via GeminiAgent.
Python executes the trade scripts after consensus is reached.
"""

import logging
import os
import subprocess
from datetime import datetime

from src.config import settings
from src.database import Cycle, get_session
from src.magi import MagiSystem
from src.notify import send_discord
from src.trader import calc_pnl

logger = logging.getLogger(__name__)

_CONTEXT_FILE = "/app/prompt/context.md"

_PROMPT_TEMPLATE = """\
{context}

---

過去の振り返りが `/app/data/reflections/` に蓄積されています。
Bash で `ls /app/data/reflections/` を確認し、関連しそうなファイルを Read ツールで参照してから判断してください。

以下の{count}つのチャートを Read ツールで開いてください:

{chart_list}

チャートを見てトレード判断をしてください。

最後に必ず以下のフォーマットで出力すること:
DECISION: LONG or SHORT or HOLD
REASON: （日本語で理由を記述）
"""

_PROMPT_IN_POSITION = """\
{context}

---

過去の振り返りが `/app/data/reflections/` に蓄積されています。
Bash で `ls /app/data/reflections/` を確認し、関連しそうなファイルを Read ツールで参照してから判断してください。

現在ポジション保有中:
- サイド: {side}
- エントリー価格: ${entry_price:,.2f}
- 保有時間: {elapsed}
- 含み損益: {pnl_sign}${pnl_usd:.2f}（推定）

以下の{count}つのチャートを Read ツールで開いてください:
{chart_list}

チャートを見て、現在のポジションをどうするか判断してください。

最後に必ず以下のフォーマットで出力すること:
DECISION: EXIT or HOLD
REASON: （日本語で理由を記述）
"""

_PROMPT_EMERGENCY = """\
{context}

---

🚨 **緊急MAGI集会** 🚨
通常のサイクルを中断し、緊急招集されました。

**トリガー**: {emergency_reason}

過去の振り返りが `/app/data/reflections/` に蓄積されています。
Bash で `ls /app/data/reflections/` を確認し、関連しそうなファイルを Read ツールで参照してから判断してください。

現在ポジション保有中:
- サイド: {side}
- エントリー価格: ${entry_price:,.2f}
- 保有時間: {elapsed}
- 含み損益: {pnl_sign}${pnl_usd:.2f}（推定）

以下の{count}つのチャートを Read ツールで開いてください:
{chart_list}

**これは緊急事態です。** チャートと上記の状況を総合的に分析し、即座にポジションを閉じるべきか慎重に判断してください。
リスク管理を最優先に、迅速かつ正確な判断を行ってください。

最後に必ず以下のフォーマットで出力すること:
DECISION: EXIT or HOLD
REASON: （日本語で理由を記述。緊急トリガーに対する見解も含めること）
"""


def _fetch_mid(coin: str) -> float:
    """Fetch current mid price from Hyperliquid."""
    from hyperliquid.info import Info
    info = Info(settings.api_url, skip_ws=True)
    return float(info.all_mids()[coin])


def _load_context() -> str:
    try:
        with open(_CONTEXT_FILE) as f:
            content = f.read().strip()
        return content.format(
            position_max_hours=settings.position_max_hours,
            cycle_interval_minutes=settings.cycle_interval_minutes,
        )
    except FileNotFoundError:
        logger.warning(f"Context file not found: {_CONTEXT_FILE}")
        return ""


def _build_chart_list(charts: list[tuple[str, str, str]]) -> str:
    lines = []
    for i, (interval, label, path) in enumerate(charts, 1):
        lines.append(f"{i}. {label}: {path}")
    return "\n".join(lines)


def run_cycle(
    charts: list[tuple[str, str, str]],
    live_position: dict | None = None,
    emergency: str | None = None,
) -> dict:
    """
    Run one trading cycle using the MAGI multi-agent voting system.
    charts: list of (interval, label, file_path) from generate_multi_tf_charts()
    live_position: dict from get_live_position() (HL source of truth), or None.
    emergency: if set, the reason string for the emergency trigger.
    Returns dict with 'decision' and 'reasoning'.
    """
    coin = settings.trading_coin
    chart_paths = [path for _, _, path in charts]
    chart_list_str = _build_chart_list(charts)
    context = _load_context()
    in_position = live_position is not None

    if in_position:
        now = datetime.utcnow()
        entry_time = live_position.get("entry_time")
        if entry_time:
            elapsed_str = f"{int((now - entry_time).total_seconds() // 60)}分"
        else:
            elapsed_str = "不明"

        # Use HL unrealized_pnl directly if available, otherwise calculate
        if live_position.get("unrealized_pnl") is not None:
            pnl = live_position["unrealized_pnl"]
        else:
            try:
                current_price = _fetch_mid(live_position["coin"])
            except Exception as e:
                logger.warning(f"Failed to fetch mid price: {e}. Using entry price.")
                current_price = live_position["entry_price"]
            pnl = calc_pnl(
                live_position["side"], live_position["entry_price"],
                current_price, live_position["size_usd"],
            )

        prompt_vars = dict(
            context=context,
            side=live_position["side"].upper(),
            entry_price=live_position["entry_price"],
            elapsed=elapsed_str,
            pnl_sign="+" if pnl >= 0 else "",
            pnl_usd=abs(pnl),
            count=len(charts),
            chart_list=chart_list_str,
        )

        if emergency:
            prompt_vars["emergency_reason"] = emergency
            base_prompt = _PROMPT_EMERGENCY.format(**prompt_vars)
        else:
            base_prompt = _PROMPT_IN_POSITION.format(**prompt_vars)
        cycle_type = "EMERGENCY" if emergency else "MAGI"
        logger.info(
            f"[{cycle_type}] Starting cycle (IN POSITION: {live_position['side'].upper()}) "
            f"with {len(charts)} timeframe charts"
        )
    else:
        base_prompt = _PROMPT_TEMPLATE.format(
            context=context,
            count=len(charts),
            chart_list=chart_list_str,
        )
        logger.info(f"[MAGI] Starting cycle with {len(charts)} timeframe charts")

    # 15m チャートパスをダッシュボード表示用に保存
    primary_chart = next((p for _, _, p in charts if "15m" in p), charts[0][2] if charts else "")

    # Cycle を MAGI より前に作成し ID を確保
    with get_session() as session:
        cycle = Cycle(
            timestamp=datetime.utcnow(),
            coin=coin,
            chart_path=primary_chart,
        )
        session.add(cycle)
        session.commit()
        cycle_id = cycle.id

    # Chart refresh callback for re-deliberation rounds
    from src.chart import generate_multi_tf_charts

    def _refresh_charts() -> list[str]:
        pos = live_position
        new_charts = generate_multi_tf_charts(
            coin,
            entry_price=pos["entry_price"] if pos else None,
            entry_time=pos["entry_time"] if pos else None,
            side=pos["side"] if pos else None,
        )
        logger.info(f"[MAGI] Chart refresh: {len(new_charts)} charts regenerated")
        return [path for _, _, path in new_charts]

    # Run MAGI voting
    magi = MagiSystem()
    magi_result = magi.run(
        base_prompt=base_prompt,
        charts=chart_paths,
        cycle_id=cycle_id,
        in_position=in_position,
        chart_fn=_refresh_charts,
    )

    decision  = magi_result["decision"]
    reasoning = magi_result["reasoning"]
    rounds    = magi_result["rounds"]
    adopted   = magi_result["adopted_by"]

    logger.info(
        f"[MAGI] Final: {decision} | rounds={rounds} | adopted_by={adopted} | "
        f"reason: {reasoning[:100]}"
    )

    # Check for auth errors in any vote output
    all_raw = " ".join(
        v.get("raw_output", "") for v in magi_result["votes"].values()
    )
    if "OAuth token has expired" in all_raw or "authentication_error" in all_raw:
        logger.error("Claude OAuth token expired — sending Discord alert")
        send_discord(
            title="⚠️ CC Visual Trade — 認証エラー",
            message=(
                "Claude の OAuth トークンが期限切れです。\n\n"
                "`claude auth login` でホスト側を再認証してください。\n\n"
                "または `.env` に `ANTHROPIC_API_KEY` を設定すると恒久的に解決します。"
            ),
            color=0xFF0000,
        )

    # Python executes trade scripts (agents no longer call them directly)
    env = {**os.environ, "CYCLE_ID": str(cycle_id)}
    if decision == "LONG":
        subprocess.run(["python", "/app/script/long.py"], env=env)
    elif decision == "SHORT":
        subprocess.run(["python", "/app/script/short.py"], env=env)
    elif decision == "EXIT":
        subprocess.run(["python", "/app/script/close.py"])
    # HOLD: do nothing

    # Update Cycle record
    with get_session() as session:
        cycle = session.query(Cycle).filter(Cycle.id == cycle_id).first()
        if cycle:
            cycle.ai_decision  = decision
            cycle.ai_reasoning = reasoning
            cycle.action_taken = decision.lower()
            # Store summary of all votes as raw output
            votes_summary = "\n\n".join(
                f"=== {name.upper()} ===\n{v.get('raw_output', '')[:1000]}"
                for name, v in magi_result["votes"].items()
            )
            cycle.claude_raw_output = votes_summary[:5000]
            session.commit()

    return {"decision": decision, "reason": reasoning}
