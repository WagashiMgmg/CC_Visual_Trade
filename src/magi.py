"""
MAGI System — EVA-inspired multi-agent voting for trade decisions.

Melchior  = Claude Code CLI   (master agent)
Balthazar = Gemini CLI
Caspar    = placeholder (future expansion)

Voting rounds:
  Round 0: Independent analysis
  Round 1: Share each other's conclusions, re-analyze
  Round 2: Internet-augmented research round
  Round 3: Compare vs Melchior's reasoning; choose stronger logic
  No consensus after Round 3 → Melchior's decision adopted
"""

import logging
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from src.database import MagiVote, get_session

logger = logging.getLogger(__name__)

_DECISION_RE = re.compile(r"DECISION:\s*(LONG|SHORT|HOLD|EXIT)", re.IGNORECASE)
_REASON_RE   = re.compile(r"REASON:\s*(.+?)(?:\n\n|\Z)", re.DOTALL)


def _parse_vote(output: str) -> dict:
    decision = "HOLD"
    reasoning = ""
    m = _DECISION_RE.search(output)
    if m:
        decision = m.group(1).upper()
    r = _REASON_RE.search(output)
    if r:
        reasoning = r.group(1).strip()
    return {"decision": decision, "reasoning": reasoning, "raw_output": output}


# ── Base agent ────────────────────────────────────────────────────────────────

class MagiAgent:
    name: str
    display: str
    available: bool = True

    def analyze(self, prompt: str, charts: list[str], allowed_tools: str = "Read") -> dict | None:
        raise NotImplementedError

    def _save_vote(self, cycle_id: int, round_num: int, vote: dict):
        with get_session() as session:
            session.add(MagiVote(
                cycle_id=cycle_id,
                agent_name=self.name,
                round=round_num,
                decision=vote["decision"],
                reasoning=vote.get("reasoning", ""),
                raw_output=(vote.get("raw_output", "") or "")[:5000],
                timestamp=datetime.utcnow(),
            ))
            session.commit()


# ── Melchior (Claude Code CLI) ────────────────────────────────────────────────

class ClaudeAgent(MagiAgent):
    name    = "melchior"
    display = "Melchior"

    def analyze(self, prompt: str, charts: list[str], allowed_tools: str = "Read") -> dict | None:
        try:
            result = subprocess.run(
                [
                    "claude",
                    "-p", prompt,
                    "--allowedTools", allowed_tools,
                    "--permission-mode", "bypassPermissions",
                ],
                capture_output=True,
                text=True,
                timeout=600,
                cwd="/app",
                env=os.environ.copy(),
            )
            output = result.stdout
            if result.stderr:
                logger.debug(f"[Melchior] stderr: {result.stderr[:200]}")
            return _parse_vote(output)
        except subprocess.TimeoutExpired:
            logger.error("[Melchior] timed out")
            return _parse_vote("DECISION: HOLD\nREASON: タイムアウト")
        except FileNotFoundError:
            logger.error("[Melchior] claude CLI not found")
            return _parse_vote("DECISION: HOLD\nREASON: claude CLIが見つかりません")
        except Exception as e:
            logger.error(f"[Melchior] error: {e}")
            return _parse_vote("DECISION: HOLD\nREASON: エラー発生")


# ── Balthazar (Gemini CLI) ────────────────────────────────────────────────────

