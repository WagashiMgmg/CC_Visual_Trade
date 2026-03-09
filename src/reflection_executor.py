"""
Reflection executor with agent fallback chain.

Executes reflection subprocesses using the same MAGI agents (Melchior → Balthazar → Caspar)
with fallback. Runs in a background thread to avoid blocking the main trading loop.

Fallback chain:
  1. Claude Sonnet (full tool support: Read, Write, Edit, Bash)
  2. Gemini (text-only output, Python handles file I/O)
  3. Claude Haiku (full tool support, lighter model)
  4. Minimal Python fallback (save raw data for retry)
"""

import json
import logging
import os
import re
import shutil
import threading
from collections.abc import Callable
from datetime import datetime

from src.magi import (
    AgentResult,
    CasparAgent,
    ClaudeAgent,
    GeminiAgent,
    MagiAgent,
    run_with_fallback,
)
from src.notify import send_discord

logger = logging.getLogger(__name__)

REFLECTIONS_DIR = "/app/data/reflections"
PENDING_DIR = f"{REFLECTIONS_DIR}/pending"

# Limit concurrent agent calls to avoid API rate limiting (Claude Sonnet 429)
_reflection_semaphore = threading.Semaphore(2)


def execute_reflection(
    reflection_type: str,
    identifier: str,
    claude_prompt: str,
    expected_reflection_path: str,
    archive_dir: str | None,
    trade_data: dict,
    chart_paths: list[str] | None = None,
    db_update_fn: Callable | None = None,
) -> None:
    """Launch reflection in a background thread with agent fallback chain.

    Args:
        reflection_type: "trade" | "hold" | "early_exit"
        identifier: e.g. "trade_19", "hold_73"
        claude_prompt: Full prompt with tool instructions for Claude agents.
        expected_reflection_path: Where the reflection file should be written.
        archive_dir: Chart archive directory (cleaned up on success only).
        trade_data: Raw data dict for minimal fallback.
        chart_paths: PNG paths for Gemini @ syntax.
        db_update_fn: Optional callable(status: str) to update DB status.
    """
    thread = threading.Thread(
        target=_run_reflection,
        args=(reflection_type, identifier, claude_prompt, expected_reflection_path,
              archive_dir, trade_data, chart_paths, db_update_fn),
        daemon=True,
    )
    thread.start()
    logger.info(f"[ReflectionExecutor] Launched background thread for {identifier}")


def _run_reflection(
    reflection_type: str,
    identifier: str,
    claude_prompt: str,
    expected_reflection_path: str,
    archive_dir: str | None,
    trade_data: dict,
    chart_paths: list[str] | None,
    db_update_fn: Callable | None,
) -> None:
    """Background thread: try agents in fallback order."""
    with _reflection_semaphore:
        _run_reflection_inner(
            reflection_type, identifier, claude_prompt, expected_reflection_path,
            archive_dir, trade_data, chart_paths, db_update_fn,
        )


def _run_reflection_inner(
    reflection_type: str,
    identifier: str,
    claude_prompt: str,
    expected_reflection_path: str,
    archive_dir: str | None,
    trade_data: dict,
    chart_paths: list[str] | None,
    db_update_fn: Callable | None,
) -> None:
    """Actual reflection logic (called inside semaphore)."""
    if db_update_fn:
        try:
            db_update_fn("reflecting")
        except Exception as e:
            logger.warning(f"[ReflectionExecutor] DB status update failed: {e}")

    agents: list[MagiAgent] = [ClaudeAgent(), GeminiAgent(), CasparAgent()]
    failure_reasons: list[tuple[str, str]] = []

    def prompt_for(agent: MagiAgent) -> str:
        if agent.name == "balthazar":
            return _build_gemini_prompt(claude_prompt, chart_paths or [])
        return claude_prompt

    def tools_for(agent: MagiAgent) -> str:
        if agent.name == "balthazar":
            return ""  # Gemini has no tool support
        return "Read,Write,Edit,Bash"

    result = run_with_fallback(
        agents, prompt_for,
        allowed_tools=tools_for, timeout=900,
    )

    if result is None:
        # Collect failure reasons from agent states
        for agent in agents:
            if not agent.available:
                failure_reasons.append((agent.display, "marked OFFLINE"))
            else:
                failure_reasons.append((agent.display, "returned no output"))

        _write_pending(reflection_type, identifier, claude_prompt,
                       expected_reflection_path, archive_dir, trade_data,
                       chart_paths, failure_reasons)

        if db_update_fn:
            try:
                db_update_fn("reflection_failed")
            except Exception as e:
                logger.warning(f"[ReflectionExecutor] DB status update failed: {e}")

        send_discord(
            f"振り返り全失敗: {identifier}",
            f"全エージェント失敗。PENDING保存済み。\n"
            + "\n".join(f"- {name}: {reason}" for name, reason in failure_reasons),
            color=0xFF0000,
        )
        return

    # Notify if fallback was used
    if result.agent_name == "balthazar":
        send_discord(
            f"振り返りフォールバック: {identifier}",
            f"Gemini で実行（rule.html編集はスキップ）",
            color=0xFF8C00,
        )
        _write_from_stdout(result.stdout, expected_reflection_path, identifier)
    elif result.agent_name == "caspar":
        send_discord(
            f"振り返りフォールバック: {identifier}",
            f"Haiku で実行（Melchior失敗）",
            color=0xFFA500,
        )
        # Claude Haiku writes files via tools, just verify
        if not _verify_output(expected_reflection_path):
            logger.warning(f"[ReflectionExecutor] Haiku did not produce {expected_reflection_path}")
    else:
        # Melchior (primary) — just verify
        if not _verify_output(expected_reflection_path):
            logger.warning(f"[ReflectionExecutor] Melchior did not produce {expected_reflection_path}")

    # Success: clean up archive
    if archive_dir and os.path.isdir(archive_dir):
        shutil.rmtree(archive_dir, ignore_errors=True)
        logger.info(f"[ReflectionExecutor] Cleaned up archive: {archive_dir}")

    if db_update_fn:
        try:
            db_update_fn("reflected")
        except Exception as e:
            logger.warning(f"[ReflectionExecutor] DB status update failed: {e}")

    logger.info(f"[ReflectionExecutor] {identifier} completed via {result.agent_name}")


