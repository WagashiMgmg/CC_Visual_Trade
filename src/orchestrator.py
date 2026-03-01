"""
Orchestrator: calls `claude -p` with the chart image path.
Claude Code reads the chart, decides LONG/SHORT/HOLD, and executes
the appropriate script via its Bash tool.
"""

import logging
import re
import subprocess
from datetime import datetime

from src.config import settings
from src.database import Cycle, get_session

logger = logging.getLogger(__name__)

CLAUDE_PROMPT = """\
あなたはプロのトレーダーです。
まず Read ツールで以下のチャート画像ファイルを開いて分析してください: {chart_path}

このチャートは {coin}/USD の15分足ローソクチャートです（直近100本 ≈ 約25時間分）。
チャートには以下の指標が表示されています:
- SMA20 (橙色): 20期間単純移動平均
- SMA50 (青色): 50期間単純移動平均
- RSI (紫色、下段): 14期間 RSI（赤破線=70 / 緑破線=30）
- 出来高バー (中段)

分析してトレード判断を行ってください:
1. トレンドの方向性と強さ (SMAの傾きとクロス)
2. RSIの水準 (70以上=買われ過ぎ/30以下=売られ過ぎ)
3. 直近の価格アクション (サポート/レジスタンス、パターン)
4. 出来高の変化

判断後、以下のアクションを必ず取ること:
- LONG と判断した場合 → Bash ツールで `python /app/script/long.py` を実行すること
- SHORT と判断した場合 → Bash ツールで `python /app/script/short.py` を実行すること
- HOLD と判断した場合 → 何もしない

最後に必ず以下のフォーマットで出力すること:
DECISION: LONG
REASON: （日本語で2〜3文で理由を記述）

または

DECISION: SHORT
REASON: （日本語で2〜3文で理由を記述）

または

DECISION: HOLD
REASON: （日本語で2〜3文で理由を記述）
"""


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


def run_cycle(chart_path: str) -> dict:
    """
    Run one trading cycle: call Claude Code CLI, parse response, record to DB.
    Returns dict with 'decision' and 'reason'.
    """
    coin = settings.trading_coin
    prompt = CLAUDE_PROMPT.format(chart_path=chart_path, coin=coin)

    logger.info(f"Calling Claude Code CLI with chart: {chart_path}")

    claude_output = ""
    action_taken = "error"

    try:
        result = subprocess.run(
            [
                "claude",
                "-p", prompt,
                "--allowedTools", "Read,Bash",
                "--dangerouslySkipPermissions",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            cwd="/app",
        )
        claude_output = result.stdout
        if result.stderr:
            logger.warning(f"Claude stderr: {result.stderr[:300]}")
    except subprocess.TimeoutExpired:
        logger.error("Claude Code timed out after 180s")
        claude_output = "DECISION: HOLD\nREASON: タイムアウトのためスキップしました。"
    except FileNotFoundError:
        logger.error("'claude' command not found. Is Claude Code CLI installed?")
        claude_output = "DECISION: HOLD\nREASON: claude CLIが見つかりません。"
    except Exception as e:
        logger.error(f"Claude Code error: {e}")
        claude_output = "DECISION: HOLD\nREASON: 予期しないエラーが発生しました。"

    parsed = _parse_response(claude_output)
    action_taken = parsed["decision"].lower()

    logger.info(f"Decision: {parsed['decision']} | Reason: {parsed['reason'][:80]}")

    with get_session() as session:
        cycle = Cycle(
            timestamp=datetime.utcnow(),
            coin=coin,
            chart_path=chart_path,
            ai_decision=parsed["decision"],
            ai_reasoning=parsed["reason"],
            action_taken=action_taken,
            claude_raw_output=claude_output[:5000],
        )
        session.add(cycle)
        session.commit()

    return parsed
