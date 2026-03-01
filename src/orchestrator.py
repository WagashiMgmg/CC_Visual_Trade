"""
Orchestrator: calls `claude -p` with multiple timeframe chart paths.
Claude Code reads all charts, performs multi-timeframe analysis,
decides LONG/SHORT/HOLD, and executes the appropriate script.
"""

import logging
import re
import subprocess
from datetime import datetime

from src.config import settings
from src.database import Cycle, Trade, get_session
from src.notify import send_discord
from src.trader import calc_pnl

logger = logging.getLogger(__name__)

_CONTEXT_FILE = "/app/prompt/context.md"

_PROMPT_TEMPLATE = """\
{context}

---

以下の{count}つのチャートを Read ツールで開いてください:

{chart_list}

チャートを見てトレード判断をしてください。

判断後、以下のアクションを必ず取ること:
- LONG  と判断した場合 → Bash ツールで `python /app/script/long.py` を実行すること
- SHORT と判断した場合 → Bash ツールで `python /app/script/short.py` を実行すること
- HOLD  と判断した場合 → 何もしない

最後に必ず以下のフォーマットで出力すること:
DECISION: LONG or SHORT or HOLD
REASON: （日本語で理由を記述）
"""

_PROMPT_IN_POSITION = """\
{context}

---

現在ポジション保有中:
- サイド: {side}
- エントリー価格: ${entry_price:,.2f}
- 保有時間: {elapsed}
- 含み損益: {pnl_sign}${pnl_usd:.2f}（推定）

以下の{count}つのチャートを Read ツールで開いてください:
{chart_list}

チャートを見て、現在のポジションをどうするか判断してください。

判断後、以下のアクションを必ず取ること:
- EXIT と判断した場合 → Bash ツールで `python /app/script/close.py` を実行すること
- HOLD と判断した場合 → 何もしない（ポジション継続）

最後に必ず以下のフォーマットで出力すること:
DECISION: EXIT or HOLD
REASON: （日本語で理由を記述）
"""


def _fetch_mid(coin: str) -> float:
    """Fetch current mid price from Hyperliquid."""
    from hyperliquid.info import Info
    info = Info(settings.api_url, skip_ws=True)
    return float(info.all_mids()[coin])


def _load_context() -> str:
    try:
        with open(_CONTEXT_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning(f"Context file not found: {_CONTEXT_FILE}")
        return ""


def _build_chart_list(charts: list[tuple[str, str, str]]) -> str:
    lines = []
    for i, (interval, label, path) in enumerate(charts, 1):
        lines.append(f"{i}. {label}: {path}")
    return "\n".join(lines)


def _parse_response(output: str) -> dict:
    decision = "HOLD"
    reason = ""

    m = re.search(r"DECISION:\s*(LONG|SHORT|HOLD|EXIT)", output, re.IGNORECASE)
    if m:
        decision = m.group(1).upper()

    r = re.search(r"REASON:\s*(.+?)(?:\n\n|\Z)", output, re.DOTALL)
    if r:
        reason = r.group(1).strip()

    return {"decision": decision, "reason": reason}


def run_cycle(charts: list[tuple[str, str, str]], open_trade: Trade | None = None) -> dict:
    """
    Run one trading cycle with multi-timeframe charts.
    charts: list of (interval, label, file_path) from generate_multi_tf_charts()
    open_trade: expunged Trade object when in a position, or None.
    Returns dict with 'decision' and 'reason'.
    """
    coin = settings.trading_coin
    chart_list_str = _build_chart_list(charts)
    context = _load_context()

    if open_trade:
        now = datetime.utcnow()
        elapsed_str = f"{int((now - open_trade.entry_time).total_seconds() // 60)}分"
        try:
            current_price = _fetch_mid(open_trade.coin)
        except Exception as e:
            logger.warning(f"Failed to fetch mid price: {e}. Using entry price.")
            current_price = open_trade.entry_price
        pnl = calc_pnl(open_trade.side, open_trade.entry_price, current_price, open_trade.size_usd)
        prompt = _PROMPT_IN_POSITION.format(
            context=context,
            side=open_trade.side.upper(),
            entry_price=open_trade.entry_price,
            elapsed=elapsed_str,
            pnl_sign="+" if pnl >= 0 else "",
            pnl_usd=abs(pnl),
            count=len(charts),
            chart_list=chart_list_str,
        )
        logger.info(
            f"Calling Claude Code CLI (IN POSITION: {open_trade.side.upper()}) "
            f"with {len(charts)} timeframe charts"
        )
    else:
        prompt = _PROMPT_TEMPLATE.format(
            context=context,
            count=len(charts),
            chart_list=chart_list_str,
        )
        logger.info(f"Calling Claude Code CLI with {len(charts)} timeframe charts")

    claude_output = ""
    try:
        result = subprocess.run(
            [
                "claude",
                "-p", prompt,
                "--allowedTools", "Read,Bash",
                "--permission-mode", "bypassPermissions",
            ],
            capture_output=True,
            text=True,
            timeout=300,   # 6枚読むので余裕を持たせる
            cwd="/app",
        )
        claude_output = result.stdout
        if result.stderr:
            logger.warning(f"Claude stderr: {result.stderr[:300]}")
    except subprocess.TimeoutExpired:
        logger.error("Claude Code timed out after 300s")
        claude_output = "DECISION: HOLD\nREASON: タイムアウトのためスキップしました。"
    except FileNotFoundError:
        logger.error("'claude' command not found. Is Claude Code CLI installed?")
        claude_output = "DECISION: HOLD\nREASON: claude CLIが見つかりません。"
    except Exception as e:
        logger.error(f"Claude Code error: {e}")
        claude_output = "DECISION: HOLD\nREASON: 予期しないエラーが発生しました。"

    if "OAuth token has expired" in claude_output or "authentication_error" in claude_output:
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

    parsed = _parse_response(claude_output)
    logger.info(f"Decision: {parsed['decision']} | Reason: {parsed['reason'][:100]}")

    # 15m チャートパスをダッシュボード表示用に保存
    primary_chart = next((p for _, _, p in charts if "15m" in p), charts[0][2] if charts else "")

    with get_session() as session:
        cycle = Cycle(
            timestamp=datetime.utcnow(),
            coin=coin,
            chart_path=primary_chart,
            ai_decision=parsed["decision"],
            ai_reasoning=parsed["reason"],
            action_taken=parsed["decision"].lower(),
            claude_raw_output=claude_output[:5000],
        )
        session.add(cycle)
        session.commit()

    return parsed
