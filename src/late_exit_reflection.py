"""
Late-exit reflection for positions that gave back significant gains before closing.

When a trade closes (agent EXIT or 4h forced-close), we fetch 1-minute candles
over the trade period to find the Maximum Favorable Excursion (MFE). If the MFE
PnL was significantly better than the actual exit PnL, the agent held too long
and we trigger a reflection to improve HOLD/EXIT rules.
"""

import logging
import os
from datetime import datetime, timezone

import requests

from src.config import settings
from src.reflection import RULE_CONSISTENCY_CHECK, fee_note

logger = logging.getLogger(__name__)

REFLECTIONS_DIR = "/app/data/reflections"
HYPOTHESES_FILE = "/app/data/reflections/hypotheses.md"


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


def _calculate_mfe(
    coin: str,
    side: str,
    entry_price: float,
    entry_time: datetime,
    exit_time: datetime,
) -> tuple[float, float, str]:
    """Calculate Maximum Favorable Excursion during the trade period.

    Returns (best_price, best_pnl_pct, best_time_iso).
    best_pnl_pct is price change % from entry.
    """
    candles = _fetch_candles(coin, entry_time, exit_time)
    if not candles:
        return entry_price, 0.0, entry_time.isoformat()

    if side == "long":
        best_candle = max(candles, key=lambda c: float(c["h"]))
        best_price = float(best_candle["h"])
        best_pnl_pct = (best_price - entry_price) / entry_price * 100
    else:
        best_candle = min(candles, key=lambda c: float(c["l"]))
        best_price = float(best_candle["l"])
        best_pnl_pct = (entry_price - best_price) / entry_price * 100

    best_time = datetime.fromtimestamp(
        int(best_candle["t"]) / 1000, tz=timezone.utc
    ).isoformat()

    return best_price, best_pnl_pct, best_time


def _build_late_exit_prompt(
    trade_info: dict,
    best_price: float,
    best_pnl_pct: float,
    best_time: str,
    fee_rate_pct: float | None = None,
) -> str:
    trade_id = trade_info["trade_id"]
    coin = trade_info["coin"]
    side = trade_info["side"]
    entry_price = trade_info["entry_price"]
    exit_price = trade_info["exit_price"]
    actual_pnl_usd = trade_info["pnl_usd"]
    size_usd = trade_info.get("size_usd", 0) or 1
    actual_pnl_pct = actual_pnl_usd / size_usd * 100
    entry_time = trade_info["entry_time"]
    exit_time = trade_info["exit_time"]
    archive_dir = trade_info.get("archive_dir", "")

    def fmt_pnl(v):
        return f"+{v:.2f}%" if v >= 0 else f"{v:.2f}%"

    actual_str = fmt_pnl(actual_pnl_pct)
    best_str = fmt_pnl(best_pnl_pct)
    giveback_pct = best_pnl_pct - actual_pnl_pct
    giveback_str = fmt_pnl(giveback_pct)
    side_jp = "ロング" if side == "long" else "ショート"
    fee_block = fee_note(fee_rate_pct) if fee_rate_pct is not None else ""

    return f"""# 遅延EXIT振り返りタスク

{fee_block}
## トレード情報
- Trade ID: {trade_id}
- コイン: {coin}
- サイド: {side.upper()} ({side_jp})
- エントリー価格: ${entry_price:.2f}
- エントリー時刻: {entry_time}
- EXIT価格（実際）: ${exit_price:.2f}
- EXIT時刻: {exit_time}
- 実際のPnL: {actual_str}

## トレード中の最大有利価格（MFE）
- 最大有利価格: ${best_price:.2f}
- MFE到達時刻: {best_time}
- MFE時点でのPnL（エントリーから）: {best_str}
- MFE後に戻したPnL（Giveback）: {giveback_str}（大きいほど「もっと早く出れば良かった」）

## 指示

ステップ0: `{HYPOTHESES_FILE}` をReadツールで読み込んでください（存在しない場合はスキップ）。未解決の仮説がある場合、今回の結果がそれらを支持・否定するかステップ2で検証してください。

ステップ1: エントリー時点のアーカイブチャートを確認してください。
```bash
ls {archive_dir}
```
上記のファイルをすべてReadツールで開いて分析してください。

ステップ2: 以下を分析してください。必要であれば `/app/data/reflections/` 以下の過去の振り返りも参照してください。
- MFE到達後、なぜ利益を守れなかったか（Giveback: {giveback_str}）
- MFE付近でEXITすべきシグナルがあったか（価格の天井サイン、RSI過買い、出来高変化等）
- HOLDし続けた判断ロジックの何が問題だったか
- EXITルールに何を追加・変更すれば防げたか

ステップ3: Writeツールで `{REFLECTIONS_DIR}/late_exit_{trade_id}.md` に振り返り全文を書き込んでください。

フォーマット:
```markdown
## LateExit {trade_id} — {side.upper()} {coin} — 実際PnL: {actual_str} / MFE: {best_str}（Giveback: {giveback_str}）
**エントリー時刻**: {entry_time}
**EXIT時刻**: {exit_time}
**EXIT価格**: ${exit_price:.2f}
**MFE価格**: ${best_price:.2f}（{best_time}）
**Giveback**: {giveback_str}

### 遅延EXIT評価
（なぜMFEで利益を確保できなかったか）

### 主因分析
（MFE後の値動きの特徴と、出られなかった理由）

### 見落としたEXITシグナル
（MFE付近でEXITすべきだったサイン、あるいはHOLDが正当だった場合はその根拠）

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
- Givebackが大きかった場合（giveback > 0）: 「このシグナルが見られる時はEXITすべき」条件を学習済みルールに追加・強化
- MFE後のGivebackが許容範囲だった場合: EXITルールを支持する根拠を記録し、`<small class="rule-stat">` の統計を更新

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
git pull --rebase --autostash && git add prompt/rule.html && git commit -m "reflect: LateExit {trade_id} — rule.html update" && git push
```

ステップ5: `{HYPOTHESES_FILE}` を更新してください（Writeツール使用）。以下のルールに従ってください:

**仮説の検証結果を反映:**
- 今回のトレードで**支持**された仮説 → `支持` カウントを+1
- 今回のトレードで**否定**された仮説 → 削除（理由はステップ3の振り返りファイルに記録済み）
- **支持2回以上**に達した仮説 → ステップ4でルールに昇格済みのはずなので、仮説から削除

**新たな仮説の追加:**
- 今回の振り返りで矛盾する観察、確信度の低い教訓、ルール化するには早い気づきがあれば追加
- 各仮説には: タイトル、初出Trade ID、支持/否定カウント、内容、検証条件を記載
- 最大10件を維持。超過時は支持0かつ古いものから削除

"""


