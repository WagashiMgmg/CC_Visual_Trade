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
from src.database import Cycle, get_session

logger = logging.getLogger(__name__)

CLAUDE_PROMPT = """\
以下の{count}つのチャートを Read ツールで開いてください:

{chart_list}

各チャートには SMA20 (橙)・SMA50 (青)・RSI (紫)・出来高 が表示されています。

チャートを見てトレード判断をしてください。

判断後、以下のアクションを必ず取ること:
- LONG  と判断した場合 → Bash ツールで `python /app/script/long.py` を実行すること
- SHORT と判断した場合 → Bash ツールで `python /app/script/short.py` を実行すること
- HOLD  と判断した場合 → 何もしない

最後に必ず以下のフォーマットで出力すること:
DECISION: LONG or SHORT or HOLD
REASON: （日本語で理由を記述）
"""


def _build_chart_list(charts: list[tuple[str, str, str]]) -> str:
    lines = []
    for i, (interval, label, path) in enumerate(charts, 1):
        lines.append(f"{i}. {label}: {path}")
    return "\n".join(lines)


def _parse_response(output: str) -> dict:
    decision = "HOLD"
    reason = ""

    m = re.search(r"DECISION:\s*(LONG|SHORT|HOLD)", output, re.IGNORECASE)
    if m:
        decision = m.group(1).upper()

    r = re.search(r"REASON:\s*(.+?)(?:\n\n|\Z)", output, re.DOTALL)
    if r:
        reason = r.group(1).strip()

    return {"decision": decision, "reason": reason}


def run_cycle(charts: list[tuple[str, str, str]]) -> dict:
    """
    Run one trading cycle with multi-timeframe charts.
    charts: list of (interval, label, file_path) from generate_multi_tf_charts()
    Returns dict with 'decision' and 'reason'.
    """
    coin = settings.trading_coin
    chart_list_str = _build_chart_list(charts)
    prompt = CLAUDE_PROMPT.format(
        count=len(charts),
        chart_list=chart_list_str,
        coin=coin,
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
