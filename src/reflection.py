"""
Post-trade reflection module.

After a trade closes, archives the entry charts and launches a Claude subprocess
to analyze the outcome and append learnings to /app/data/AGENTS.md.
"""

import logging
import os
import shutil
import subprocess
from datetime import datetime

logger = logging.getLogger(__name__)

AGENTS_MD = "/app/data/AGENTS.md"
CHARTS_DIR = "/app/charts"


def archive_charts(trade_id: int, coin: str) -> str | None:
    """
    Copy current coin charts to /app/charts/trade_{trade_id}/ for later reflection.
    Returns the archive directory path, or None if no charts were found.
    """
    archive_dir = f"{CHARTS_DIR}/trade_{trade_id}"
    os.makedirs(archive_dir, exist_ok=True)
    count = 0
    for fname in os.listdir(CHARTS_DIR):
        if fname.startswith(f"{coin}_") and fname.endswith(".png"):
            shutil.copy2(f"{CHARTS_DIR}/{fname}", f"{archive_dir}/{fname}")
            count += 1
    if count == 0:
        os.rmdir(archive_dir)
        return None
    logger.info(f"Archived {count} chart(s) to {archive_dir}")
    return archive_dir


def _lookup_entry_cycle(entry_time: datetime) -> dict | None:
    """
    Find the most recent LONG/SHORT Cycle record at or before entry_time.
    Returns a dict with ai_decision, ai_reasoning, timestamp; or None.
    """
    try:
        from src.database import Cycle, get_session

        with get_session() as session:
            cycle = (
                session.query(Cycle)
                .filter(
                    Cycle.ai_decision.in_(["LONG", "SHORT"]),
                    Cycle.timestamp <= entry_time,
                )
                .order_by(Cycle.timestamp.desc())
                .first()
            )
            if cycle:
                return {
                    "ai_decision": cycle.ai_decision,
                    "ai_reasoning": cycle.ai_reasoning or "",
                    "timestamp": cycle.timestamp.isoformat() if cycle.timestamp else "",
                }
    except Exception as e:
        logger.warning(f"Could not look up entry cycle: {e}")
    return None


def _build_reflection_prompt(trade_info: dict, cycle_info: dict | None) -> str:
    """Build the Claude prompt for post-trade reflection."""
    archive_dir = trade_info["archive_dir"]
    trade_id = trade_info.get("trade_id", "?")
    coin = trade_info.get("coin", "?")
    side = trade_info.get("side", "?")
    entry_price = trade_info.get("entry_price", 0)
    exit_price = trade_info.get("exit_price", 0)
    pnl = trade_info.get("pnl_usd", 0)
    entry_time = trade_info.get("entry_time", "?")
    exit_time = trade_info.get("exit_time", "?")

    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
    result_label = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAK-EVEN")

    if cycle_info:
        reasoning_section = f"""
## エントリー時のAI判断
- 決定: {cycle_info['ai_decision']}
- 時刻: {cycle_info['timestamp']}
- 根拠:
{cycle_info['ai_reasoning']}
"""
    else:
        reasoning_section = "\n## エントリー時のAI判断\n（記録なし）\n"

    return f"""# トレード振り返りタスク

## トレード情報
- Trade ID: {trade_id}
- コイン: {coin}
- サイド: {side.upper()}
- エントリー価格: ${entry_price:.2f}
- エグジット価格: ${exit_price:.2f}
- PnL: {pnl_str} ({result_label})
- エントリー時刻: {entry_time}
- エグジット時刻: {exit_time}
{reasoning_section}
## 指示

ステップ1: 以下のコマンドでアーカイブチャートを確認し、全チャートをReadツールで開いて分析してください。
```bash
ls {archive_dir}
```

ステップ2: チャートとエントリー時の判断根拠を照らし合わせて以下を分析してください:
- エントリー判断の根拠となったシグナルは実際に正しかったか
- 結果（{result_label}）の主因は何か
- 見落としていたサイン・逆シグナルはあったか
- 今後のトレードルール改善点（具体的に）

ステップ3: `/app/data/AGENTS.md` をReadツールで読み込み、以下のフォーマットで新しい振り返りエントリを追記してWriteツールで保存してください。ファイルが存在しない場合は新規作成してください。

追記フォーマット:
```markdown
## Trade {trade_id} — {side.upper()} {coin} — {result_label} ({pnl_str})
**日時**: {entry_time} → {exit_time}
**価格**: ${entry_price:.2f} → ${exit_price:.2f}

### 判断評価
（エントリー根拠が正しかったか・間違っていたかの評価）

### 主因分析
（勝因・敗因の分析）

### 見落とし
（見落としたサイン・改善すべき点）

### ルール更新
- （具体的なルール追加・変更・削除）
```

ステップ4: AGENTS.md全体を再度Readし、以下を行ってWriteで保存してください:
- 重複したルールを統合
- 矛盾するルールは最新のトレードを優先
- 最小限の変更に留める

ステップ5: 以下のコマンドでアーカイブディレクトリを削除してください:
```bash
rm -rf {archive_dir}
```
"""


def trigger_reflection(trade_info: dict) -> None:
    """
    Launch a Claude subprocess asynchronously to perform post-trade reflection.

    trade_info must contain:
        archive_dir, trade_id, coin, side, entry_price,
        exit_price, pnl_usd, entry_time, exit_time
    """
    archive_dir = trade_info.get("archive_dir")
    if not archive_dir or not os.path.isdir(archive_dir):
        logger.info(
            f"No archive dir for trade_id={trade_info.get('trade_id')}, skipping reflection"
        )
        return

    cycle_info = _lookup_entry_cycle(trade_info.get("entry_time"))
    prompt = _build_reflection_prompt(trade_info, cycle_info)

    try:
        subprocess.Popen(
            [
                "claude", "-p", prompt,
                "--allowedTools", "Read,Write,Edit,Bash",
                "--permission-mode", "bypassPermissions",
            ],
            cwd="/app",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        logger.info(
            f"Launched reflection subprocess for trade_id={trade_info.get('trade_id')}"
        )
    except Exception as e:
        logger.error(f"Failed to launch reflection subprocess: {e}")