def check_and_trigger_late_exit(
    trade_info: dict,
    fee_rate_pct: float | None = None,
) -> None:
    """Check if MFE giveback exceeds threshold; if so, trigger late-exit reflection.

    Called immediately after a trade closes (from close.py and trader.py).
    Runs the reflection in a background thread via execute_reflection().
    """
    entry_time = trade_info.get("entry_time")
    exit_time = trade_info.get("exit_time") or datetime.utcnow()

    if isinstance(entry_time, str):
        entry_time = datetime.fromisoformat(entry_time)
    if isinstance(exit_time, str):
        exit_time = datetime.fromisoformat(exit_time)

    if entry_time is None:
        logger.warning("check_and_trigger_late_exit: entry_time missing, skipping")
        return

    duration_secs = (exit_time - entry_time).total_seconds()
    if duration_secs < 5 * 60:
        logger.info("Late exit check: trade too short (<5m), skipping")
        return

    coin = trade_info.get("coin", "")
    side = trade_info.get("side", "")
    entry_price = trade_info.get("entry_price", 0.0)
    actual_pnl_usd = trade_info.get("pnl_usd", 0.0)
    size_usd = trade_info.get("size_usd", 0) or 1
    actual_pnl_pct = actual_pnl_usd / size_usd * 100
    trade_id = trade_info.get("trade_id")

    try:
        best_price, best_pnl_pct, best_time = _calculate_mfe(
            coin, side, entry_price, entry_time, exit_time
        )
    except Exception as e:
        logger.error(f"Late exit MFE calculation failed for trade {trade_id}: {e}")
        return

    giveback_pct = best_pnl_pct - actual_pnl_pct

    if fee_rate_pct is None:
        from src.trader import get_fee_rate_pct
        fee_rate_pct = get_fee_rate_pct()

    threshold_pct = fee_rate_pct * settings.hold_reflection_min_pnl_multiplier

    if giveback_pct < threshold_pct:
        logger.info(
            f"Late exit check trade {trade_id}: giveback={giveback_pct:.2f}% "
            f"below threshold={threshold_pct:.2f}% — skipped"
        )
        return

    logger.info(
        f"Late exit trade {trade_id}: MFE={best_price:.2f} giveback={giveback_pct:.2f}% "
        f"≥ threshold={threshold_pct:.2f}% — triggering reflection"
    )

    prompt = _build_late_exit_prompt(
        trade_info, best_price, best_pnl_pct, best_time, fee_rate_pct
    )
    archive_dir = trade_info.get("archive_dir", "")
    chart_paths = (
        [f"{archive_dir}/{f}" for f in os.listdir(archive_dir) if f.endswith(".png")]
        if archive_dir and os.path.isdir(archive_dir)
        else []
    )

    from src.reflection_executor import execute_reflection
    execute_reflection(
        reflection_type="late_exit",
        identifier=f"late_exit_{trade_id}",
        claude_prompt=prompt,
        expected_reflection_path=f"{REFLECTIONS_DIR}/late_exit_{trade_id}.md",
        archive_dir=archive_dir,
        trade_data=trade_info,
        chart_paths=chart_paths,
    )
