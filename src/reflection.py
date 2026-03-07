"""
Post-trade reflection module.

After a trade closes, archives the entry charts and launches a Claude subprocess
to analyze the outcome, write the full reflection to /app/data/reflections/,
and update the ## 学習済みルール section of /app/prompt/rule.html.
"""

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

AGENTS_MD = "/app/prompt/rule.html"
CHARTS_DIR = "/app/charts"
REFLECTIONS_DIR = "/app/data/reflections"
HYPOTHESES_FILE = "/app/data/reflections/hypotheses.md"

# Shared consistency-check instruction injected after AGENTS.md rule updates
RULE_CONSISTENCY_CHECK = """
**ルール整合性チェック（必須）:**
ステップ4で更新した後、以下の手順で全ルールの論理整合性を検証してください。

1. `/app/prompt/rule.html` を再度Readし、`<h2>学習済みルール</h2>` と `<h2>エントリー推奨条件</h2>` の全ルールをPython風擬似コードに変換してください。以下のフォーマットで記述:

```python
def check_rules(signal, market) -> (bool, str):
    # 学習済みルール1: (ルール名)
    if signal == "LONG":
        if (条件):
            return False, "Rule1: ..."
    # ... 全ルール

def check_entry_signals(market) -> str | None:
    # Entry1: (条件名)
    if (条件の組み合わせ):
        return "SHORT"
    # ... 全エントリー推奨条件
```

2. 擬似コードを見て以下をチェック:
   - **自己矛盾**: 同一スナップショットで同時に満たせない条件の組み合わせ（例: `rsi >= 70 and rsi < 65`）。時系列イベント（「一度X超→その後Y未満に下落」）をスナップショット条件と混同していないか
   - **相互矛盾**: 学習済みルールがエントリー推奨条件を常にブロックしてしまう組み合わせ（例: Rule1がEntry3を常に阻止）
   - **冗長**: 学習済みルールとエントリー推奨条件で同じチェックを二重に行っている
   - **到達不能**: 前段の条件により絶対に到達しないエントリー推奨条件

3. 矛盾・問題が見つかった場合、`/app/prompt/rule.html` をEditツールで即座に修正してください:
   - スナップショット矛盾 → 時系列条件に書き換え（例: `RSI≥70後に＜65転換` のように状態遷移を明記）
   - 常時ブロック → ルール側に例外追加、またはエントリー条件の前提を修正
   - 冗長 → エントリー推奨条件側の重複チェックを削除（学習済みルールに任せる）
   - 到達不能 → 条件を修正するか、エントリー推奨条件を削除

4. 修正した場合、振り返りファイルの末尾に `### 整合性チェック修正` セクションを追記し、何を修正したか記録してください。
"""


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
                f"  {v['reasoning'][:400]}"
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

ステップ0: `{HYPOTHESES_FILE}` をReadツールで読み込んでください（ファイルが存在しない場合はスキップ）。未解決の仮説がある場合、今回のトレード結果がそれらを支持・否定するか、ステップ2で検証してください。

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
- 新たな禁止ルールを提案する場合は、Google Scholar または arXiv で関連論文を必ず検索し、理論的裏付けとなる論文タイトルとURLを特定してください。裏付けが見つからない場合は禁止ではなく警戒・注意の表現に留めること。

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

### 仮説検証
（ステップ0で読んだ未解決の仮説に対する検証結果。該当なしの場合は「該当なし」）

### 新たな仮説
（今回の振り返りで生まれた矛盾する観察・確信度の低い教訓。なければ「なし」）

### ルール更新
- （具体的なルール追加・変更・削除。なければ「なし」と記載）
```

ステップ4: `/app/prompt/rule.html` をReadツールで読み込み、`<h2>学習済みルール</h2>` と `<h2>エントリー推奨条件</h2>` の2セクションをEditツールで更新してください。ファイルはHTMLで記述されています。Markdownではなく正しいHTMLタグを使用し、ファイル冒頭の **ルール管理** と **ルール見直し** に記載されたポリシーに従って以下を行ってください。

**ルール掃除（最重要）:**
- 適用回数が5回以上かつWIN率40%以下のルールは**削除を強く推奨**（改定ではなく削除を優先）
- 抽象的・曖昧すぎるルール（例:「慎重に判断」「注意が必要」等）は具体的条件に書き換えるか削除
- 他のルールと矛盾・重複するルールがあれば統合または削除
- ルール数が多すぎるとエントリー機会を過度に抑制する。**不要なルールの削除は追加と同等以上に価値がある**
- 今回のトレードがLOSSの場合、敗因となったルールの欠如を特定し追加。ただし過度に具体的な（1回限りの状況に依存する）ルールは避けること

**ルール簡潔化（毎回必ず実施）:**
- 全ルールを見直し、**本文150文字以内**（例外・出典タグは別枠）に圧縮すること
- 長い説明・補足が付いたルールは、本質だけを残して短縮
- 条件分岐が複雑なルールは、分割するか削除
- 同じ意味のルールが複数あれば1つに統合

**ルール追加・更新:**
- 各セクション1件を追加・更新・削除すること（削除も有効なアクション）
- LOSSの場合もエントリー推奨条件には「このシグナルが揃っていればWINだった可能性がある」仮説的ポジティブ条件を1件記録すること
- `<h2>エントリー推奨条件</h2>` セクションが存在しない場合は `<ol></ol>` ごと新規作成すること
- 今回のトレード判断で参照・適用したルールの `<small class="rule-stat">適用N / WINN</small>` 数値を更新すること（適用: 常に+1、WIN時はさらにWIN: +1）
{RULE_CONSISTENCY_CHECK}
ステップ5: `{HYPOTHESES_FILE}` を更新してください（Writeツール使用）。以下のルールに従ってください:

**仮説の検証結果を反映:**
- 今回のトレードで**支持**された仮説 → `支持` カウントを+1
- 今回のトレードで**否定**された仮説 → 削除（理由はステップ3の振り返りファイルに記録済み）
- **支持2回以上**に達した仮説 → ステップ4でAGENTS.mdのルールに昇格済みのはずなので、仮説から削除

**新たな仮説の追加:**
- 今回の振り返りで矛盾する観察、確信度の低い教訓、ルール化するには早い気づきがあれば追加
- 各仮説には以下を記載: タイトル、初出Trade ID、支持/否定カウント、内容、検証条件（どういう結果が出たら支持/否定か）
- 最大10件を維持。超過時は支持0かつ古いものから削除

**フォーマット:**
```markdown
# 未解決の仮説

## H1: (仮説タイトル)
- **初出**: Trade XX
- **支持**: 0
- **否定**: 0
- **内容**: (矛盾する観察や確信度の低い教訓)
- **検証条件**: (どういうトレード結果が出たら支持/否定と判断するか)
```

ファイルが存在しない場合は新規作成してください。仮説が0件の場合も「# 未解決の仮説」ヘッダだけは残してください。

ステップ6: 今回の振り返りで「このインジケーターがあれば」「この機能を追加したい」などコーディング改善リクエストがあれば、Bashツールで以下のコマンドを実行してGitHub Issueを作成してください。なければスキップ。
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

ステップ7: 以下のコマンドでアーカイブディレクトリを削除してください:
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
