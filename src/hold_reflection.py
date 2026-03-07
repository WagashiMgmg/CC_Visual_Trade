"""
Missed-opportunity reflection for flat-HOLD decisions.

After a HOLD decision while flat, waits for a configurable window (default 4h),
then checks if the price moved enough that an entry would have been profitable.
If so, triggers a Claude subprocess reflection to learn from the missed opportunity.
"""

import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import requests

from src.config import settings
from src.database import Cycle, HoldOpportunity, MagiVote, get_session
from src.reflection import RULE_CONSISTENCY_CHECK

logger = logging.getLogger(__name__)

AGENTS_MD = "/app/AGENTS.md"
CHARTS_DIR = "/app/charts"
REFLECTIONS_DIR = "/app/data/reflections"


def archive_hold_charts(opportunity_id: int, coin: str) -> str | None:
    """Copy current coin charts to /app/charts/hold_{id}/ for later reflection."""
    archive_dir = f"{CHARTS_DIR}/hold_{opportunity_id}"
    os.makedirs(archive_dir, exist_ok=True)
    count = 0
    for fname in os.listdir(CHARTS_DIR):
        if fname.startswith(f"{coin}_") and fname.endswith(".png"):
            shutil.copy2(f"{CHARTS_DIR}/{fname}", f"{archive_dir}/{fname}")
            count += 1
    if count == 0:
        os.rmdir(archive_dir)
        return None
    logger.info(f"Archived {count} hold chart(s) to {archive_dir}")
    return archive_dir


