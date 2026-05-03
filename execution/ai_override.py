#!/usr/bin/env python3
"""
AIOverride — 模糊边界AI决策层

在规则信号触发但处于模糊场景时，调用LLM进行二次判断。
非每根K线都调用，仅在预设触发条件满足时介入。

触发条件:
  1. LGB 返回 no_opinion（规则有信号但ML不确定）
  2. 连亏2次后首次信号
  3. ADX 边缘 (20-25) 的震荡转趋势区域
  4. 每日前2次交易
  5. 平仓时 RSI 接近但未触及阈值

LLM 通过 hermes chat -q 非交互式调用，超时 10 秒降级到原规则。
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
from typing import Optional, Tuple, Dict, Any, List

logger = logging.getLogger("CryptoQuant.AIOverride")

PROJECT = Path(__file__).resolve().parent.parent
LOG_FILE = PROJECT / "data" / "ai_override.log"
STATE_FILE = PROJECT / "data" / "ai_override_state.json"

# ── 配置 ────────────────────────────────────────────────────────────────
LLM_TIMEOUT_SECONDS = 10          # LLM 调用超时
HERMES_BIN = "hermes"             # hermes CLI 路径 (备选)
COOLDOWN_SECONDS = 900            # 同一触发类型15分钟内不重复调
MAX_DAILY_LLM_CALLS = 12          # 每日最多LLM调用次数
DRY_RUN = os.getenv("AI_OVERRIDE_DRY_RUN", "").lower() in ("1", "true", "yes")

# LLM API 配置 (从 ~/.hermes/config.yaml 读取)
LLM_API_BASE = os.getenv(
    "AI_OVERRIDE_API_BASE",
    "https://integrate.api.nvidia.com/v1"
)
LLM_API_KEY = os.getenv("AI_OVERRIDE_API_KEY", os.getenv("NVIDIA_API_KEY", ""))
LLM_MODEL = os.getenv(
    "AI_OVERRIDE_MODEL",
    "deepseek-ai/deepseek-v4-pro"
)

# ── 触发条件阈值 ────────────────────────────────────────────────────────
ADX_EDGE_LOW = 20
ADX_EDGE_HIGH = 25
CONSECUTIVE_LOSSES_THRESHOLD = 2
DAILY_EARLY_TRADES = 2
RSI_EXIT_EDGE_MARGIN = 5          # RSI 离平仓阈值差 5 以内 → AI判断


def _ensure_log():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not LOG_FILE.exists():
        LOG_FILE.write_text("")


def _log(level: str, msg: str):
    _ensure_log()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {level:5s} | {msg}"
    logger.info(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _load_state() -> Dict[str, Any]:
    """加载状态: 调用计数、冷却时间、连亏次数"""
    try:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
    except Exception:
        pass
    return {
        "daily_calls": 0,
        "reset_date": datetime.now().strftime("%Y-%m-%d"),
        "cooldowns": {},      # {trigger_type: last_call_timestamp}
        "consecutive_losses": 0,
        "daily_trade_count": 0,
        "last_signal": None,
    }


def _save_state(state: Dict[str, Any]):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


class AIOverride:
    """AI 决策层，封装 LLM 调用逻辑。

    用法:
      ai = AIOverride()
      decision = ai.judge(signal_context)
      # → 'approve' | 'reject' | 'fallback'
    """

    def __init__(self):
        self.state = _load_state()
        self._reset_daily_if_needed()
        self.calls_saved = 0

    def _reset_daily_if_needed(self):
        today = datetime.now().strftime("%Y-%m-%d")
        # 首次运行时 reset_date 为空 → 初始化为今天，避免每次都重置
        if not self.state.get("reset_date"):
            self.state["reset_date"] = today
            _save_state(self.state)
            return
        if self.state.get("reset_date") != today:
            self.state["daily_calls"] = 0
            self.state["reset_date"] = today
            self.state["cooldowns"] = {}
            self.state["daily_trade_count"] = 0
            _save_state(self.state)
            _log("info", "每日状态重置")

    # ── 触发条件判断 ────────────────────────────────────────────────────

    def _should_trigger_lgb_edge(self, signal: str, lgb_opinion: str) -> bool:
        """LGB 双确认边缘: 规则有信号但LGB返回no_opinion"""
        return signal in ("LONG", "SHORT") and lgb_opinion == "no_opinion"

    def _should_trigger_after_losses(self) -> bool:
        """连亏2次后的首次信号"""
        return self.state.get("consecutive_losses", 0) >= CONSECUTIVE_LOSSES_THRESHOLD

    def _should_trigger_adx_edge(self, adx_val: float) -> bool:
        """ADX 边缘区域"""
        return ADX_EDGE_LOW <= adx_val <= ADX_EDGE_HIGH

    def _should_trigger_early_trade(self) -> bool:
        """每日前N笔交易"""
        return self.state.get("daily_trade_count", 0) < DAILY_EARLY_TRADES

    def _should_trigger_rsi_exit_edge(self, signal: str, rsi_val: float) -> bool:
        """平仓RSI接近阈值"""
        from strategies.spot.optimized_v6 import RSI_LONG_EXIT, RSI_SHORT_EXIT
        if signal == "SELL":   # 平多
            return RSI_LONG_EXIT - RSI_EXIT_EDGE_MARGIN <= rsi_val <= RSI_LONG_EXIT
        if signal == "COVER":  # 平空
            return RSI_SHORT_EXIT <= rsi_val <= RSI_SHORT_EXIT + RSI_EXIT_EDGE_MARGIN
        return False

    def _on_cooldown(self, trigger_type: str) -> bool:
        """特定触发类型是否在冷却中"""
        last = self.state["cooldowns"].get(trigger_type, 0)
        return (time.time() - last) < COOLDOWN_SECONDS

    def _set_cooldown(self, trigger_type: str):
        self.state["cooldowns"][trigger_type] = time.time()

    def _exceeded_daily_limit(self) -> bool:
        return self.state["daily_calls"] >= MAX_DAILY_LLM_CALLS

    # ── 核心判断逻辑 ────────────────────────────────────────────────────

    def judge(
        self,
        signal: str,
        lgb_opinion: str,
        bar_context: Dict[str, Any],
    ) -> str:
        """主入口：判断是否触发AI，如果需要则调用LLM。

        Parameters
        ----------
        signal : 原始 MATrend 信号 ('LONG'|'SHORT'|'SELL'|'COVER'|'HOLD')
        lgb_opinion : LGB 确认结果 ('agree'|'disagree'|'no_opinion')
        bar_context : 当前K线上下文
            {
                'close': float, 'rsi': float, 'adx': float,
                'macd_hist': float, 'ma7': float, 'ma25': float,
                'volatility': float, 'has_position': bool,
                'position_side': str, 'position_pnl': float,
                'bars_held': int,
            }

        Returns
        -------
        'approve'  — AI 同意执行
        'reject'   — AI 建议放弃
        'fallback' — 不触发AI，走原规则
        """
        if signal == "HOLD":
            return "fallback"

        self._reset_daily_if_needed()

        # 确定触发原因
        trigger_reasons: List[str] = []
        adx_val = bar_context.get("adx", 0)
        rsi_val = bar_context.get("rsi", 50)

        if self._should_trigger_lgb_edge(signal, lgb_opinion):
            trigger_reasons.append("lgb_edge")
        if self._should_trigger_after_losses():
            trigger_reasons.append("after_losses")
        if self._should_trigger_adx_edge(adx_val):
            trigger_reasons.append(f"adx_edge(adx={adx_val:.0f})")
        if self._should_trigger_early_trade():
            trigger_reasons.append("early_trade")
        if self._should_trigger_rsi_exit_edge(signal, rsi_val):
            trigger_reasons.append(f"rsi_exit_edge(rsi={rsi_val:.0f})")

        if not trigger_reasons:
            return "fallback"

        # 冷却检查
        active_reasons = [r for r in trigger_reasons if not self._on_cooldown(r)]
        if not active_reasons:
            _log("debug", f"所有触发原因在冷却中: {trigger_reasons}")
            return "fallback"

        # 每日限额
        if self._exceeded_daily_limit():
            _log("warn", f"超过每日LLM调用上限({MAX_DAILY_LLM_CALLS})，fallback")
            return "fallback"

        # 调用 LLM
        _log("info", f"🤖 AIOverride触发: {active_reasons} | signal={signal}")
        result = self._call_llm(signal, lgb_opinion, bar_context, active_reasons)

        # 更新状态
        for r in active_reasons:
            self._set_cooldown(r)
        self.state["daily_calls"] += 1
        self.calls_saved += 1
        _save_state(self.state)

        return result

    # ── LLM 调用 ────────────────────────────────────────────────────────

    def _call_llm(
        self,
        signal: str,
        lgb_opinion: str,
        bar: Dict[str, Any],
        reasons: List[str],
    ) -> str:
        """调用 LLM (首选 OpenAI-compatible API，备选 hermes CLI)。

        返回 'approve' | 'reject' | 'fallback'
        """
        prompt = self._build_prompt(signal, lgb_opinion, bar, reasons)

        if DRY_RUN:
            _log("info", f"DRY_RUN 模式, prompt={len(prompt)}chars → fallback")
            return "fallback"

        # 策略1: 直接调 OpenAI-compatible API (更快)
        if LLM_API_KEY:
            result = self._call_openai_api(prompt)
            if result is not None:
                return result

        # 策略2: hermes CLI (备选)
        result = self._call_hermes_cli(prompt)
        if result is not None:
            return result

        _log("error", "所有 LLM 调用方式均失败")
        return "fallback"

    def _call_openai_api(self, prompt: str) -> Optional[str]:
        """通过 curl 调用 OpenAI-compatible API。

        返回 'approve' | 'reject' | 'fallback' | None (失败)
        """
        payload = json.dumps({
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 10,       # 只需要一个词
            "temperature": 0.1,     # 确定性输出
        })

        try:
            proc = subprocess.run(
                [
                    "curl", "-s", "--max-time", str(LLM_TIMEOUT_SECONDS),
                    f"{LLM_API_BASE}/chat/completions",
                    "-H", f"Authorization: Bearer {LLM_API_KEY}",
                    "-H", "Content-Type: application/json",
                    "-d", payload,
                ],
                capture_output=True,
                text=True,
                timeout=LLM_TIMEOUT_SECONDS + 2,
            )

            if proc.returncode != 0:
                _log("warn", f"curl 失败: {proc.returncode} | stderr={proc.stderr[:200]}")
                return None

            data = json.loads(proc.stdout)
            choice = data.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "").strip()
            _log("info", f"API 回复 ({len(content)} chars): {content[:100]}")

            return self._parse_llm_response(content)

        except subprocess.TimeoutExpired:
            _log("warn", f"API 调用超时 ({LLM_TIMEOUT_SECONDS}s)")
            return None
        except json.JSONDecodeError as e:
            _log("warn", f"API 返回不是合法 JSON: {e} | stdout={proc.stdout[:200]}")
            return None
        except Exception as e:
            _log("error", f"API 调用异常: {e}")
            return None

    def _call_hermes_cli(self, prompt: str) -> Optional[str]:
        """备选: 通过 hermes CLI 调用。

        返回 'approve' | 'reject' | 'fallback' | None (失败)
        """
        try:
            result = subprocess.run(
                [HERMES_BIN, "chat", "-q", prompt, "-Q"],
                capture_output=True,
                text=True,
                timeout=LLM_TIMEOUT_SECONDS + 5,
                env={**os.environ, "HERMES_MAX_TURNS": "1"},
                cwd=str(PROJECT),
            )

            if result.returncode != 0:
                _log("warn", f"hermes CLI 返回非0: {result.returncode}")
                return None

            output = result.stdout.strip()
            _log("info", f"hermes CLI 回复 ({len(output)} chars): {output[:300]}")
            return self._parse_llm_response(output)

        except subprocess.TimeoutExpired:
            _log("warn", f"hermes CLI 超时")
            return None
        except FileNotFoundError:
            _log("error", f"hermes CLI 不可用: {HERMES_BIN}")
            return None
        except Exception as e:
            _log("error", f"hermes CLI 异常: {e}")
            return None

    def _build_prompt(
        self,
        signal: str,
        lgb_opinion: str,
        bar: Dict[str, Any],
        reasons: List[str],
    ) -> str:
        """构建 LLM 提示词。

        要求 LLM 只回复一个词: APPROVE / REJECT / FALLBACK。
        不给模型太多上下文避免思考过深——只需要判断模糊边界。
        """

        signal_meaning = {
            "LONG": "开多仓",
            "SHORT": "开空仓",
            "SELL": "平多仓",
            "COVER": "平空仓",
        }

        parts = [
            "你是加密货币量化交易系统的AI决策层。只回复一个词：APPROVE / REJECT / FALLBACK。不要解释。",
            "",
            f"信号: {signal_meaning.get(signal, signal)} ({signal})",
            f"触发原因: {', '.join(reasons)}",
            f"LGB确认: {lgb_opinion}",
            f"价格: ${bar.get('close', 0):.0f}",
            f"RSI: {bar.get('rsi', 50):.1f}",
            f"ADX: {bar.get('adx', 20):.1f}",
            f"MACD_Hist: {bar.get('macd_hist', 0):.2f}",
            f"MA7: ${bar.get('ma7', 0):.0f}",
            f"MA25: ${bar.get('ma25', 0):.0f}",
            f"波动率: {bar.get('volatility', 0) * 100:.2f}%",
        ]

        if bar.get("has_position"):
            parts.extend([
                f"持仓方向: {bar['position_side']}",
                f"持仓盈亏: {bar.get('position_pnl', 0) * 100:.2f}%",
                f"已持仓K线数: {bar.get('bars_held', 0)}",
            ])

        parts.extend([
            f"连续亏损次数: {self.state.get('consecutive_losses', 0)}",
            f"当日交易次数: {self.state.get('daily_trade_count', 0)}",
            "",
            "APPROVE = 同意执行信号, REJECT = 放弃, FALLBACK = 不干预",
            "只在信号明显不合理(如震荡市强行方向交易)时才REJECT。默认APPROVE。",
        ])

        return "\n".join(parts)

    def _parse_llm_response(self, output: str) -> str:
        """从 LLM 输出中提取决策词。"""
        # 标准化
        output_clean = output.strip().upper()

        # 精确匹配
        if re.search(r'\bAPPROVE\b', output_clean):
            return "approve"
        if re.search(r'\bREJECT\b', output_clean):
            return "reject"
        if re.search(r'\bFALLBACK\b', output_clean):
            return "fallback"

        # 常见中文
        if any(w in output.lower() for w in ["同意", "执行", "开仓", "平仓"]):
            return "approve"
        if any(w in output.lower() for w in ["拒绝", "放弃", "不执行", "跳过"]):
            return "reject"

        # 兜底: 任何非reject的明确回复 → approve
        _log("info", f"LLM回复无法解析决策词, 默认fallback: {output[:100]}")
        return "fallback"

    # ── 状态更新 ────────────────────────────────────────────────────────

    def update_trade_result(self, pnl: float):
        """交易结束后更新连亏计数"""
        if pnl < 0:
            self.state["consecutive_losses"] += 1
        else:
            self.state["consecutive_losses"] = 0
        self.state["daily_trade_count"] += 1
        self.state["last_signal"] = None
        _save_state(self.state)

    def set_last_signal(self, signal: str):
        """记录当前信号（用于下次判断）"""
        self.state["last_signal"] = signal
        _save_state(self.state)

    def flush(self):
        """确保状态落盘"""
        if self.calls_saved > 0:
            _save_state(self.state)
            _log("info", f"状态已保存 ({self.calls_saved} 次LLM调用)")
            self.calls_saved = 0


# ── 工厂 ────────────────────────────────────────────────────────────────

_global_override: Optional[AIOverride] = None


def get_ai_override() -> AIOverride:
    global _global_override
    if _global_override is None:
        _global_override = AIOverride()
    return _global_override


# ── CLI 测试 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    ai = get_ai_override()

    # 模拟信号
    ctx = {
        "close": 78620.0,
        "rsi": 48.5,
        "adx": 22.0,
        "macd_hist": -3.2,
        "ma7": 78500.0,
        "ma25": 78200.0,
        "volatility": 0.003,
        "has_position": False,
        "position_side": "",
        "position_pnl": 0.0,
        "bars_held": 0,
    }

    result = ai.judge(signal="LONG", lgb_opinion="no_opinion", bar_context=ctx)
    print(f"\n决策结果: {result}")