"""
Post-trade reflection module.

After a trade closes, archives the entry charts and launches a Claude subprocess
to analyze the outcome, write the full reflection to /app/data/reflections/,
and update the ## 学習済みルール section of /app/AGENTS.md.
"""

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

AGENTS_MD = "/app/AGENTS.md"
CHARTS_DIR = "/app/charts"
REFLECTIONS_DIR = "/app/data/reflections"


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


def _lookup_entry_cycle(trade_id: int) -> dict | None:
    """
    Look up the Cycle that triggered a trade by trade.cycle_id.
    Returns a dict with ai_decision, ai_reasoning, timestamp, votes; or None.
    """
    try:
        from src.database import Cycle, MagiVote, Trade, get_session

        with get_session() as session:
            trade = session.query(Trade).filter(Trade.id == trade_id).first()
            if trade and trade.cycle_id:
                cycle = session.query(Cycle).filter(Cycle.id == trade.cycle_id).first()
                if cycle:
                    # Fetch latest round vote per agent
                    all_votes = (
                        session.query(MagiVote)
                        .filter(MagiVote.cycle_id == trade.cycle_id)
                        .order_by(MagiVote.agent_name, MagiVote.round.desc())
                        .all()
                    )
                    by_agent: dict[str, MagiVote] = {}
                    for v in all_votes:
                        if v.agent_name not in by_agent:
                            by_agent[v.agent_name] = v
                    rounds_used = max((v.round for v in all_votes), default=0) + 1

                    votes = {
                        name: {
                            "decision": v.decision,
                            "reasoning": v.reasoning or "",
                            "round": v.round,
                        }
                        for name, v in by_agent.items()
                    }

                    return {
                        "ai_decision": cycle.ai_decision,
                        "ai_reasoning": cycle.ai_reasoning or "",
                        "timestamp": cycle.timestamp.isoformat() if cycle.timestamp else "",
                        "rounds": rounds_used,
                        "votes": votes,
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
        votes = cycle_info.get("votes", {})
        rounds = cycle_info.get("rounds", 1)
        votes_lines = []
        for agent_name, v in votes.items():
            votes_lines.append(
                f"- **{agent_name.capitalize()}** (Round {v['round']}): {v['decision']}\n"
                f"  {v['reasoning'][:300]}"
            )
        votes_str = "\n".join(votes_lines) if votes_lines else "（投票記録なし）"
        reasoning_section = f"""
## エントリー時のMAGI判断
- コンセンサス: {cycle_info['ai_decision']} （{rounds}ラウンド）
- 時刻: {cycle_info['timestamp']}
- コンセンサス根拠:
{cycle_info['ai_reasoning']}

### 各エージェントの投票
{votes_str}
"""
    else:
        reasoning_section = "\n## エントリー時のMAGI判断\n（記録なし）\n"

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

ステップ2: チャートとエントリー時の判断根拠を照らし合わせて以下を分析してください。
必要であれば `/app/data/reflections/` 以下の過去の振り返りも参照してください。
- エントリー判断の根拠となったシグナルは実際に正しかったか
- 結果（{result_label}）の主因は何か
- 見落としていたサイン・逆シグナルはあったか
- 今後のトレードルール改善点（具体的に）
- 一つの結論に辿り着いたら、googlescholarで関連するトレード分析論文を検索して、理論的な裏付けがあるかも確認してください。

ステップ3: Writeツールで `{REFLECTIONS_DIR}/trade_{trade_id}.md` に振り返り全文を書き込んでください。

フォーマット:
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
- （具体的なルール追加・変更・削除。なければ「なし」と記載）
```

ステップ4: `/app/AGENTS.md` をReadツールで読み込み、`## 学習済みルール` セクションのみをEditツールで更新してください。
- トレード履歴セクション（## Trade N ...）は書かない
- 新しいルールがある場合のみ追記。既存ルールと重複する場合は更新

ステップ5: 今回の振り返りで「このインジケーターがあれば」「この機能を追加したい」などコーディング改善リクエストがあれば、Bashツールで以下のコマンドを実行してGitHub Issueを作成してください。なければスキップ。
```bash
gh issue create \
  --title "[Trade {trade_id}] （機能タイトル）" \
  --body "## 背景
Trade {trade_id} の振り返りで気づいた改善点

## 説明
（詳細な説明）

## 優先度
high / medium / low" \
  --label "enhancement"
```

ステップ6: 以下のコマンドでアーカイブディレクトリを削除してください:
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

    cycle_info = _lookup_entry_cycle(trade_info.get("trade_id"))
    prompt = _build_reflection_prompt(trade_info, cycle_info)

    try:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)  # Allow nested claude launch from within a claude session
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
            env=env,
        )
        logger.info(
            f"Launched reflection subprocess for trade_id={trade_info.get('trade_id')}"
        )
    except Exception as e:
        logger.error(f"Failed to launch reflection subprocess: {e}")
