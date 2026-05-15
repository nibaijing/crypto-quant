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
from learning.trading_memory import TradingMemory

logger = logging.getLogger("CryptoQuant.DecisionEngine")

PROJECT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT / "data" / "decision_engine.log"
STATE_FILE = PROJECT / "data" / "decision_engine_state.json"

# ── 配置 ────────────────────────────────────────────────────────────────
LLM_TIMEOUT_SECONDS = 12
MAX_DAILY_LLM_CALLS = 9999   # 无限制 (原12, 用户要求日内不限配额)
COOLDOWN_SECONDS = 900            # 同一决策类型15分钟冷却
DRY_RUN = os.getenv("AI_OVERRIDE_DRY_RUN", "").lower() in ("1", "true", "yes")

# 自动放行阈值 — 策略信号满足度达到此值, 不调 LLM
AUTO_CLEAR_LONG_THRESHOLD = 0.75   # 4.5/6 (从0.83/5/6下调, 配合策略信号阈值)
AUTO_CLEAR_SHORT_THRESHOLD = 0.75

# AI 主动介入阈值 — HOLD 时某方向达到此值, 调 LLM
AI_INTERVENE_THRESHOLD = 0.60     # 与策略 SIGNAL_THRESHOLD 对齐 (从0.67下调)

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
    DEFAULT_STATE = {
        "daily_calls": 0,
        "reset_date": datetime.now().strftime("%Y-%m-%d"),
        "cooldowns": {},
        "total_decisions": 0,
        "ai_decisions": 0,
        "auto_decisions": 0,
        "fail_memory": None,
    }
    try:
        if STATE_FILE.exists():
            loaded = json.loads(STATE_FILE.read_text())
            if not isinstance(loaded, dict):
                return dict(DEFAULT_STATE)
            # Merge: fill missing keys with defaults (handles partial/empty states)
            for key, val in DEFAULT_STATE.items():
                loaded.setdefault(key, val)
            return loaded
    except Exception:
        pass
    return dict(DEFAULT_STATE)

def _save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