# ── Gemini prompt conversion ─────────────────────────────────────────────────

def _build_gemini_prompt(claude_prompt: str, chart_paths: list[str]) -> str:
    """Transform Claude tool-use prompt into Gemini text-output prompt.

    Strips tool instructions (Read, Write, Edit, Bash commands) and adds
    @ chart references and structured output instructions.
    """
    # Add chart @ references
    chart_refs = " ".join(f"@{p}" for p in chart_paths) if chart_paths else ""
    chart_section = f"\n\nCharts: {chart_refs}" if chart_refs else ""

    # Strip tool-specific instructions
    lines = claude_prompt.split("\n")
    filtered = []
    skip_code_block = False
    for line in lines:
        # Skip bash code blocks
        if line.strip().startswith("```bash"):
            skip_code_block = True
            continue
        if skip_code_block:
            if line.strip() == "```":
                skip_code_block = False
            continue
        # Skip tool-specific instructions
        if any(kw in line for kw in [
            "Readツールで", "Writeツールで", "Editツールで", "Bashツールで",
            "Readで開いて", "Writeで書き込み", "Editで更新",
            "上記のファイルをすべてRead",
            "rule.html変更後のコミット", "git add prompt/rule.html",
            "rm -rf",
        ]):
            continue
        filtered.append(line)

    cleaned = "\n".join(filtered)

    return (
        f"{cleaned}{chart_section}\n\n"
        f"---\n"
        f"上記の分析結果を以下の形式でそのまま出力してください。"
        f"ファイル書き込みやコマンド実行は不要です。テキストのみ出力してください。\n"
    )


# ── Output handlers ──────────────────────────────────────────────────────────

def _write_from_stdout(stdout: str, reflection_path: str, identifier: str) -> bool:
    """Write Gemini's stdout as the reflection file."""
    os.makedirs(os.path.dirname(reflection_path), exist_ok=True)
    content = stdout.strip()
    if not content:
        logger.warning(f"[ReflectionExecutor] Gemini stdout empty for {identifier}")
        return False
    # Add marker that this was a Gemini fallback
    header = f"<!-- Generated by Gemini fallback -->\n"
    with open(reflection_path, "w") as f:
        f.write(header + content)
    logger.info(f"[ReflectionExecutor] Wrote Gemini reflection to {reflection_path}")
    return True


def _verify_output(reflection_path: str) -> bool:
    """Check that reflection file exists and has reasonable content."""
    if not os.path.isfile(reflection_path):
        return False
    size = os.path.getsize(reflection_path)
    return size > 100  # At least 100 bytes of content


# ── Pending / retry ──────────────────────────────────────────────────────────

