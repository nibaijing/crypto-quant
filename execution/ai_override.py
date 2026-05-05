#!/usr/bin/env python3
"""
DecisionEngine — AI 决策层 (取代 ai_override.py)

架构:
  Strategy Layer → SignalReport → DecisionEngine → FinalDecision → Execution

职责:
  1. 接收策略层的 SignalReport (含条件得分 + 指标快照)
  2. 自主判断是否开仓/平仓 (不限于 approve/reject)
  3. 在 HOLD 信号时也可主动决策开仓 (当条件得分足够高时)
  4. 风控平仓 (exit_signal) 直接放行

调用策略:
  - 信号极清晰 (6/6 vs ≤1/6) → 自动放行, 不调 LLM
  - 信号模糊 (4~5/6)       → 调 LLM 二次判断
  - HOLD + 某方向 ≥4/6      → 调 LLM, 可能主动开仓
  - 每日限额 12 次          → 超限后仅自动放行
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

from execution.signals import SignalReport, FinalDecision

logger = logging.getLogger("CryptoQuant.DecisionEngine")

PROJECT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT / "data" / "decision_engine.log"
STATE_FILE = PROJECT / "data" / "decision_engine_state.json"

# ── 配置 ────────────────────────────────────────────────────────────────
LLM_TIMEOUT_SECONDS = 12
MAX_DAILY_LLM_CALLS = 12
COOLDOWN_SECONDS = 900            # 同一决策类型15分钟冷却
DRY_RUN = os.getenv("AI_OVERRIDE_DRY_RUN", "").lower() in ("1", "true", "yes")

# 自动放行阈值 — 策略信号满足度达到此值, 不调 LLM
AUTO_CLEAR_LONG_THRESHOLD = 0.83   # 5/6
AUTO_CLEAR_SHORT_THRESHOLD = 0.83

# AI 主动介入阈值 — HOLD 时某方向达到此值, 调 LLM
AI_INTERVENE_THRESHOLD = 0.67     # 4/6

# LLM API
LLM_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
LLM_MODEL = os.getenv("AI_OVERRIDE_MODEL", "deepseek-v4-flash")
LLM_API_BASE = os.getenv("AI_OVERRIDE_API_BASE", "https://api.deepseek.com/v1")


def _ensure_log():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.write_text("")

def _log(level: str, msg: str):
    _ensure_log()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {level:5s} | {msg}"
    getattr(logger, level if hasattr(logger, level) else "info")(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {
        "daily_calls": 0,
        "reset_date": datetime.now().strftime("%Y-%m-%d"),
        "cooldowns": {},
        "total_decisions": 0,
        "ai_decisions": 0,
        "auto_decisions": 0,
    }

def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


class DecisionEngine:
    """AI 决策层 — 从策略信号到最终执行决策"""

    def __init__(self):
        self.state = _load_state()
        self._reset_daily_if_needed()

    def _reset_daily_if_needed(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if not self.state.get("reset_date"):
            self.state["reset_date"] = today
            _save_state(self.state)
            return
        if self.state.get("reset_date") != today:
            self.state["daily_calls"] = 0
            self.state["reset_date"] = today
            self.state["cooldowns"] = {}
            _save_state(self.state)
            _log("info", "每日决策状态重置")

    # ── 主入口 ──────────────────────────────────────────────────────────

    def decide(self, report: SignalReport) -> FinalDecision:
        """
        主决策入口。

        Parameters
        ----------
        report : SignalReport
            策略层输出的结构化信号报告

        Returns
        -------
        FinalDecision
        """
        self._reset_daily_if_needed()

        # 1. 平仓信号 — 送 AI 二次确认 (AI 可 override 为 HOLD, 失败则执行原平仓)
        if report.exit_signal:
            if self._should_call_llm(report):
                return self._call_llm_decide(report, is_exit=True)
            # 配额耗尽 → 直接执行平仓
            _log("info", f"🚨 平仓(配额耗尽): {report.exit_signal} | price=${report.price:,.0f}")
            return FinalDecision(
                action=report.exit_signal,
                source="auto_clear",
                reasoning=f"Daily LLM quota exhausted, executing exit: {report.exit_signal}",
                confidence=1.0,
            )

        # 2. 冷却期 — 不放行 (除非 AI 判断极端行情)
        if report.is_cooldown:
            return FinalDecision(
                action="HOLD",
                source="auto_clear",
                reasoning="Cooldown period active",
                confidence=1.0,
            )

        # 3. 信号极清晰 → 自动放行
        auto = self._auto_clear(report)
        if auto:
            return auto

        # 4. 信号模糊 或 HOLD + 高分 → 调 LLM
        if self._should_call_llm(report):
            return self._call_llm_decide(report)

        # 5. 信号弱 → HOLD
        return FinalDecision(
            action="HOLD",
            source="auto_clear",
            reasoning="Strategy conditions insufficient, no AI intervention threshold met",
            confidence=0.9,
        )

    # ── 自动放行 ────────────────────────────────────────────────────────

    def _auto_clear(self, report: SignalReport) -> Optional[FinalDecision]:
        """信号极清晰时自动放行, 跳过 LLM。

        LONG: long_score ≥ 5/6 AND short_score ≤ 1/6 → auto LONG
        SHORT: short_score ≥ 5/6 AND long_score ≤ 1/6 → auto SHORT
        """
        # LONG 极强
        if (report.long_score >= AUTO_CLEAR_LONG_THRESHOLD
                and report.short_score <= 0.17
                and report.raw_signal == "LONG"):
            _log("info", f"⚡ 自动放行 LONG | score={report.long_score:.0%} | ${report.price:,.0f}")
            return FinalDecision(
                action="LONG",
                source="auto_clear",
                reasoning=f"Clear LONG signal: {report.long_score:.0%} conditions met, SHORT only {report.short_score:.0%}",
                confidence=report.long_score,
            )

        # SHORT 极强
        if (report.short_score >= AUTO_CLEAR_SHORT_THRESHOLD
                and report.long_score <= 0.17
                and report.raw_signal == "SHORT"):
            _log("info", f"⚡ 自动放行 SHORT | score={report.short_score:.0%} | ${report.price:,.0f}")
            return FinalDecision(
                action="SHORT",
                source="auto_clear",
                reasoning=f"Clear SHORT signal: {report.short_score:.0%} conditions met, LONG only {report.long_score:.0%}",
                confidence=report.short_score,
            )

        return None

    # ── AI 调判断 ────────────────────────────────────────────────────────

    def _should_call_llm(self, report: SignalReport) -> bool:
        """判断是否需要调 LLM。

        触发条件:
        - 平仓信号 (exit_signal) 始终触发 (受配额限制)
        - raw_signal 为非 HOLD 且 long_score/short_score 在 3~5/6 之间
        - raw_signal 为 HOLD 但某方向 ≥ 4/6
        - 每日限额未超
        """
        if self.state["daily_calls"] >= MAX_DAILY_LLM_CALLS:
            return False

        if DRY_RUN:
            return False

        # 平仓信号 → 始终调 LLM
        if report.exit_signal:
            return True

        # 已有持仓且无平仓信号 → 跳过 LLM (避免浪费配额)
        if report.current_position:
            return False

        # 非 HOLD 信号: 分数不够自动放行 → 调 LLM
        if report.raw_signal in ("LONG", "SHORT"):
            score = report.long_score if report.raw_signal == "LONG" else report.short_score
            if 0.33 <= score < AUTO_CLEAR_LONG_THRESHOLD:
                return True

        # HOLD 信号: 某方向 ≥ 4/6 → AI 可能主动开仓
        if report.raw_signal == "HOLD":
            if report.long_score >= AI_INTERVENE_THRESHOLD or report.short_score >= AI_INTERVENE_THRESHOLD:
                return True

        return False

    def _call_llm_decide(self, report: SignalReport, is_exit: bool = False) -> FinalDecision:
        """调 LLM 做最终决策。

        Parameters
        ----------
        is_exit : bool
            True=平仓场景, prompt 不同, fallback 为执行原平仓而非 HOLD

        返回 FinalDecision with source='ai_decision'
        失败时: 平仓场景 fallback 到原 exit_signal; 开仓场景 fallback HOLD
        """
        if is_exit:
            reasons = [f"exit_signal.{report.exit_signal}"]
            prompt = self._build_exit_prompt(report, reasons)
        else:
            reasons = self._get_trigger_reasons(report)
            prompt = self._build_decision_prompt(report, reasons)

        _log("info", f"{'🚨' if is_exit else '🤖'} AI决策触发: {reasons} | ${report.price:,.0f} "
              f"LONG={report.long_score:.0%} SHORT={report.short_score:.0%}")

        result = self._invoke_llm(prompt)

        if result is None:
            if is_exit:
                _log("warn", f"LLM 调用失败, 执行原平仓: {report.exit_signal}")
                return FinalDecision(action=report.exit_signal or "SELL",
                                     source="auto_clear",
                                     reasoning=f"LLM failed, fallback to exit: {report.exit_signal}",
                                     confidence=1.0)
            _log("warn", "LLM 调用失败, fallback HOLD")
            return FinalDecision(action="HOLD", source="auto_clear",
                                reasoning="LLM call failed, fallback to HOLD", confidence=0.5)

        # 更新状态
        self.state["daily_calls"] += 1
        self.state["ai_decisions"] = self.state.get("ai_decisions", 0) + 1
        self.state["total_decisions"] = self.state.get("total_decisions", 0) + 1
        _save_state(self.state)

        action = result.get("action", "HOLD")
        reasoning = result.get("reasoning", "AI decision")
        confidence = result.get("confidence", 0.5)

        # 平仓场景: AI 可输出 SELL/COVER(确认平仓) 或 HOLD(驳回), 不允许 LONG/SHORT
        if is_exit:
            if action not in ("SELL", "COVER", "HOLD"):
                action = report.exit_signal or "SELL"
        else:
            if action not in ("LONG", "SHORT", "HOLD"):
                action = "HOLD"

        emoji = "🚨" if is_exit and action != "HOLD" else ("✅" if action != "HOLD" else "⏸️")
        _log("info", f"{emoji} AI决策: {action} | "
              f"confidence={confidence:.0%} | {reasoning[:80]}")

        return FinalDecision(
            action=action,
            source="ai_decision",
            reasoning=reasoning,
            confidence=confidence,
        )

    def _get_trigger_reasons(self, report: SignalReport) -> List[str]:
        reasons = []
        if report.raw_signal in ("LONG", "SHORT"):
            score = report.long_score if report.raw_signal == "LONG" else report.short_score
            reasons.append(f"borderline_{report.raw_signal.lower()}({score:.0%})")
        if report.raw_signal == "HOLD":
            if report.long_score >= AI_INTERVENE_THRESHOLD:
                reasons.append(f"hold_but_long_viable({report.long_score:.0%})")
            if report.short_score >= AI_INTERVENE_THRESHOLD:
                reasons.append(f"hold_but_short_viable({report.short_score:.0%})")
        if report.lgb_opinion == "no_opinion":
            reasons.append("lgb_uncertain")
        return reasons

    # ── Prompt 构建 ──────────────────────────────────────────────────────

    def _build_decision_prompt(self, report: SignalReport, reasons: List[str]) -> str:
        """构建 AI 决策提示词。

        核心思路: 给 AI 完整的市场快照 + 策略评估, 让它做交易员式的综合判断。
        """
        # 基础上下文
        parts = [
            "You are the AI Decision Layer for a crypto quantitative trading system (BTC-USDT perpetual futures, 15-min bars).",
            "Strategy layer provides technical indicators and condition scores. You make the FINAL call.",
            "",
            report.to_ai_context(),
            "",
            "## Rules",
            "- You can OVERRIDE the strategy: if strategy says HOLD but conditions look good, you can say LONG/SHORT.",
            "- Conversely, if strategy says LONG/SHORT but market looks risky, say HOLD.",
            "- Consider RSI extremes: >75 is overbought (risky to long), <25 is oversold (risky to short).",
            "- Consider ADX: <25 means ranging/choppy (avoid trading), >35 means trending (trade with trend).",
            "- Consider price context: is this a breakout or a fakeout?",
            f"- Trigger reason: {', '.join(reasons)}",
            "",
            "Reply format (choose ONE):",
            "LONG",
            "SHORT",
            "HOLD",
            "",
            "You may append a one-line reasoning after the decision word.",
            "Example: LONG | Strong bullish alignment with volume confirmation, RSI cooling from peak",
            "",
            "Decision:",
        ]
        return "\n".join(parts)

    def _build_exit_prompt(self, report: SignalReport, reasons: List[str]) -> str:
        """构建平仓决策提示词 — AI 确认或驳回平仓信号。"""
        exit_type = report.exit_signal or "SELL"
        exit_desc = {"SELL": "平多仓 (close long)", "COVER": "平空仓 (close short)"}.get(exit_type, exit_type)

        parts = [
            "You are the AI Decision Layer for a crypto quantitative trading system (BTC-USDT perpetual futures, 15-min bars).",
            f"⚠️ EXIT SCENARIO: Strategy layer wants to {exit_desc}.",
            "Your job: confirm the exit or override it (keep the position).",
            "Only override if you have HIGH conviction the position will recover/improve.",
            "When in doubt, respect the strategy exit — safety first.",
            "",
            report.to_ai_context(),
            "",
            "## Exit Context",  
            f"- Exit Type: {exit_desc}",
            f"- Trigger: {', '.join(reasons)}",
            f"- LONG Score: {report.long_score:.0%} | SHORT Score: {report.short_score:.0%}",
            "",
            "## Rules",  
            "- Say SELL (for long exit) or COVER (for short exit) to confirm the strategy exit.",
            "- Say HOLD to REJECT the exit — only if you are highly confident the position will be profitable.",
            "- Consider: Is this a real reversal or a temporary pullback? Is volume confirming?",  
            "- Overriding an exit is RISKY. Default to confirming unless evidence is strong.",
            "- RSI extreme + ADX confirming trend → likely real exit. RSI mild + low ADX → possible fakeout.",
            "",  
            "Reply format (choose ONE):",  
            f"{exit_type}  (confirm exit)",
            "HOLD    (reject exit, keep position)",  
            "",  
            "One-line reasoning after the decision.",  
            f"Example: {exit_type} | RSI overbought at 78 with ADX 38 confirming trend exhaustion",  
            "",  
            "Decision:",
        ]
        return "\n".join(parts)

    # ── LLM 调用 ────────────────────────────────────────────────────────

    def _invoke_llm(self, prompt: str) -> Optional[dict]:
        """调 LLM API, 返回解析后的决策 dict。

        Returns
        -------
        dict or None
            {"action": "LONG", "reasoning": "...", "confidence": 0.8}
        """
        # 策略1: OpenAI-compatible API
        if LLM_API_KEY:
            result = self._call_openai_api(prompt)
            if result:
                return result

        # 策略2: hermes CLI (备选)
        result = self._call_hermes_cli(prompt)
        if result:
            return result

        return None

    def _call_openai_api(self, prompt: str) -> Optional[dict]:
        payload = json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 200,
            "temperature": 0.2,
        })

        try:
            proc = subprocess.run(
                ["curl", "-s", "--max-time", str(LLM_TIMEOUT_SECONDS),
                 f"{LLM_API_BASE}/chat/completions",
                 "-H", f"Authorization: Bearer {LLM_API_KEY}",
                 "-H", "Content-Type: application/json",
                 "-d", payload],
                capture_output=True, text=True,
                timeout=LLM_TIMEOUT_SECONDS + 3,
            )
            if proc.returncode != 0:
                _log("warn", f"curl failed: rc={proc.returncode}")
                return None

            data = json.loads(proc.stdout)
            msg = data.get("choices", [{}])[0].get("message", {})
            content = msg.get("content", "").strip()
            # 推理模型 (v4-flash/v4-pro) 可能把输出放 reasoning_content
            if not content:
                content = msg.get("reasoning_content", "").strip()
            _log("debug", f"LLM raw response: {content[:200]}")
            return self._parse_decision(content)

        except subprocess.TimeoutExpired:
            _log("warn", f"API timeout ({LLM_TIMEOUT_SECONDS}s)")
        except json.JSONDecodeError as e:
            _log("warn", f"API response not valid JSON: {e}")
        except Exception as e:
            _log("error", f"API exception: {e}")

        return None

    def _call_hermes_cli(self, prompt: str) -> Optional[dict]:
        try:
            result = subprocess.run(
                ["hermes", "chat", "-q", prompt, "-Q"],
                capture_output=True, text=True,
                timeout=LLM_TIMEOUT_SECONDS + 5,
                env={**os.environ, "HERMES_MAX_TURNS": "1"},
                cwd=str(PROJECT),
            )
            if result.returncode != 0:
                return None
            return self._parse_decision(result.stdout.strip())
        except Exception as e:
            _log("warn", f"hermes CLI failed: {e}")
        return None

    def _parse_decision(self, text: str) -> Optional[dict]:
        """从 LLM 输出中提取决策。"""
        text_clean = text.strip()

        # 提取第一行
        first_line = text_clean.split("\n")[0].strip().upper()

        action = "HOLD"
        if re.search(r'\bLONG\b', first_line):
            action = "LONG"
        elif re.search(r'\bSHORT\b', first_line):
            action = "SHORT"

        # 提取推理 (第一行中 | 后面的部分)
        reasoning = ""
        if "|" in text_clean.split("\n")[0]:
            reasoning = text_clean.split("\n")[0].split("|", 1)[1].strip()
        elif len(text_clean.split("\n")) > 1:
            reasoning = text_clean.split("\n")[1].strip()[:150]
        else:
            reasoning = "No reasoning provided"

        # 置信度 (如果有)
        confidence = 0.7
        conf_match = re.search(r'confidence[=:]\s*([\d.]+)', text_clean, re.IGNORECASE)
        if conf_match:
            confidence = float(conf_match.group(1))

        return {"action": action, "reasoning": reasoning[:200], "confidence": confidence}

    # ── 状态管理 ────────────────────────────────────────────────────────

    def update_trade_result(self, pnl: float):
        """交易结束后更新状态 (保留兼容)"""
        self.state["total_decisions"] = self.state.get("total_decisions", 0) + 1
        _save_state(self.state)

    def get_stats(self) -> dict:
        return {
            "daily_calls": self.state.get("daily_calls", 0),
            "max_daily_calls": MAX_DAILY_LLM_CALLS,
            "ai_decisions": self.state.get("ai_decisions", 0),
            "auto_decisions": self.state.get("auto_decisions", 0),
            "total_decisions": self.state.get("total_decisions", 0),
        }


# ── 全局单例 ────────────────────────────────────────────────────────────

_global_engine: Optional[DecisionEngine] = None


def get_decision_engine() -> DecisionEngine:
    global _global_engine
    if _global_engine is None:
        _global_engine = DecisionEngine()
    return _global_engine


# 保留旧接口兼容
def get_ai_override():
    return get_decision_engine()
