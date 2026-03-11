"""
Early-exit reflection for agent-initiated EXIT decisions.

When the AI agent exits a position before the 4h forced-close window, we
archive the exit-time charts and record an EarlyExitOpportunity. After the
window expires, a scheduler checks what would have happened if the agent held
until the 4h mark. If holding would have been significantly more profitable,
we trigger a Claude subprocess reflection to improve EXIT rules.
"""

import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

import requests

from src.config import settings
from src.database import EarlyExitOpportunity, get_session
from src.reflection import RULE_CONSISTENCY_CHECK, fee_note

logger = logging.getLogger(__name__)

CHARTS_DIR = "/app/charts"
REFLECTIONS_DIR = "/app/data/reflections"
HYPOTHESES_FILE = "/app/data/reflections/hypotheses.md"


def _archive_exit_charts(opp_id: int, coin: str) -> str | None:
    """Copy current coin charts to /app/charts/early_exit_{id}/ for later reflection."""
    archive_dir = f"{CHARTS_DIR}/early_exit_{opp_id}"
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


def record_early_exit(trade_info: dict) -> None:
    """Record an agent-initiated EXIT for deferred hold-longer analysis.

    Only records if there is meaningful time remaining before the forced-close
    window (>= 15 minutes). Called from close.py after each agent EXIT.
    """
    entry_time = trade_info.get("entry_time")
    exit_time = trade_info.get("exit_time") or datetime.utcnow()

    if isinstance(entry_time, str):
        entry_time = datetime.fromisoformat(entry_time)
    if isinstance(exit_time, str):
        exit_time = datetime.fromisoformat(exit_time)

    if entry_time is None:
        logger.warning("record_early_exit: entry_time missing, skipping")
        return

    window_end_time = entry_time + timedelta(hours=settings.position_max_hours)
    time_remaining = (window_end_time - exit_time).total_seconds()

    if time_remaining < 15 * 60:
        logger.info(
            f"Early exit: only {time_remaining:.0f}s remaining before "
            f"{settings.position_max_hours}h forced-close mark — skipping record"
        )
        return

    coin = trade_info.get("coin", "")

    with get_session() as session:
        opp = EarlyExitOpportunity(
            trade_id=trade_info.get("trade_id"),
            coin=coin,
            side=trade_info.get("side"),
            entry_price=trade_info.get("entry_price"),
            exit_price=trade_info.get("exit_price"),
            actual_pnl=trade_info.get("pnl_usd", 0),
            exit_time=exit_time,
            window_end_time=window_end_time,
        )
        session.add(opp)
        session.flush()
        opp_id = opp.id

        archive_dir = _archive_exit_charts(opp_id, coin)
        opp.chart_archive_dir = archive_dir
        session.commit()

    logger.info(
        f"Recorded early exit opportunity id={opp_id} for "
        f"trade_id={trade_info.get('trade_id')}, "
        f"window_remaining={time_remaining / 60:.0f}m"
    )