def _write_pending(
    reflection_type: str,
    identifier: str,
    claude_prompt: str,
    expected_reflection_path: str,
    archive_dir: str | None,
    trade_data: dict,
    chart_paths: list[str] | None,
    failure_reasons: list[tuple[str, str]],
) -> None:
    """Save all data needed for retry when all agents fail."""
    os.makedirs(PENDING_DIR, exist_ok=True)

    # Save retry JSON
    pending_data = {
        "reflection_type": reflection_type,
        "identifier": identifier,
        "claude_prompt": claude_prompt,
        "expected_reflection_path": expected_reflection_path,
        "archive_dir": archive_dir,
        "trade_data": trade_data,
        "chart_paths": chart_paths,
        "failure_reasons": failure_reasons,
        "retry_count": 0,
        "created_at": datetime.utcnow().isoformat(),
    }
    pending_path = f"{PENDING_DIR}/{reflection_type}_{identifier}.json"
    with open(pending_path, "w") as f:
        json.dump(pending_data, f, ensure_ascii=False, indent=2, default=str)

    # Write [PENDING] reflection file
    os.makedirs(os.path.dirname(expected_reflection_path), exist_ok=True)
    failures_text = "\n".join(f"- {name}: {reason}" for name, reason in failure_reasons)
    with open(expected_reflection_path, "w") as f:
        f.write(
            f"## [PENDING] {identifier}\n"
            f"**Status**: AI reflection failed — awaiting retry\n"
            f"**Created**: {datetime.utcnow().isoformat()}\n"
            f"**Archive**: {archive_dir or 'N/A'}\n\n"
            f"### Failure reasons\n{failures_text}\n"
        )

    logger.error(
        f"[ReflectionExecutor] All agents failed for {identifier}. "
        f"Pending data saved to {pending_path}"
    )


MAX_RETRIES = 3


def retry_pending_reflections() -> None:
    """Scan pending directory and retry failed reflections.

    Called by scheduler every 30 minutes.
    """
    if not os.path.isdir(PENDING_DIR):
        return

    pending_files = [f for f in os.listdir(PENDING_DIR) if f.endswith(".json")]
    if not pending_files:
        return

    logger.info(f"[ReflectionExecutor] Found {len(pending_files)} pending reflection(s)")

    for fname in pending_files:
        fpath = f"{PENDING_DIR}/{fname}"
        try:
            with open(fpath) as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"[ReflectionExecutor] Failed to read {fpath}: {e}")
            continue

        retry_count = data.get("retry_count", 0)
        identifier = data.get("identifier", fname)

        if retry_count >= MAX_RETRIES:
            # Abandon
            logger.warning(f"[ReflectionExecutor] {identifier} exceeded max retries, abandoning")
            send_discord(
                f"振り返りリトライ上限: {identifier}",
                f"{MAX_RETRIES}回リトライ失敗。手動対応が必要です。",
                color=0xFF0000,
            )
            # Move to abandoned
            abandoned_path = fpath.replace(".json", ".abandoned.json")
            os.rename(fpath, abandoned_path)
            continue

        # Increment retry count
        data["retry_count"] = retry_count + 1
        with open(fpath, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

        logger.info(f"[ReflectionExecutor] Retrying {identifier} (attempt {retry_count + 1}/{MAX_RETRIES})")

        # Build db_update_fn if applicable
        db_update_fn = _build_db_update_fn(data)

        # Re-execute (this launches a new background thread)
        execute_reflection(
            reflection_type=data["reflection_type"],
            identifier=data["identifier"],
            claude_prompt=data["claude_prompt"],
            expected_reflection_path=data["expected_reflection_path"],
            archive_dir=data.get("archive_dir"),
            trade_data=data.get("trade_data", {}),
            chart_paths=data.get("chart_paths"),
            db_update_fn=db_update_fn,
        )

        # Remove pending file (execute_reflection will create a new one if it fails again)
        os.remove(fpath)


def _build_db_update_fn(data: dict) -> Callable | None:
    """Build a DB update function for hold/early_exit reflections."""
    reflection_type = data.get("reflection_type")

    if reflection_type == "hold":
        opp_id = data.get("trade_data", {}).get("id")
        if opp_id is None:
            return None

        def update(status: str):
            from src.database import HoldOpportunity, get_session
            with get_session() as session:
                opp = session.query(HoldOpportunity).filter(HoldOpportunity.id == opp_id).first()
                if opp:
                    opp.status = status
                    if status == "reflected":
                        opp.reflection_path = data.get("expected_reflection_path")
                    session.commit()
        return update

    elif reflection_type == "early_exit":
        opp_id = data.get("trade_data", {}).get("id")
        if opp_id is None:
            return None

        def update(status: str):
            from src.database import EarlyExitOpportunity, get_session
            with get_session() as session:
                opp = session.query(EarlyExitOpportunity).filter(
                    EarlyExitOpportunity.id == opp_id
                ).first()
                if opp:
                    opp.status = status
                    if status == "reflected":
                        opp.reflection_path = data.get("expected_reflection_path")
                    session.commit()
        return update

    return None