class GeminiAgent(MagiAgent):
    name    = "balthazar"
    display = "Balthazar"

    def _check_available(self) -> bool:
        try:
            r = subprocess.run(["gemini", "--version"], capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    def analyze(self, prompt: str, charts: list[str], allowed_tools: str = "Read") -> dict | None:
        chart_refs = " ".join(f"@{p}" for p in charts)
        full_prompt = f"{prompt}\n\nCharts: {chart_refs}" if charts else prompt

        output = self._run_gemini(full_prompt)
        if output is None:
            # Fallback: text-only prompt
            logger.warning("[Balthazar] @ syntax failed, retrying text-only")
            output = self._run_gemini(prompt)
        if output is None:
            return _parse_vote("DECISION: HOLD\nREASON: Gemini CLIエラー")
        return _parse_vote(output)

    def _run_gemini(self, prompt: str) -> str | None:
        try:
            result = subprocess.run(
                ["gemini", "-p", prompt],
                capture_output=True,
                text=True,
                timeout=600,
                cwd="/app",
                env=os.environ.copy(),
            )
            if result.returncode != 0:
                logger.warning(f"[Balthazar] non-zero exit: {result.stderr[:200]}")
                return None
            return result.stdout
        except subprocess.TimeoutExpired:
            logger.error("[Balthazar] timed out")
            return None
        except FileNotFoundError:
            logger.warning("[Balthazar] gemini CLI not found — marking unavailable")
            self.available = False
            return None
        except Exception as e:
            logger.error(f"[Balthazar] error: {e}")
            return None


# ── Caspar (placeholder) ──────────────────────────────────────────────────────

class CasparAgent(MagiAgent):
    name      = "caspar"
    display   = "Caspar"
    available = False

    def analyze(self, prompt: str, charts: list[str], allowed_tools: str = "Read") -> dict | None:
        return None


# ── MagiSystem ────────────────────────────────────────────────────────────────

class MagiSystem:
    MAX_ROUNDS = 4  # rounds 0-3; no consensus after round 3 → master wins

    def __init__(self):
        self.agents: list[MagiAgent] = [
            ClaudeAgent(),
            GeminiAgent(),
            CasparAgent(),
        ]
        self._melchior: ClaudeAgent = self.agents[0]  # type: ignore

    def _active_agents(self) -> list[MagiAgent]:
        return [a for a in self.agents if a.available]

    def _majority_threshold(self) -> int:
        n = len(self._active_agents())
        return n // 2 + 1

    def _consensus(self, votes: dict[str, str]) -> str | None:
        """Return consensus decision or None if no majority."""
        from collections import Counter
        counts = Counter(votes.values())
        threshold = self._majority_threshold()
        for decision, count in counts.most_common():
            if count >= threshold:
                return decision
        return None

    def _run_agents_parallel(
        self,
        prompts: dict[str, str],    # agent_name → prompt
        charts: list[str],
        allowed_tools: dict[str, str],  # agent_name → tools string
    ) -> dict[str, dict]:
        """Run active agents concurrently; return {agent_name: vote}."""
        active = self._active_agents()
        results: dict[str, dict] = {}

        with ThreadPoolExecutor(max_workers=len(active)) as executor:
            futures = {
                executor.submit(
                    agent.analyze,
                    prompts.get(agent.name, prompts.get("default", "")),
                    charts,
                    allowed_tools.get(agent.name, "Read"),
                ): agent
                for agent in active
            }
            for future in as_completed(futures):
                agent = futures[future]
                try:
                    vote = future.result()
                    if vote:
                        results[agent.name] = vote
                    else:
                        results[agent.name] = {"decision": "HOLD", "reasoning": "エラー", "raw_output": ""}
                except Exception as e:
                    logger.error(f"[MAGI] {agent.name} future error: {e}")
                    results[agent.name] = {"decision": "HOLD", "reasoning": "エラー", "raw_output": ""}

        return results

    def _save_round_votes(self, cycle_id: int, round_num: int, votes: dict[str, dict]):
        for agent_name, vote in votes.items():
            agent = next((a for a in self.agents if a.name == agent_name), None)
            if agent:
                agent._save_vote(cycle_id, round_num, vote)

    def _log_round(self, round_num: int, votes: dict[str, dict], consensus: str | None):
        parts = ", ".join(
            f"{name.capitalize()}={v['decision']}"
            for name, v in votes.items()
        )
        if consensus:
            logger.info(f"[MAGI] Round {round_num}: {parts} → consensus {consensus}")
        else:
            logger.info(f"[MAGI] Round {round_num}: {parts} → no consensus, proceeding to next round")

    # ── Round builders ────────────────────────────────────────────────────────

    def _build_round0_prompt(self, base_prompt: str, charts: list[str]) -> dict[str, str]:
        """Round 0: independent analysis."""
        chart_list = "\n".join(f"- {p}" for p in charts)
        prompt = (
            f"{base_prompt}\n\n"
            f"あなたはMAGIシステムの一員として独立した分析を行います。\n"
            f"チャートファイル:\n{chart_list}\n\n"
            f"最後に必ず以下のフォーマットで出力してください:\n"
            f"DECISION: LONG or SHORT or HOLD\n"
            f"REASON: （日本語で理由を記述）"
        )
        return {"default": prompt}

    def _build_round0_prompt_in_position(self, base_prompt: str, charts: list[str]) -> dict[str, str]:
        """Round 0 in-position: EXIT or HOLD."""
        chart_list = "\n".join(f"- {p}" for p in charts)
        prompt = (
            f"{base_prompt}\n\n"
            f"あなたはMAGIシステムの一員として独立した分析を行います。\n"
            f"チャートファイル:\n{chart_list}\n\n"
            f"最後に必ず以下のフォーマットで出力してください:\n"
            f"DECISION: EXIT or HOLD\n"
            f"REASON: （日本語で理由を記述）"
        )
        return {"default": prompt}

    def _build_round1_prompts(
        self, base_prompt: str, charts: list[str], prev_votes: dict[str, dict], in_position: bool
    ) -> dict[str, str]:
        """Round 1: share other agents' conclusions."""
        others_summary = "\n".join(
            f"- {name.capitalize()}: {v['decision']} — {v['reasoning'][:200]}"
            for name, v in prev_votes.items()
        )
        decision_fmt = "EXIT or HOLD" if in_position else "LONG or SHORT or HOLD"
        chart_list = "\n".join(f"- {p}" for p in charts)
        prompts = {}
        for agent in self._active_agents():
            my_vote = prev_votes.get(agent.name, {})
            prompt = (
                f"{base_prompt}\n\n"
                f"【再審議 Round 1】他のMAGIエージェントの初回判断:\n{others_summary}\n\n"
                f"あなた({agent.display})の初回判断: {my_vote.get('decision', 'HOLD')}\n"
                f"チャートを再度確認し、他エージェントの意見を踏まえて再判断してください。\n"
                f"チャートファイル:\n{chart_list}\n\n"
                f"最後に必ず以下のフォーマットで出力してください:\n"
                f"DECISION: {decision_fmt}\n"
                f"REASON: （日本語で理由を記述）"
            )
            prompts[agent.name] = prompt
        return prompts

    def _build_round2_prompts(
        self, base_prompt: str, charts: list[str], prev_votes: dict[str, dict], in_position: bool
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Round 2: internet-augmented research."""
        others_summary = "\n".join(
            f"- {name.capitalize()}: {v['decision']} — {v['reasoning'][:200]}"
            for name, v in prev_votes.items()
        )
        decision_fmt = "EXIT or HOLD" if in_position else "LONG or SHORT or HOLD"
        chart_list = "\n".join(f"- {p}" for p in charts)
        prompts = {}
        tools = {}
        for agent in self._active_agents():
            my_vote = prev_votes.get(agent.name, {})
            base = (
                f"{base_prompt}\n\n"
                f"【再審議 Round 2 — 外部情報収集】前回までのMAGI判断:\n{others_summary}\n\n"
                f"あなた({agent.display})の前回判断: {my_vote.get('decision', 'HOLD')}\n"
                f"Bashツールを使ってウェブ検索し、最新のマーケット情報・ニュースを確認した上で再判断してください。\n"
                f"⚠️ Bash はウェブ検索・情報収集のみに使用すること。"
                f"long.py / short.py / close.py などのトレードスクリプトは絶対に実行しないこと。\n"
                f"チャートファイル:\n{chart_list}\n\n"
                f"最後に必ず以下のフォーマットで出力してください:\n"
                f"DECISION: {decision_fmt}\n"
                f"REASON: （日本語で理由を記述）"
            )
            prompts[agent.name] = base
            # Claude gets Bash for web search; Gemini has native web search
            tools[agent.name] = "Read,Bash" if agent.name == "melchior" else "Read"
        return prompts, tools

    def _build_round3_prompts(
        self,
        base_prompt: str,
        charts: list[str],
        prev_votes: dict[str, dict],
        melchior_vote: dict,
        in_position: bool,
    ) -> dict[str, str]:
        """Round 3: compare vs Melchior reasoning."""
        decision_fmt = "EXIT or HOLD" if in_position else "LONG or SHORT or HOLD"
        chart_list = "\n".join(f"- {p}" for p in charts)
        melchior_summary = (
            f"Melchior判断: {melchior_vote.get('decision', 'HOLD')}\n"
            f"根拠: {melchior_vote.get('reasoning', '')[:400]}"
        )
        prompts = {}
        for agent in self._active_agents():
            my_vote = prev_votes.get(agent.name, {})
            prompt = (
                f"{base_prompt}\n\n"
                f"【再審議 Round 3 — マスター比較】\n"
                f"マスターエージェント(Melchior)の判断:\n{melchior_summary}\n\n"
                f"あなた({agent.display})の前回判断: {my_vote.get('decision', 'HOLD')}\n"
                f"根拠: {my_vote.get('reasoning', '')[:200]}\n\n"
                f"チャートを再確認し、自分の根拠とMelchiorの根拠を論理的に比較して、"
                f"より強い根拠を持つ判断を採用してください。\n"
                f"チャートファイル:\n{chart_list}\n\n"
                f"最後に必ず以下のフォーマットで出力してください:\n"
                f"DECISION: {decision_fmt}\n"
                f"REASON: （日本語で理由を記述）"
            )
            prompts[agent.name] = prompt
        return prompts

    # ── Main run ──────────────────────────────────────────────────────────────

    def run(
        self,
        base_prompt: str,
        charts: list[str],
        cycle_id: int,
        in_position: bool = False,
    ) -> dict:
        """
        Run MAGI voting. Returns:
        {
            'decision': str,
            'reasoning': str,
            'rounds': int,    # how many rounds were needed (0-indexed final round + 1)
            'votes': {agent_name: {'decision': ..., 'reasoning': ...}},
            'adopted_by': 'consensus' | 'master',
        }
        """
        all_votes: dict[str, dict] = {}
        final_round = 0

        # ── Round 0: independent analysis ────────────────────────────────────
        if in_position:
            prompts = self._build_round0_prompt_in_position(base_prompt, charts)
        else:
            prompts = self._build_round0_prompt(base_prompt, charts)

        votes = self._run_agents_parallel(
            prompts, charts, {a.name: "Read" for a in self._active_agents()}
        )
        self._save_round_votes(cycle_id, 0, votes)
        all_votes = votes
        decision_map = {k: v["decision"] for k, v in votes.items()}
        consensus = self._consensus(decision_map)
        self._log_round(0, votes, consensus)

        if consensus:
            return self._build_result(consensus, votes, 1, "consensus")

        # ── Round 1: shared context ───────────────────────────────────────────
        final_round = 1
        prompts1 = self._build_round1_prompts(base_prompt, charts, votes, in_position)
        votes1 = self._run_agents_parallel(
            prompts1, charts, {a.name: "Read" for a in self._active_agents()}
        )
        self._save_round_votes(cycle_id, 1, votes1)
        all_votes = votes1
        decision_map = {k: v["decision"] for k, v in votes1.items()}
        consensus = self._consensus(decision_map)
        self._log_round(1, votes1, consensus)

        if consensus:
            return self._build_result(consensus, votes1, 2, "consensus")

        # ── Round 2: internet research ────────────────────────────────────────
        final_round = 2
        prompts2, tools2 = self._build_round2_prompts(base_prompt, charts, votes1, in_position)
        votes2 = self._run_agents_parallel(prompts2, charts, tools2)
        self._save_round_votes(cycle_id, 2, votes2)
        all_votes = votes2
        decision_map = {k: v["decision"] for k, v in votes2.items()}
        consensus = self._consensus(decision_map)
        self._log_round(2, votes2, consensus)

        if consensus:
            return self._build_result(consensus, votes2, 3, "consensus")

        # ── Round 3: compare vs Melchior ─────────────────────────────────────
        final_round = 3
        melchior_vote = votes2.get("melchior", votes1.get("melchior", votes.get("melchior", {})))
        prompts3 = self._build_round3_prompts(base_prompt, charts, votes2, melchior_vote, in_position)
        votes3 = self._run_agents_parallel(
            prompts3, charts, {a.name: "Read" for a in self._active_agents()}
        )
        self._save_round_votes(cycle_id, 3, votes3)
        all_votes = votes3
        decision_map = {k: v["decision"] for k, v in votes3.items()}
        consensus = self._consensus(decision_map)
        self._log_round(3, votes3, consensus)

        if consensus:
            return self._build_result(consensus, votes3, 4, "consensus")

        # ── No consensus → Melchior decides ──────────────────────────────────
        master_vote = votes3.get("melchior", melchior_vote)
        master_decision = master_vote.get("decision", "HOLD")
        logger.info(
            f"[MAGI] No consensus after Round 3 — Melchior master decision: {master_decision}"
        )
        return self._build_result(master_decision, votes3, 4, "master")

    def _build_result(
        self,
        decision: str,
        votes: dict[str, dict],
        rounds_used: int,
        adopted_by: str,
    ) -> dict:
        # Use Melchior's reasoning as the canonical reason
        melchior_vote = votes.get("melchior", {})
        reasoning = melchior_vote.get("reasoning", "")
        return {
            "decision": decision,
            "reasoning": reasoning,
            "rounds": rounds_used,
            "votes": votes,
            "adopted_by": adopted_by,
        }