class DecisionEngine:
    """独立决策引擎 — 接收 SignalReport → 输出 FinalDecision。

    流程:
      1. 风控平仓 (exit_signal) → 直接放行
      2. 冷却期 → 不放行
      3. auto_clear → 策略信号直通 (raw_signal)
      4. 自动放行 (极强信号) → 不调 LLM
      5. LLM 调用 → 模糊信号/高分 HOLD
      6. 规则回退 → LLM 不可用时
    """

    def __init__(self, llm_api_key: str = ""):
        self._state_path = STATE_FILE
        self.state = _load_state()
        self._state_path.parent.mkdir(parents=True, exist_ok=True)

        self.llm_api_key = llm_api_key or LLM_API_KEY or ""
        self._memory = TradingMemory(self.state.get("fail_memory"))

        # 是否启用了轻量模式 (无 AI 配额限制)
        self.llm_available = self._is_llm_available()
        if self.llm_available:
            _log("info", f"🤖 LLM 可用 ({LLM_MODEL}) — 智能化决策已启用")
        else:
            _log("warn", "⚠️ LLM 不可用 — 仅规则自动放行")

    # ── 主入口 ────────────────────────────────────────────────────────────

    def decide(self, report: SignalReport) -> FinalDecision:
        """主决策入口 — if/else chain 代替 switch (Python 3.10- 兼容)。"""

        # 0. 风控平仓直接放行 (EXIT_ATR / EXIT_TIME)
        if report.exit_signal in ("EXIT_ATR", "EXIT_TIME"):
            action = "SELL" if report.exit_signal == "EXIT_ATR" or report.raw_signal in ("SELL", "SHORT") else "COVER"
            if report.raw_signal in ("SELL", "COVER"):
                action = report.raw_signal
            elif report.exit_signal == "EXIT_TIME":
                action = "SELL" if report.raw_signal in ("SELL", "SHORT") else "COVER"
            _log("info", f"🚨 风控平仓(直通): {action} | {report.exit_signal} | price=${report.price:,.0f}")
            return FinalDecision(
                action=action,
                source="risk_management",
                reasoning=f"Strategy exit: {report.exit_signal} (AI disabled)",
                confidence=1.0,
            )

        # 1. 冷却期 — 不放行
        if report.is_cooldown:
            return FinalDecision(
                action="HOLD", source="auto_clear",
                reasoning="Cooldown period active", confidence=1.0,
            )

        # 2. 开仓信号直通策略层
        if report.raw_signal == "LONG":
            _log("info", f"⚡ 策略开多(直通): score={report.long_score:.0%} | ${report.price:,.0f}")
            return FinalDecision(
                action="LONG", source="auto_clear",
                reasoning=f"Strategy LONG signal (AI disabled, {report.long_score:.0%}/{report.short_score:.0%})",
                confidence=report.long_score,
            )
        if report.raw_signal == "SHORT":
            _log("info", f"⚡ 策略开空(直通): score={report.short_score:.0%} | ${report.price:,.0f}")
            return FinalDecision(
                action="SHORT", source="auto_clear",
                reasoning=f"Strategy SHORT signal (AI disabled, {report.short_score:.0%}/{report.long_score:.0%})",
                confidence=report.short_score,
            )

        # 3. HOLD → HOLD
        return FinalDecision(
            action="HOLD", source="auto_clear",
            reasoning=f"Strategy HOLD (AI disabled, {report.long_score:.0%}/{report.short_score:.0%})",
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

    def _is_llm_available(self) -> bool:
        """检测 LLM 是否可用 (API key 或 hermes CLI)。"""
        if DRY_RUN:
            return False
        if LLM_API_KEY:
            return True
        # Check hermes CLI
        try:
            result = subprocess.run(["which", "hermes"], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                return True
        except Exception:
            pass
        return False

    def _rule_based_decide(self, report: SignalReport) -> Optional[FinalDecision]:
        """LLM 不可用时的规则回退决策。

        覆盖 _should_call_llm 的触发条件 (borderline 信号, HOLD+高分):
        - exit_signal → 已在外层处理 (直接执行)
        - HOLD 但某方向高分 → 开仓
        - 信号分数在边界 → 开仓
        """
        reason = None

        # 已有持仓 → 不干预
        if report.current_position:
            return None

        # HOLD 但 long_score 高分 → 开多
        if report.raw_signal == "HOLD":
            if report.long_score_w >= 0.50 and report.long_score_w > report.short_score_w:
                reason = f"Rule-based: HOLD but LONG weighted {report.long_score_w:.2f} > SHORT, score OK"
                _log("info", f"📐 {reason}")
                return FinalDecision(action="LONG", source="auto_clear", reasoning=reason, confidence=report.long_score_w)
            if report.short_score_w >= 0.50 and report.short_score_w > report.long_score_w:
                reason = f"Rule-based: HOLD but SHORT weighted {report.short_score_w:.2f} > LONG, score OK"
                _log("info", f"📐 {reason}")
                return FinalDecision(action="SHORT", source="auto_clear", reasoning=reason, confidence=report.short_score_w)

        # 非 HOLD 信号但不够 auto_clear → 按加权分决策
        if report.long_score_w >= 0.45 and report.long_score_w > report.short_score_w:
            reason = f"Rule-based: LONG weighted {report.long_score_w:.2f} > {report.short_score_w:.2f}"
            _log("info", f"📐 {reason}")
            return FinalDecision(action="LONG", source="auto_clear", reasoning=reason, confidence=report.long_score_w)
        if report.short_score_w >= 0.45 and report.short_score_w > report.long_score_w:
            reason = f"Rule-based: SHORT weighted {report.short_score_w:.2f} > {report.long_score_w:.2f}"
            _log("info", f"📐 {reason}")
            return FinalDecision(action="SHORT", source="auto_clear", reasoning=reason, confidence=report.short_score_w)

        return None

    # ── AI 调判断 ────────────────────────────────────────────────────────

    def _should_call_llm(self, report: SignalReport) -> bool:
        """判断是否需要调 LLM。

        触发条件:
        - 平仓信号 (exit_signal) 始终触发 (受配额限制)
        - raw_signal 为非 HOLD 且 long_score/short_score 在 3~5/6 之间
        - raw_signal 为 HOLD 但某方向 ≥ 4/6
        - 方向明确: LONG 和 SHORT 加权分差 > 0.25 (否则跳过避免 AI 猜方向)
        - 每日限额未超
        """
        if self.state["daily_calls"] >= MAX_DAILY_LLM_CALLS:
            return False

        # 平仓信号 → 始终调 LLM
        if report.exit_signal:
            return True

        # 已有持仓且无平仓信号 → 跳过 LLM (避免浪费配额)
        if report.current_position:
            return False

        # 方向分歧过滤: LONG/SHORT 加权分差 < 0.25 时方向不明确, 跳过 AI 猜方向
        score_diff = abs(report.long_score_w - report.short_score_w)
        if score_diff < 0.25 and not report.exit_signal:
            _log("info", f"⏸️ 方向分歧,跳过AI: LONG={report.long_score:.0%} SHORT={report.short_score:.0%} diff={score_diff:.2f}")
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
                fallback_action = self._map_exit_to_action(report)
                _log("warn", f"LLM 调用失败, 执行平仓: {fallback_action}")
                return FinalDecision(action=fallback_action,
                                     source="auto_clear",
                                     reasoning=f"LLM failed, fallback to exit: {fallback_action}",
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
                action = self._map_exit_to_action(report)
        else:
            if action not in ("LONG", "SHORT", "HOLD"):
                action = "HOLD"

        if reasoning in (None, "", "No reasoning provided", "No reasoning provided "):
            reasoning = f"AI decision: {action}"

        _log("info", f"{'✅' if action != 'HOLD' else '⏸️'} AI决策: {action} | confidence={confidence:0.0%} | {reasoning[:80]}")
        return FinalDecision(action=action, source="ai_decision",
                            reasoning=reasoning, confidence=confidence)

    def _get_trigger_reasons(self, report: SignalReport) -> List[str]:
        """生成触发 AI 决策的原因列表。"""
        reasons = []
        if report.raw_signal in ("LONG", "SHORT"):
            reasons.append(f"{report.raw_signal}_signal({report.long_score:.0%}/{report.short_score:.0%})")
        if report.long_score >= AI_INTERVENE_THRESHOLD:
            reasons.append(f"hold_but_long_viable({report.long_score:.0%})")
        if report.short_score >= AI_INTERVENE_THRESHOLD:
            reasons.append(f"hold_but_short_viable({report.short_score:.0%})")
        # LGB
        if report.lgb_opinion and report.lgb_opinion.get("score", 0) > 0.5:
            reasons.append(f"lgb_{report.lgb_opinion.get('action', 'uncertain')}")
        return reasons if reasons else ["borderline_signal"]

    def _map_exit_to_action(self, report: SignalReport) -> str:
        """将 exit_signal 映射为交易动作。"""
        action_map = {
            "EXIT_RSI": "SELL" if report.raw_signal in ("SELL", None) else "COVER",
            "EXIT_TREND": "SELL" if report.raw_signal in ("SELL", None) else "COVER",
            "EXIT_TIME": "SELL" if report.raw_signal in ("SELL", None) else "COVER",
            "exit": "SELL" if report.raw_signal in ("SELL", None) else "COVER",
        }
        if report.raw_signal == "SELL":
            return "SELL"
        if report.raw_signal == "COVER":
            return "COVER"
        if report.exit_signal and report.exit_signal in action_map:
            return action_map[report.exit_signal]
        return "HOLD"

    def _build_decision_prompt(self, report: SignalReport, reasons: List[str]) -> str:
        """构建 LLM 开仓决策 prompt。"""
        bias_raw = report.factor_bias or {}
        bias_label = bias_raw.get("bias", "neutral")
        bias_conf = bias_raw.get("confidence", 0.0)

        position_ctx = ""
        if report.current_position:
            pos = report.current_position
            position_ctx = (
                f"Current position: {pos.get('side', '?')} | "
                f"Entry=${pos.get('entry_price', 0):,.0f} | "
                f"Size={pos.get('size', 0):.4f} BTC | "
                f"PnL={pos.get('unrealized_pnl', 0):+.2%}"
            )

        details = {
            "Price": f"${report.price:,.0f}",
            "Regime": report.regime,
            "Price_Trend_5bars": report.price_trend_5bars,
            "RSI": f"{report.rsi:.0f}({report.rsi_trend})",
            "ADX": f"{report.adx:.0f}({report.adx_trend})",
            "MACD_hist": f"{report.macd_hist:.0f}",
            "MA7": f"${report.ma7:,.0f}",
            "MA25": f"${report.ma25:,.0f}",
            "MA99": f"${report.ma99:,.0f}",
            "Volume_surge": report.volume_surge,
            "Factor_bias": f"{bias_label}(conf={bias_conf:.0%})",
        }
        if report.sentiment_score is not None:
            details["Sentiment"] = f"{report.sentiment_score:.3f}({report.sentiment_label}, conf={report.sentiment_confidence:.0%})"
        if report.polymarket_score is not None:
            details["Polymarket"] = f"{report.polymarket_score:.3f}({report.polymarket_label}, conf={report.polymarket_confidence:.0%})"

        long_conditions = " ".join(f"{k}={'✅' if v else '❌'}" for k, v in report.conditions_long.items() if k != 'VOL') + f" VOL={'✅' if report.conditions_long.get('VOL', False) else '❌'}"
        short_conditions = " ".join(f"{k}={'✅' if v else '❌'}" for k, v in report.conditions_short.items() if k != 'VOL') + f" VOL={'✅' if report.conditions_short.get('VOL', False) else '❌'}"
        cond_section = (
            f"BUY: score={report.long_score:.0%} weighted={report.long_score_w:.2f} | {long_conditions}\n"
            f"SELL: score={report.short_score:.0%} weighted={report.short_score_w:.2f} | {short_conditions}"
        )

        detail_lines = "\n".join(f"  {k}: {v}" for k, v in details.items())

        return f"""You are a crypto futures trading AI. Analyze this BTC-USDT 15m bar and decide.

{position_ctx}

SignalReport:
{cond_section}

Indicators:
{detail_lines}

Trigger reasons: {', '.join(reasons)}

Rules:
1. Output JSON: {{"action": "LONG"|"SHORT"|"HOLD", "reasoning": "why", "confidence": 0.0-1.0}}
2. LONG if strong uptrend + clear conditions
3. SHORT if clear downtrend + clear conditions
4. HOLD if mixed, choppy, or unsure — DO NOT force a direction
5. You may act on HOLD+high-score if the signal is clear enough
6. Factor bias at conf>0.8 is strong directional signal — respect it
7. CONSERVATIVE is better than wrong. When in doubt: HOLD."""

    def _build_exit_prompt(self, report: SignalReport, reasons: List[str]) -> str:
        """构建 LLM 平仓决策 prompt。

        平仓场景: AI 决定是否执行策略发出的平仓信号。
        亏损时 AI 可驳回 (HOLD), 盈利时默认执行。
        """
        pos = report.current_position or {}
        pos_entry = pos.get("entry_price", 0)
        pos_side = pos.get("side", "?")
        pos_bars = pos.get("bars_held", 0)
        pnl_pct = ((report.price - pos_entry) / pos_entry * 100) if pos_side == "long" else ((pos_entry - report.price) / pos_entry * 100)
        pnl_pct = round(pnl_pct, 2)

        bias_raw = report.factor_bias or {}
        bias_label = bias_raw.get("bias", "neutral")
        bias_conf = bias_raw.get("confidence", 0.0)

        details = {
            "Exit_signal": report.exit_signal,
            "Position": f"{pos_side} @ ${pos_entry:,.0f}, held {pos_bars} bars",
            "PnL": f"{pnl_pct:+.2f}%",
            "Price": f"${report.price:,.0f}",
            "RSI": f"{report.rsi:.0f}({report.rsi_trend})",
            "ADX": f"{report.adx:.0f}({report.adx_trend})",
            "MACD_hist": f"{report.macd_hist:.0f}",
            "Regime": report.regime,
            "Price_Trend": report.price_trend_5bars,
            "Factor_bias": f"{bias_label}(conf={bias_conf:.0%})",
        }
        if report.sentiment_score is not None:
            details["Sentiment"] = f"{report.sentiment_score:.3f}({report.sentiment_label})"
        if report.polymarket_score is not None:
            details["Polymarket"] = f"{report.polymarket_score:.3f}({report.polymarket_label})"

        long_conditions = " ".join(f"{k}={'✅' if v else '❌'}" for k, v in report.conditions_long.items())
        short_conditions = " ".join(f"{k}={'✅' if v else '❌'}" for k, v in report.conditions_short.items())

        detail_lines = "\n".join(f"  {k}: {v}" for k, v in details.items())

        return f"""You are a crypto futures exit advisor. Review this exit signal and decide.

Position: {pos_side} @ ${pos_entry:,.0f}, PnL={pnl_pct:+.2f}%
Exit signal: {report.exit_signal} (reason from strategy)

BUY conditions: {report.long_score:.0%} | {long_conditions}
SELL conditions: {report.short_score:.0%} | {short_conditions}

{detail_lines}

Trigger: {', '.join(reasons)}

Rules:
1. Output JSON: {{"action": "SELL"|"COVER"|"HOLD", "reasoning": "why", "confidence": 0.0-1.0}}
2. Default: EXIT (SELL for long, COVER for short)
3. HOLD to reject the exit only if markets clearly reversed back in position's favor
4. When losing: you may reject (HOLD) if the exit signal seems premature and indicators support holding
5. When winning (>3%): strongly lean toward profit-taking
6. CONSERVATIVE: if in doubt, exit. Protecting capital > catching last penny.
7. Be pragmatic — small repeated losses are better than one big loss."""

    def _invoke_llm(self, prompt: str) -> Optional[Dict[str, Any]]:
        """调 LLM API 返回 JSON 决策。"""
        if not self.llm_available:
            _log("warn", "LLM 不可用，跳过 LLM 调用")
            return None

        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 300,
        }
        headers = {
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        }
        import urllib.request
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        data = json.dumps(payload).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"{LLM_API_BASE}/chat/completions",
                data=data, headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, context=ctx, timeout=LLM_TIMEOUT_SECONDS) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            _log("debug", f"LLM raw: {content[:200]}")
            return self._parse_llm_output(content)
        except Exception as e:
            _log("warn", f"LLM 调用异常: {e}")
            return None

    def _parse_llm_output(self, content: str) -> Optional[Dict[str, Any]]:
        """从 LLM 输出解析 JSON。"""
        # 尝试从 ```json ... ``` 中提取
        match = re.search(r'```(?:json)?\s*({.*?})\s*```', content, re.DOTALL)
        if match:
            content = match.group(1)
        # 或直接找第一个 { }
        for brace in re.finditer(r'\{[^}]*\}', content):
            try:
                parsed = json.loads(brace.group())
                if isinstance(parsed, dict) and "action" in parsed:
                    parsed["action"] = parsed["action"].upper()
                    return parsed
            except json.JSONDecodeError:
                continue
        return None

    # ── 记忆 ──────────────────────────────────────────────────────────────

    def update_memory(self, decision, result):
        """更新交易记忆。"""
        if self._memory:
            try:
                self._memory.record(decision, result)
                self.state["fail_memory"] = self._memory.summary()
            except Exception:
                pass

    def summarize(self) -> str:
        """返回运行摘要。"""
        total = self.state.get("total_decisions", 0)
        ai_dec = self.state.get("ai_decisions", 0)
        auto_dec = self.state.get("auto_decisions", 0)
        calls = self.state.get("daily_calls", 0)
        return (
            f"DecisionEngine: {total} total | {ai_dec} AI | {auto_dec} auto | "
            f"{calls} LLM calls today"
        )


# ── 快捷函数 ──────────────────────────────────────────────────────────────

def get_decision_engine() -> DecisionEngine:
    """获取全局 DecisionEngine 单例。"""
    if not hasattr(get_decision_engine, '_instance'):
        get_decision_engine._instance = DecisionEngine()
    return get_decision_engine._instance


def make_decision(report: SignalReport, engine: Optional[DecisionEngine] = None) -> FinalDecision:
    """便捷入口 — 快速决策。"""
    if engine is None:
        engine = DecisionEngine()
    return engine.decide(report)