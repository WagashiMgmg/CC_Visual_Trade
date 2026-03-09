"""
Reflection Digest — curates past trade/hold reflections into a compact digest.

Runs every 12 hours via scheduler. Reads all reflection files, sends them to
Claude for impact-based ranking, and writes the top entries to
/app/data/reflection_digest.md for injection into MAGI prompts.
"""

import logging
import os
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

REFLECTIONS_DIR = "/app/data/reflections"
ARCHIVE_DIR = "/app/data/reflections/archive"
DIGEST_FILE = "/app/data/reflection_digest.md"


def _read_reflections() -> list[tuple[str, str, str]]:
    """
    Read all reflection files from REFLECTIONS_DIR.
    Returns list of (filename, category, content).
    """
    if not os.path.isdir(REFLECTIONS_DIR):
        return []

    results = []
    for fname in sorted(os.listdir(REFLECTIONS_DIR)):
        fpath = os.path.join(REFLECTIONS_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        if not fname.endswith(".md"):
            continue

        try:
            with open(fpath) as f:
                content = f.read()
        except Exception as e:
            logger.warning(f"Failed to read {fpath}: {e}")
            continue

        if fname.startswith("trade_"):
            match = re.search(r"(LONG|SHORT)", content[:500])
            category = match.group(1) if match else "UNKNOWN"
        elif fname.startswith("hold_"):
            match = re.search(r"見逃し(ロング|ショート)", content[:200])
            if match:
                category = "HOLD_LONG" if match.group(1) == "ロング" else "HOLD_SHORT"
            else:
                category = "HOLD"
        else:
            continue

        results.append((fname, category, content))

    return results


def _build_curation_prompt(reflections: list[tuple[str, str, str]]) -> str:
    """Build the Claude prompt for digest curation."""
    sections = []
    for fname, category, content in reflections:
        sections.append(f"=== {fname} ({category}) ===\n{content}")

    all_text = "\n\n".join(sections)

    return f"""以下は過去のトレード・HOLD振り返りファイルの全文です。

{all_text}

# 指示
上記の振り返りをインパクトファクター順に評価し、以下の基準で選定してください。

## 評価基準
- Actionability (40%): 具体的・再利用可能な教訓があるか
- Pattern recurrence (30%): 同じミスが複数の振り返りで繰り返されているか
- Recency (20%): 最近の振り返りほど現在の市場環境に適合
- Magnitude (10%): PnLインパクト

## 出力要件
- LONG / SHORT / HOLD_LONG / HOLD_SHORT の各カテゴリからインパクトファクター上位4件を選出
- 各エントリを日本語100-150文字に圧縮
- カテゴリ内のファイルが4件未満の場合はすべて採用
- HOLD_LONG/HOLD_SHORTが0件のセクションは出力しない
- Write ツールで `{DIGEST_FILE}` に以下のフォーマットで書き出すこと

## 出力フォーマット
```markdown
# 過去の振り返りから学んだ教訓（自動キュレーション）
_最終更新: YYYY-MM-DD HH:MM UTC_

## LONGトレードの教訓
- [Trade N] （100-150文字の圧縮教訓）
- ...

## SHORTトレードの教訓
- [Trade N] （100-150文字の圧縮教訓）
- ...

## HOLD（ロング見逃し）の教訓
- [Hold N] （100-150文字の圧縮教訓）
- ...

## HOLD（ショート見逃し）の教訓
- [Hold N] （100-150文字の圧縮教訓）
- ...
```

注意: ファイル名から番号を抽出して [Trade N] / [Hold N] の形式で記載すること。
"""


def _parse_selected_ids(digest_content: str) -> set[str]:
    """
    Parse the digest file to extract selected file identifiers.
    Returns set of filenames like {'trade_1.md', 'hold_11.md'}.
    """
    selected = set()
    for match in re.finditer(r"\[Trade\s+(\d+)\]", digest_content):
        selected.add(f"trade_{match.group(1)}.md")
    for match in re.finditer(r"\[Hold\s+(\d+)\]", digest_content):
        selected.add(f"hold_{match.group(1)}.md")
    return selected


def _archive_unselected(selected: set[str]) -> int:
    """
    Move unselected reflection files to archive directory.
    Returns count of archived files.
    """
    if not os.path.isdir(REFLECTIONS_DIR):
        return 0

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    count = 0
    for fname in os.listdir(REFLECTIONS_DIR):
        fpath = os.path.join(REFLECTIONS_DIR, fname)
        if not os.path.isfile(fpath) or not fname.endswith(".md"):
            continue
        if not (fname.startswith("trade_") or fname.startswith("hold_")):
            continue
        if fname in selected:
            continue
        shutil.move(fpath, os.path.join(ARCHIVE_DIR, fname))
        count += 1
    return count


def curate_digest() -> None:
    """
    Main entry point: read reflections, send to Claude for curation,
    write digest, and archive unselected files.
    """
    reflections = _read_reflections()
    if not reflections:
        logger.info("No reflection files found, skipping digest curation")
        return

    logger.info(f"Curating digest from {len(reflections)} reflection files")
    prompt = _build_curation_prompt(reflections)

    try:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--allowedTools", "Read,Write",
                "--permission-mode", "bypassPermissions",
            ],
            cwd="/app",
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        if result.returncode != 0:
            logger.error(f"Digest curation subprocess failed: {result.stderr[:500]}")
            return
    except subprocess.TimeoutExpired:
        logger.error("Digest curation subprocess timed out (600s)")
        return
    except Exception as e:
        logger.error(f"Failed to run digest curation: {e}")
        return

    # Verify digest was written
    if not os.path.isfile(DIGEST_FILE):
        logger.error(f"Digest file not created: {DIGEST_FILE}")
        return

    # Parse selected IDs and archive unselected
    try:
        with open(DIGEST_FILE) as f:
            digest_content = f.read()
        selected = _parse_selected_ids(digest_content)
        archived = _archive_unselected(selected)
        logger.info(
            f"Digest curation complete: {len(selected)} selected, "
            f"{archived} archived to {ARCHIVE_DIR}"
        )
    except Exception as e:
        logger.error(f"Failed to archive unselected reflections: {e}")
