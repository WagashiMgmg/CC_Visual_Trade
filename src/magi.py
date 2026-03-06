"""
MAGI System — EVA-inspired multi-agent voting for trade decisions.

Melchior  = Claude Code CLI   (master agent)
Balthazar = Gemini CLI
Caspar    = Codex CLI (planned) — 一時的に Claude Haiku で代替実装中

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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from src.database import MagiVote, get_session

logger = logging.getLogger(__name__)

_DECISION_RE = re.compile(r"\*{0,2}DECISION:?\*{0,2}:?\s*(LONG|SHORT|HOLD|EXIT)", re.IGNORECASE)
_REASON_RE   = re.compile(r"\*{0,2}REASON\*{0,2}:?\s*(.+?)(?:\n\n|\Z)", re.DOTALL)


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

    def _save_vote(self, cycle_id: int, round_num: int, vote: dict, timestamp: datetime | None = None):
        with get_session() as session:
            session.add(MagiVote(
                cycle_id=cycle_id,
                agent_name=self.name,
                round=round_num,
                decision=vote["decision"],
                reasoning=vote.get("reasoning", ""),
                raw_output=(vote.get("raw_output", "") or "")[:5000],
                timestamp=timestamp or datetime.utcnow(),
            ))
            session.commit()


# ── Melchior (Claude Code CLI) ────────────────────────────────────────────────

class ClaudeAgent(MagiAgent):
    name    = "melchior"
    display = "Melchior"

    def _check_available_quota(self) -> bool:
        """Return True if claude CLI is reachable."""
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True, timeout=15,
            )
            return result.returncode == 0
        except Exception:
            return False

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
            if result.stderr:
                logger.debug(f"[Melchior] stderr: {result.stderr[:200]}")
            if result.returncode != 0:
                logger.error(f"[Melchior] non-zero exit ({result.returncode}) — marking OFFLINE")
                self.available = False
                return None
            if not _DECISION_RE.search(result.stdout):
                logger.warning("[Melchior] no explicit DECISION in output — abstaining")
                return None
            return _parse_vote(result.stdout)
        except subprocess.TimeoutExpired:
            logger.error("[Melchior] timed out — marking OFFLINE")
            self.available = False
            return None
        except FileNotFoundError:
            logger.error("[Melchior] claude CLI not found — marking OFFLINE")
            self.available = False
            return None
        except Exception as e:
            logger.error(f"[Melchior] error: {e} — marking OFFLINE")
            self.available = False
            return None


# ── Balthazar (Gemini CLI) ────────────────────────────────────────────────────

class GeminiAgent(MagiAgent):
    name    = "balthazar"
    display = "Balthazar"

    # Model fallback order: default (flash) → flash-lite → pro
    _MODEL_FALLBACK = ["gemini-3.1-pro-preview", "gemini-3-flash-preview", "gemini-2.5-pro", "gemini-2.5-flash-lite", "gemini-2.5-flash"]

    def _check_available_quota(self) -> bool:
        """Return True if at least one model has quota remaining."""
        for model in self._MODEL_FALLBACK:
            _, quota_exceeded = self._run_gemini("ping", model)
            if not quota_exceeded:
                return True
        return False

    def analyze(self, prompt: str, charts: list[str], allowed_tools: str = "Read") -> dict | None:
        # Re-check availability each cycle (quota may have reset)
        if not self.available:
            if not self._check_available_quota():
                logger.info("[Balthazar] still OFFLINE — abstaining")
                return None
            self.available = True
            logger.info("[Balthazar] quota restored, back ONLINE")
        chart_refs = " ".join(f"@{p}" for p in charts)
        full_prompt = f"{prompt}\n\nCharts: {chart_refs}" if charts else prompt

        for model in self._MODEL_FALLBACK:
            output, quota_exceeded = self._run_gemini(full_prompt, model)
            if output is not None:
                if not _DECISION_RE.search(output):
                    logger.warning(f"[Balthazar] no explicit DECISION in output ({model}) — abstaining")
                    return None
                return _parse_vote(output)
            if not quota_exceeded:
                # Non-quota error (@ syntax, etc.) — retry text-only once
                logger.warning("[Balthazar] @ syntax failed, retrying text-only")
                output, quota_exceeded = self._run_gemini(prompt, model)
                if output is not None:
                    if not _DECISION_RE.search(output):
                        logger.warning("[Balthazar] no explicit DECISION in text-only retry — abstaining")
                        return None
                    return _parse_vote(output)
                if not quota_exceeded:
                    break  # Non-quota failure, skip remaining models
            label = model or "default"
            logger.warning(f"[Balthazar] quota exceeded on {label}, trying next model")

        # All models exhausted → abstain (return None, not counted in majority)
        logger.error("[Balthazar] all models quota-exceeded — marking OFFLINE")
        self.available = False
        return None

    def _run_gemini(self, prompt: str, model: str | None = None) -> tuple[str | None, bool]:
        """
        Run gemini CLI with optional model override.
        Returns (output, quota_exceeded).
        quota_exceeded=True means 429 TerminalQuotaError.
        """
        cmd = ["gemini"]
        if model:
            cmd += ["-m", model]
        cmd += ["-p", prompt]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                cwd="/app",
                env=os.environ.copy(),
            )
            if result.returncode != 0:
                stderr = result.stderr
                if "TerminalQuotaError" in stderr or "exhausted your capacity" in stderr:
                    return None, True
                logger.warning(f"[Balthazar] non-zero exit ({model or 'default'}): {stderr[:200]}")
                return None, False
            return result.stdout, False
        except subprocess.TimeoutExpired:
            logger.error(f"[Balthazar] timed out ({model or 'default'})")
            return None, False
        except FileNotFoundError:
            logger.warning("[Balthazar] gemini CLI not found — marking unavailable")
            self.available = False
            return None, False
        except Exception as e:
            logger.error(f"[Balthazar] error ({model or 'default'}): {e}")
            return None, False


# ── Caspar (Codex CLI — 一時的に Claude Haiku で代替) ────────────────────────
#
# 本来は OpenAI Codex CLI を使用予定。
# Codex CLI が利用可能になったら以下の TODO を実装して切り替える:
#
#   TODO(codex): _BACKEND を "codex" に変更し、_run_codex() を有効化する。
#                Claude Haiku 関連のコード (_run_claude_haiku) は削除可。
#
# ダッシュボード表示名は "Codex" のまま維持する（display フィールドは変更不要）。

class CasparAgent(MagiAgent):
    name      = "caspar"
    display   = "Caspar"  # ダッシュボードでは "Codex" と表示される（index.html 参照）
    available = True

    # 切り替えポイント: "haiku"（現在）→ "codex"（Codex CLI 利用可能後）
    _BACKEND = "haiku"

    # Claude Haiku モデルID（一時的な代替）
    _HAIKU_MODEL = "claude-haiku-4-5-20251001"

    def analyze(self, prompt: str, charts: list[str], allowed_tools: str = "Read") -> dict | None:
        if self._BACKEND == "codex":
            # TODO(codex): Codex CLI が利用可能になったら実装する
            return self._run_codex(prompt, charts)
        return self._run_claude_haiku(prompt, charts, allowed_tools)

    def _run_claude_haiku(self, prompt: str, charts: list[str], allowed_tools: str) -> dict | None:
        """一時的な代替: Claude Haiku を claude CLI の --model で呼び出す。"""
        try:
            result = subprocess.run(
                [
                    "claude",
                    "-p", prompt,
                    "--model", self._HAIKU_MODEL,
                    "--allowedTools", allowed_tools,
                    "--permission-mode", "bypassPermissions",
                ],
                capture_output=True,
                text=True,
                timeout=300,
                cwd="/app",
                env=os.environ.copy(),
            )
            if result.stderr:
                logger.debug(f"[Caspar/Haiku] stderr: {result.stderr[:200]}")
            if result.returncode != 0:
                logger.error(f"[Caspar/Haiku] non-zero exit ({result.returncode}) — marking OFFLINE")
                self.available = False
                return None
            if not _DECISION_RE.search(result.stdout):
                logger.warning("[Caspar/Haiku] no explicit DECISION in output — abstaining")
                return None
            return _parse_vote(result.stdout)
        except subprocess.TimeoutExpired:
            logger.error("[Caspar/Haiku] timed out — marking OFFLINE")
            self.available = False
            return None
        except FileNotFoundError:
            logger.error("[Caspar/Haiku] claude CLI not found — marking OFFLINE")
            self.available = False
            return None
        except Exception as e:
            logger.error(f"[Caspar/Haiku] error: {e} — marking OFFLINE")
            self.available = False
            return None

    def _run_codex(self, prompt: str, charts: list[str]) -> dict | None:
        """TODO(codex): Codex CLI 実装。_BACKEND = "codex" に切り替え後に完成させる。"""
        # 例: subprocess.run(["codex", "-p", prompt, ...], ...)
        raise NotImplementedError("Codex CLI 未実装")


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
        """Return consensus decision or None if no majority.
        Threshold is based on actual voters (abstentions not counted)."""
        from collections import Counter
        if not votes:
            return None
        counts = Counter(votes.values())
        threshold = len(votes) // 2 + 1
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
        """Run active agents concurrently; return {agent_name: vote}.
        Each vote includes 'completed_at' with actual completion timestamp."""
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
                completed_at = datetime.utcnow()
                try:
                    vote = future.result()
                    if vote:
                        vote["completed_at"] = completed_at
                        results[agent.name] = vote
                    # None → abstain (not counted in majority)
                except Exception as e:
                    logger.error(f"[MAGI] {agent.name} future error: {e}")
                    # Exception → also abstain

        return results

    def _save_round_votes(self, cycle_id: int, round_num: int, votes: dict[str, dict]):
        for agent_name, vote in votes.items():
            agent = next((a for a in self.agents if a.name == agent_name), None)
            if agent:
                agent._save_vote(cycle_id, round_num, vote, timestamp=vote.get("completed_at"))

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
        chart_fn: Callable[[], list[str]] | None = None,
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
        # ── Revive inactive agents (quota may have reset) ────────────────────
        for agent in self.agents:
            if not agent.available and hasattr(agent, "_check_available_quota"):
                if agent._check_available_quota():
                    agent.available = True
                    logger.info(f"[MAGI] {agent.display} quota restored, back ONLINE")

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
        if chart_fn:
            charts = chart_fn()
            logger.info("[MAGI] Round 1: charts refreshed")
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
        if chart_fn:
            charts = chart_fn()
            logger.info("[MAGI] Round 2: charts refreshed")
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
        if chart_fn:
            charts = chart_fn()
            logger.info("[MAGI] Round 3: charts refreshed")
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
        return {
            "decision": decision,
            "reasoning": "",  # Will be filled by synthesize_reasoning()
            "rounds": rounds_used,
            "votes": votes,
            "adopted_by": adopted_by,
        }

    def synthesize_reasoning(
        self,
        decision: str,
        votes: dict[str, dict],
    ) -> str:
        """Have Melchior synthesize a unified reasoning from all anonymous votes.

        Agents are anonymized (Agent A/B/C) to prevent Melchior from
        favouring its own prior opinion.  The output summarises pro and
        con arguments for the decided direction.
        """
        # Anonymize agent names
        agent_names = sorted(votes.keys())
        anon_map = {name: f"Agent {chr(65 + i)}" for i, name in enumerate(agent_names)}

        # Build anonymous vote summaries
        vote_lines = []
        for name in agent_names:
            v = votes[name]
            anon = anon_map[name]
            vote_lines.append(
                f"- {anon}: {v['decision']}\n"
                f"  理由: {v.get('reasoning', '（なし）')[:300]}"
            )
        votes_text = "\n".join(vote_lines)

        prompt = (
            f"あなたはMAGIシステムの議事録係です。\n"
            f"以下の審議結果を統合し、簡潔な日本語のサマリーを作成してください。\n\n"
            f"## 審議結果\n"
            f"**最終決定: {decision}**\n\n"
            f"## 各エージェントの意見（匿名）\n"
            f"{votes_text}\n\n"
            f"## 出力フォーマット\n"
            f"以下の形式で出力してください（マークダウン不要、プレーンテキストで）:\n\n"
            f"【決定】{decision}\n"
            f"【賛成意見】決定方向（{decision}）を支持する根拠のサマリー（2-3文）\n"
            f"【反対意見】決定方向に反対した意見の根拠サマリー（該当があれば2-3文、なければ「全員一致」）\n"
        )

        try:
            result = subprocess.run(
                [
                    "claude",
                    "-p", prompt,
                    "--allowedTools", "",
                    "--permission-mode", "bypassPermissions",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                cwd="/app",
                env=os.environ.copy(),
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()[:2000]
            logger.warning(f"[MAGI] synthesize_reasoning failed (rc={result.returncode})")
        except Exception as e:
            logger.error(f"[MAGI] synthesize_reasoning error: {e}")

        # Fallback: simple concatenation of anonymous votes
        fallback_lines = []
        for name in agent_names:
            v = votes[name]
            anon = anon_map[name]
            fallback_lines.append(f"{anon}({v['decision']}): {v.get('reasoning', '')[:200]}")
        return f"【決定】{decision}\n" + "\n".join(fallback_lines)