def _fetch_candles(coin: str, start_time: datetime, end_time: datetime) -> list:
    """Fetch 1-minute candles between start_time and end_time."""
    api_url = settings.api_url + "/info"
    start_ms = int(start_time.replace(tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(end_time.replace(tzinfo=timezone.utc).timestamp() * 1000)

    payload = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1m", "startTime": start_ms, "endTime": end_ms},
    }
    resp = requests.post(api_url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _calculate_post_exit_outcomes(
    coin: str,
    side: str,
    entry_price: float,
    exit_time: datetime,
    window_end_time: datetime,
) -> tuple[float, float, float, float]:
    """Calculate what would have happened if the trade was held after exit.

    Returns (price_at_window_end, max_favorable_price,
             hypothetical_pnl_pct, max_hypothetical_pnl_pct).
    PnL values are in price change % from entry_price.
    """
    candles = _fetch_candles(coin, exit_time, window_end_time)
    if not candles:
        return exit_time, exit_time, 0.0, 0.0

    price_at_window_end = float(candles[-1]["c"])

    if side == "long":
        max_favorable_price = max(float(c["h"]) for c in candles)
        hyp_pnl_pct = (price_at_window_end - entry_price) / entry_price * 100
        max_hyp_pnl_pct = (max_favorable_price - entry_price) / entry_price * 100
    else:
        max_favorable_price = min(float(c["l"]) for c in candles)
        hyp_pnl_pct = (entry_price - price_at_window_end) / entry_price * 100
        max_hyp_pnl_pct = (entry_price - max_favorable_price) / entry_price * 100

    return price_at_window_end, max_favorable_price, hyp_pnl_pct, max_hyp_pnl_pct


def _build_early_exit_prompt(opportunity: dict, fee_rate_pct: float | None = None) -> str:
    """Build the Claude prompt for early-exit reflection."""
    opp_id = opportunity["id"]
    coin = opportunity["coin"]
    side = opportunity["side"]
    entry_price = opportunity["entry_price"]
    exit_price = opportunity["exit_price"]
    actual_pnl = opportunity["actual_pnl"]  # now in %
    exit_time = opportunity["exit_time"]
    window_end_time = opportunity["window_end_time"]
    price_at_end = opportunity["price_at_window_end"]
    max_price = opportunity["max_favorable_price"]
    hyp_pnl = opportunity["hypothetical_pnl"]  # now in %
    max_hyp_pnl = opportunity["max_hypothetical_pnl"]  # now in %
    archive_dir = opportunity["chart_archive_dir"]

    def fmt_pnl(v):
        return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"

    actual_str = fmt_pnl(actual_pnl)
    hyp_str = fmt_pnl(hyp_pnl)
    max_hyp_str = fmt_pnl(max_hyp_pnl)
    improvement = hyp_pnl - actual_pnl
    improvement_str = fmt_pnl(improvement)
    side_jp = "ロング" if side == "long" else "ショート"
    fee_block = fee_note(fee_rate_pct) if fee_rate_pct is not None else ""

    return f"""# 早期EXIT振り返りタスク

{fee_block}
## EXIT判断情報
- Opportunity ID: {opp_id}
- コイン: {coin}
- サイド: {side.upper()} ({side_jp})
- エントリー価格: ${entry_price:.2f}
- EXIT価格（実際）: ${exit_price:.2f}
- 実際のPnL（エントリーから）: {actual_str}
- EXIT時刻: {exit_time}
- {settings.position_max_hours}h強制決済上限時刻: {window_end_time}

## もし{settings.position_max_hours}hまで保有し続けていたら
- {settings.position_max_hours}h時点の価格: ${price_at_end:.2f}
- {settings.position_max_hours}h時点での仮想PnL（エントリーから）: {hyp_str}
- 実際のPnLとの差: {improvement_str}（プラスならholdの方が良かった）
- 残存時間中の最大有利価格（MFE）: ${max_price:.2f}
- MFEでの最大仮想PnL: {max_hyp_str}

## 指示

ステップ0: `{HYPOTHESES_FILE}` をReadツールで読み込んでください（存在しない場合はスキップ）。未解決の仮説がある場合、今回の結果がそれらを支持・否定するかステップ2で検証してください。

ステップ1: EXIT時点のアーカイブチャートを確認してください（AIがEXITを決断した瞬間の市場状態）。
```bash
ls {archive_dir}
```
上記のファイルをすべてReadツールで開いて分析してください。

ステップ2: 以下を分析してください。必要であれば `/app/data/reflections/` 以下の過去の振り返りも参照してください。
- EXITを決断した根拠は正しかったか（実際: {actual_str} vs 継続仮想: {hyp_str}）
- 保有し続けた場合に{improvement_str}の差が生じた理由は何か
- 「まだ保有すべき」シグナルを見落としていたか、あるいは正しくEXITできたか
- EXIT判断のルール改善点（具体的に）

ステップ3: Writeツールで `{REFLECTIONS_DIR}/early_exit_{opp_id}.md` に振り返り全文を書き込んでください。

フォーマット:
```markdown
## EarlyExit {opp_id} — {side.upper()} {coin} — 実際PnL: {actual_str} / 継続仮想PnL: {hyp_str}
**EXIT時刻**: {exit_time}
**EXIT価格**: ${exit_price:.2f}
**{settings.position_max_hours}h時点価格**: ${price_at_end:.2f}（仮想PnL: {hyp_str}）
**最大有利価格（MFE）**: ${max_price:.2f}（最大仮想PnL: {max_hyp_str}）

### EXIT判断評価
（EXITの決断は正しかったか）

### 主因分析
（なぜ保有継続の方が良い/悪かったか）

### 見落とし
（EXITすべきでなかったサイン、またはEXITで正解だったサイン）

### 仮説検証
（ステップ0で読んだ未解決仮説に対する検証。該当なしの場合は「該当なし」）

### 新たな仮説
（今回の振り返りで生まれた確信度の低い教訓。なければ「なし」）

### ルール更新
- （EXITルール追加・変更・削除。なければ「なし」）
```

ステップ4: まず以下のコマンドで `rule.html` の過去の変更履歴（diff付き）を確認し、ルール更新の傾向・文脈を把握してください:
```bash
git log -p -20 -- prompt/rule.html
```
履歴を参考にした上で、`/app/prompt/rule.html` をReadツールで読み込み、`<h2>学習済みルール</h2>` と `<h2>エントリー推奨条件</h2>` の2セクションをEditツールで更新してください。ファイルはHTMLで記述されています。Markdownではなく正しいHTMLタグを使用し、ファイル冒頭の **ルール管理** と **ルール見直し** に記載されたポリシーに従って以下を行ってください。

**EXITルール更新（今回の主題）:**
- 保有継続が有利だった場合（improvement > 0）: 「このシグナルが見られる時はまだ保有を継続すべき」条件を学習済みルールに追加・強化
- 早期EXITが正解だった場合（improvement ≤ 0）: EXITルールを支持する根拠を記録し、`<small class="rule-stat">` の統計を更新

**ルール掃除（毎回必ず実施）:**
- 適用回数が5回以上かつWIN率40%以下のルールは**削除を強く推奨**
- 抽象的・曖昧すぎるルールは具体的条件に書き換えるか削除
- 他のルールと矛盾・重複するルールがあれば統合または削除
- ルール数が多すぎるとエントリー機会を過度に抑制する。**不要なルールの削除は追加と同等以上に価値がある**

**ルール簡潔化（毎回必ず実施）:**
- 全ルールを見直し、**本文150文字以内**（例外・出典タグは別枠）に圧縮すること
- 長い説明・補足が付いたルールは本質だけを残して短縮
- 条件分岐が複雑なルールは分割するか削除
- 同じ意味のルールが複数あれば1つに統合
{RULE_CONSISTENCY_CHECK}
**rule.html変更後のコミット＆プッシュ（必須）:**
rule.htmlを変更した場合、以下のコマンドで必ずコミット＆プッシュしてください:
```bash
git pull --rebase --autostash && git add prompt/rule.html && git commit -m "reflect: EarlyExit {opp_id} — rule.html update" && git push
```

ステップ5: `{HYPOTHESES_FILE}` を更新してください（Writeツール使用）。以下のルールに従ってください:

**仮説の検証結果を反映:**
- 今回のトレードで**支持**された仮説 → `支持` カウントを+1
- 今回のトレードで**否定**された仮説 → 削除（理由はステップ3の振り返りファイルに記録済み）
- **支持2回以上**に達した仮説 → ステップ4でルールに昇格済みのはずなので、仮説から削除

**新たな仮説の追加:**
- 今回の振り返りで矛盾する観察、確信度の低い教訓、ルール化するには早い気づきがあれば追加
- 各仮説には: タイトル、初出Opportunity ID、支持/否定カウント、内容、検証条件を記載
- 最大10件を維持。超過時は支持0かつ古いものから削除

"""


def trigger_early_exit_reflection(opp_id: int, fee_rate_pct: float | None = None) -> None:
    """Launch reflection with agent fallback chain for early exit."""
    with get_session() as session:
        opp = (
            session.query(EarlyExitOpportunity)
            .filter(EarlyExitOpportunity.id == opp_id)
            .first()
        )
        if not opp:
            logger.warning(f"EarlyExitOpportunity {opp_id} not found")
            return

        opp_dict = {
            "id": opp.id,
            "trade_id": opp.trade_id,
            "coin": opp.coin,
            "side": opp.side,
            "entry_price": opp.entry_price,
            "exit_price": opp.exit_price,
            "actual_pnl": opp.actual_pnl,
            "exit_time": opp.exit_time.isoformat() if opp.exit_time else "?",
            "window_end_time": opp.window_end_time.isoformat() if opp.window_end_time else "?",
            "price_at_window_end": opp.price_at_window_end,
            "max_favorable_price": opp.max_favorable_price,
            "hypothetical_pnl": opp.hypothetical_pnl,
            "max_hypothetical_pnl": opp.max_hypothetical_pnl,
            "chart_archive_dir": opp.chart_archive_dir,
        }

    archive_dir = opp_dict.get("chart_archive_dir")
    if not archive_dir or not os.path.isdir(archive_dir):
        logger.info(f"No archive dir for early exit {opp_id}, skipping reflection")
        return

    if fee_rate_pct is None:
        from src.trader import get_fee_rate_pct
        fee_rate_pct = get_fee_rate_pct()
    prompt = _build_early_exit_prompt(opp_dict, fee_rate_pct)

    chart_paths = [
        f"{archive_dir}/{f}" for f in os.listdir(archive_dir) if f.endswith(".png")
    ] if os.path.isdir(archive_dir) else []

    def db_update_fn(status: str):
        with get_session() as session:
            opp = (
                session.query(EarlyExitOpportunity)
                .filter(EarlyExitOpportunity.id == opp_id)
                .first()
            )
            if opp:
                opp.status = status
                if status == "reflected":
                    opp.reflection_path = f"{REFLECTIONS_DIR}/early_exit_{opp_id}.md"
                session.commit()

    from src.reflection_executor import execute_reflection
    execute_reflection(
        reflection_type="early_exit",
        identifier=f"early_exit_{opp_id}",
        claude_prompt=prompt,
        expected_reflection_path=f"{REFLECTIONS_DIR}/early_exit_{opp_id}.md",
        archive_dir=archive_dir,
        trade_data=opp_dict,
        chart_paths=chart_paths,
        db_update_fn=db_update_fn,
    )


def check_pending_early_exits() -> None:
    """Check pending early-exit opportunities that have passed their review window.

    Called by the scheduler every 30 minutes. For each pending opportunity
    whose window_end_time has passed, fetches price data and decides whether
    to trigger a reflection (improvement >= threshold) or skip.

    Also retries stale "checked" entries whose reflection thread was killed
    (e.g. container restart) before the reflection file was written.
    """
    now = datetime.utcnow()

    with get_session() as session:
        pending = (
            session.query(EarlyExitOpportunity)
            .filter(
                EarlyExitOpportunity.status == "pending",
                EarlyExitOpportunity.window_end_time <= now,
            )
            .all()
        )

        # Stale entries: reflection was triggered but thread was killed
        # (e.g. container restart) before reflection_path was written.
        # "checked"  → trigger_early_exit_reflection was called but thread never started
        # "reflecting" → thread started but was killed (check_time > 20 min ago)
        stale_cutoff = now - timedelta(minutes=20)
        stale = [
            opp for opp in (
                session.query(EarlyExitOpportunity)
                .filter(
                    EarlyExitOpportunity.reflection_path == None,  # noqa: E711
                    (
                        (EarlyExitOpportunity.status == "checked") |
                        (
                            (EarlyExitOpportunity.status == "reflecting") &
                            (EarlyExitOpportunity.check_time <= stale_cutoff)
                        )
                    ),
                )
                .all()
            )
            if opp.chart_archive_dir and os.path.isdir(opp.chart_archive_dir)
        ]

        if not pending and not stale:
            return

        logger.info(
            f"Checking {len(pending)} pending early exit opportunities"
            + (f", retrying {len(stale)} stale" if stale else "")
        )

        from src.trader import get_fee_rate_pct
        fee_rate_pct = get_fee_rate_pct()
        threshold_pct = fee_rate_pct * settings.hold_reflection_min_pnl_multiplier

        for opp in pending:
            try:
                # Convert actual_pnl (USD) to % for comparison
                actual_pnl_pct = opp.actual_pnl / (opp.entry_price * 1) * 100 if opp.entry_price else 0
                # For long: pnl_pct = (exit - entry) / entry * 100
                if opp.side == "long":
                    actual_pnl_pct = (opp.exit_price - opp.entry_price) / opp.entry_price * 100
                else:
                    actual_pnl_pct = (opp.entry_price - opp.exit_price) / opp.entry_price * 100

                price_at_end, max_price, hyp_pnl_pct, max_hyp_pnl_pct = _calculate_post_exit_outcomes(
                    opp.coin,
                    opp.side,
                    opp.entry_price,
                    opp.exit_time,
                    opp.window_end_time,
                )

                improvement_pct = hyp_pnl_pct - actual_pnl_pct

                opp.check_time = now
                opp.price_at_window_end = price_at_end
                opp.max_favorable_price = max_price
                opp.hypothetical_pnl = hyp_pnl_pct
                opp.max_hypothetical_pnl = max_hyp_pnl_pct

                if improvement_pct >= threshold_pct:
                    opp.status = "checked"
                    session.commit()
                    logger.info(
                        f"Early exit {opp.id} (trade {opp.trade_id}): "
                        f"holding would have improved PnL by {improvement_pct:.2f}% "
                        f"— triggering reflection"
                    )
                    trigger_early_exit_reflection(opp.id, fee_rate_pct=fee_rate_pct)
                else:
                    opp.status = "skipped"
                    if opp.chart_archive_dir:
                        shutil.rmtree(opp.chart_archive_dir, ignore_errors=True)
                    session.commit()
                    logger.info(
                        f"Early exit {opp.id}: improvement {improvement_pct:.2f}% "
                        f"below threshold {threshold_pct:.2f}% — skipped"
                    )

            except Exception as e:
                logger.error(f"Error checking early exit opportunity {opp.id}: {e}")
                session.commit()

        for opp in stale:
            logger.info(
                f"Retrying stale early exit {opp.id} (trade {opp.trade_id}): "
                f"status=checked but reflection_path=NULL — relaunching"
            )
            trigger_early_exit_reflection(opp.id, fee_rate_pct=fee_rate_pct)