def _fetch_candles_for_window(coin: str, start_time: datetime, window_hours: int):
    """Fetch 1-minute candles for the window after a HOLD decision."""
    api_url = settings.api_url + "/info"
    start_ms = int(start_time.replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = start_ms + window_hours * 3600 * 1000

    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1m", "startTime": start_ms, "endTime": end_ms},
    }
    resp = requests.post(api_url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _calculate_mfe(coin: str, hold_time: datetime, hold_price: float, window_hours: int = 4):
    """
    Calculate Max Favorable Excursion for both long and short directions.
    Returns (best_long_pnl, best_short_pnl, best_long_price, best_short_price).
    PnL is calculated for position_size_usd with fees deducted.
    """
    candles = _fetch_candles_for_window(coin, hold_time, window_hours)
    if not candles:
        return 0.0, 0.0, hold_price, hold_price

    highs = [float(c["h"]) for c in candles]
    lows = [float(c["l"]) for c in candles]

    best_long_price = max(highs) if highs else hold_price
    best_short_price = min(lows) if lows else hold_price

    from src.trader import get_user_fee_rate
    size = settings.position_size_usd
    fee_rate = get_user_fee_rate()
    round_trip_fee = size * fee_rate * 2

    best_long_pnl = (best_long_price - hold_price) / hold_price * size - round_trip_fee
    best_short_pnl = (hold_price - best_short_price) / hold_price * size - round_trip_fee

    return best_long_pnl, best_short_pnl, best_long_price, best_short_price


def _lookup_hold_cycle(cycle_id: int) -> dict | None:
    """Look up MAGI voting info for a HOLD cycle."""
    try:
        with get_session() as session:
            cycle = session.query(Cycle).filter(Cycle.id == cycle_id).first()
            if not cycle:
                return None

            all_votes = (
                session.query(MagiVote)
                .filter(MagiVote.cycle_id == cycle_id)
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
        logger.warning(f"Could not look up hold cycle: {e}")
    return None


def _build_hold_reflection_prompt(opportunity: dict, cycle_info: dict | None) -> str:
    """Build the Claude prompt for missed-opportunity reflection."""
    opp_id = opportunity["id"]
    coin = opportunity["coin"]
    hold_price = opportunity["hold_price"]
    hold_time = opportunity["hold_time"]
    max_price = opportunity["max_favorable_price"]
    direction = opportunity["max_favorable_direction"]
    hyp_pnl = opportunity["hypothetical_pnl"]
    archive_dir = opportunity["chart_archive_dir"]

    pnl_str = f"+${hyp_pnl:.2f}" if hyp_pnl >= 0 else f"-${abs(hyp_pnl):.2f}"
    direction_jp = "ロング" if direction == "long" else "ショート"

    if cycle_info:
        votes = cycle_info.get("votes", {})
        rounds = cycle_info.get("rounds", 1)
        votes_lines = []
        for agent_name, v in votes.items():
            votes_lines.append(
                f"- **{agent_name.capitalize()}** (Round {v['round']}): {v['decision']}\n"
                f"  {v['reasoning'][:400]}"
            )
        votes_str = "\n".join(votes_lines) if votes_lines else "(投票記録なし)"
        reasoning_section = f"""
## HOLD時のMAGI判断
- コンセンサス: {cycle_info['ai_decision']} ({rounds}ラウンド)
- 時刻: {cycle_info['timestamp']}
- コンセンサス根拠:
{cycle_info['ai_reasoning']}

### 各エージェントの投票
{votes_str}
"""
    else:
        reasoning_section = "\n## HOLD時のMAGI判断\n(記録なし)\n"

    return f"""# 見逃し機会の振り返りタスク

## HOLD判断情報
- Opportunity ID: {opp_id}
- コイン: {coin}
- HOLD時価格: ${hold_price:.2f}
- HOLD判断時刻: {hold_time}
- 最大有利価格: ${max_price:.2f} ({direction_jp}方向)
- 仮想PnL: {pnl_str}（フィー控除後、最大有利変動幅ベース）
- 判定窓: {settings.hold_reflection_window_hours}時間
{reasoning_section}
## 指示

ステップ1: HOLD判断時のアーカイブチャートを確認してください。
```bash
ls {archive_dir}
```
上記のファイルをすべてReadツールで開いて分析してください。

ステップ2: 現在のチャート（事後の値動き）を確認してください。
```bash
ls /app/charts/{coin}_*.png
```
上記のファイルもすべてReadツールで開いて分析してください。

ステップ3: 以下を分析してください。
必要であれば `/app/data/reflections/` 以下の過去の振り返りも参照してください。
- HOLD判断時にどのシグナルが{direction_jp}エントリーを示唆していたか
- なぜMAGIはHOLDを選んだか（見落とし or 過度な慎重さ or ルールの過剰適用）
- {direction_jp}エントリーしていれば仮想PnL {pnl_str} だった
- AGENTS.mdのどのルールが過度にエントリーを抑制したか（もしあれば）
- 今後のルール改善点（具体的に）
- 新たなエントリー推奨条件を提案する場合は、Google Scholar または arXiv で関連論文を必ず検索し、理論的裏付けとなる論文タイトルとURLを特定してください。裏付けが見つからない場合は禁止ではなく警戒・注意の表現に留めること。

ステップ4: Writeツールで `{REFLECTIONS_DIR}/hold_{opp_id}.md` に振り返り全文を書き込んでください。

フォーマット:
```markdown
## Hold {opp_id} — 見逃し{direction_jp} {coin} — 仮想PnL {pnl_str}
**HOLD時刻**: {hold_time}
**HOLD時価格**: ${hold_price:.2f}
**最大有利価格**: ${max_price:.2f} ({direction_jp})

### HOLD判断評価
（なぜHOLDを選んだか・それは正しかったか）

### 見逃しシグナル分析
（エントリーすべきだったサインの分析）

### ルール改善提案
（具体的なルール追加・変更・削除。なければ「なし」と記載）
```

ステップ5: `/app/AGENTS.md` をReadツールで読み込み、`<h2>学習済みルール</h2>` と `<h2>エントリー推奨条件</h2>` の2セクションをEditツールで更新してください。ファイルはHTMLで記述されています。Markdownではなく正しいHTMLタグを使用し、ファイル冒頭の **ルール管理** と **ルール見直し** に記載されたポリシーに従って以下を行ってください。

**ルール掃除（最重要）:**
- 今回の見逃し機会の原因となったルールを特定し、**削除または大幅に緩和**すること
- 適用回数が5回以上かつWIN率40%以下のルールは**削除を強く推奨**（改定ではなく削除を優先）
- 抽象的・曖昧すぎるルール（例:「慎重に判断」「注意が必要」等）は具体的条件に書き換えるか削除
- 他のルールと矛盾・重複するルールがあれば統合または削除
- ルール数が多すぎるとエントリー機会を過度に抑制する。**不要なルールの削除は追加と同等以上に価値がある**

**ルール簡潔化（毎回必ず実施）:**
- 全ルールを見直し、**本文150文字以内**（例外・出典タグは別枠）に圧縮すること
- 長い説明・補足が付いたルールは、本質だけを残して短縮
- 条件分岐が複雑なルールは、分割するか削除
- 同じ意味のルールが複数あれば1つに統合

**ルール追加・更新:**
- 見逃し機会の分析結果に基づき、エントリー推奨条件に新しい条件を追加するか、既存条件を更新すること
- 学習済みルールに過度な抑制があった場合は例外条件を追加するか、ルールを緩和すること
- `<small class="rule-stat">適用N / WINN</small>` の更新は不要（HOLDは実トレードではないため）
{RULE_CONSISTENCY_CHECK}
ステップ6: 以下のコマンドでアーカイブディレクトリを削除してください:
```bash
rm -rf {archive_dir}
```
"""



def trigger_hold_reflection(opportunity_id: int) -> None:
    """Launch a Claude subprocess to perform missed-opportunity reflection."""
    with get_session() as session:
        opp = session.query(HoldOpportunity).filter(HoldOpportunity.id == opportunity_id).first()
        if not opp:
            logger.warning(f"HoldOpportunity {opportunity_id} not found")
            return

        opp_dict = {
            "id": opp.id,
            "cycle_id": opp.cycle_id,
            "coin": opp.coin,
            "hold_price": opp.hold_price,
            "hold_time": opp.hold_time.isoformat() if opp.hold_time else "?",
            "max_favorable_price": opp.max_favorable_price,
            "max_favorable_direction": opp.max_favorable_direction,
            "hypothetical_pnl": opp.hypothetical_pnl,
            "chart_archive_dir": opp.chart_archive_dir,
        }

    archive_dir = opp_dict.get("chart_archive_dir")
    if not archive_dir or not os.path.isdir(archive_dir):
        logger.info(f"No archive dir for hold opportunity {opportunity_id}, skipping")
        return

    cycle_info = _lookup_hold_cycle(opp_dict["cycle_id"])
    prompt = _build_hold_reflection_prompt(opp_dict, cycle_info)

    try:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
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
        logger.info(f"Launched hold reflection subprocess for opportunity_id={opportunity_id}")

        with get_session() as session:
            opp = session.query(HoldOpportunity).filter(HoldOpportunity.id == opportunity_id).first()
            if opp:
                opp.status = "reflected"
                opp.reflection_path = f"{REFLECTIONS_DIR}/hold_{opportunity_id}.md"
                session.commit()
    except Exception as e:
        logger.error(f"Failed to launch hold reflection subprocess: {e}")


def check_pending_opportunities() -> None:
    """
    Check pending HOLD opportunities that have passed the review window.
    Called by the scheduler every 30 minutes.
    """
    if not settings.hold_reflection_enabled:
        return

    window = timedelta(hours=settings.hold_reflection_window_hours)
    cutoff = datetime.utcnow() - window

    with get_session() as session:
        pending = (
            session.query(HoldOpportunity)
            .filter(
                HoldOpportunity.status == "pending",
                HoldOpportunity.hold_time <= cutoff,
            )
            .all()
        )

        if not pending:
            return

        logger.info(f"Checking {len(pending)} pending hold opportunities")

        from src.trader import get_user_fee_rate
        fee_rate = get_user_fee_rate()
        round_trip_fee = settings.position_size_usd * fee_rate * 2
        threshold = round_trip_fee * settings.hold_reflection_min_pnl_multiplier

        for opp in pending:
            try:
                long_pnl, short_pnl, long_price, short_price = _calculate_mfe(
                    opp.coin, opp.hold_time, opp.hold_price,
                    settings.hold_reflection_window_hours,
                )

                # Pick the better direction
                if long_pnl >= short_pnl:
                    best_pnl = long_pnl
                    best_price = long_price
                    best_dir = "long"
                else:
                    best_pnl = short_pnl
                    best_price = short_price
                    best_dir = "short"

                opp.check_time = datetime.utcnow()
                opp.max_favorable_price = best_price
                opp.max_favorable_direction = best_dir
                opp.hypothetical_pnl = best_pnl

                if best_pnl >= threshold:
                    opp.status = "checked"
                    session.commit()
                    logger.info(
                        f"Hold opportunity {opp.id}: missed ${best_pnl:.2f} ({best_dir}) "
                        f"— triggering reflection"
                    )
                    trigger_hold_reflection(opp.id)
                    continue  # status already updated in trigger_hold_reflection
                else:
                    opp.status = "skipped"
                    session.commit()
                    # Clean up chart archive after commit
                    if opp.chart_archive_dir:
                        shutil.rmtree(opp.chart_archive_dir, ignore_errors=True)
                        logger.info(f"Cleaned up charts: {opp.chart_archive_dir}")
                    logger.info(
                        f"Hold opportunity {opp.id}: best PnL ${best_pnl:.2f} "
                        f"below threshold ${threshold:.2f} — skipped"
                    )
            except Exception as e:
                logger.error(f"Error checking hold opportunity {opp.id}: {e}")
                session.commit()
