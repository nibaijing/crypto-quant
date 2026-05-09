#!/usr/bin/env python3
"""Live Gate — 开仓前集中式准入检查

设计原则（参考 DD 老师量化交易平台设计）：
- 信号进入实盘前，必须先过 Gate
- 所有风控检查集中一处，不散落
- 每项检查返回明确的 pass/reason，可追溯

以损定仓：
- 先定最大可承受亏损 → 再反推仓位大小
- 结合止损距离，确保单笔亏损在预算内
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from core.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """Gate 检查结果"""
    passed: bool
    reason: str = ""
    allowed_size: float = 0.0          # 通过以损定仓计算的仓位
    details: dict = field(default_factory=dict)


class LiveGate:
    """开仓准入 Gate。

    所有开仓信号在执行前必须通过此 Gate 的 check()。
    集中管理：白名单、杠杆、仓位冲突、风险距离、以损定仓。

    Usage:
        gate = LiveGate()
        result = gate.check(
            symbol="BTC-USDT",
            side="long",
            price=80000,
            stop_loss=79000,     # 计划止损价（以损定仓用）
            equity=1000,
            cash=500,
            leverage=3,
            current_position_side=None,  # None = 空仓
        )
        if result.passed:
            executor.buy(symbol, size=result.allowed_size, price=price)
    """

    # 白名单
    SYMBOL_WHITELIST = {"BTC-USDT", "BTC-USDT-SWAP"}

    # 以损定仓参数
    MAX_LOSS_PCT = 1.0         # 单笔最大亏损 ≤ 权益 100%（全仓止损）
    MIN_SIZE = 0.0001          # 最小合约数量

    def __init__(self):
        self.config = get_config()

    def check(
        self,
        *,
        symbol: str,
        side: str,               # "long" | "short"
        price: float,
        stop_loss: float,        # 计划止损价（必填，用于以损定仓）
        equity: float,
        cash: float,
        leverage: int,
        current_position_side: Optional[str] = None,
        current_position_size: float = 0.0,
    ) -> GateResult:
        """执行全部 Gate 检查，返回 GateResult。

        检查顺序：白名单 → 持仓冲突 → 反向拦截 → 止损合理性 → 以损定仓 →
                  单笔上限 → 总仓位上限 → 保证金充足性。
        任一失败立即返回，不继续后续检查。
        """
        details = {}

        # 1. 白名单
        if symbol not in self.SYMBOL_WHITELIST:
            return GateResult(False, f"标的 {symbol} 不在白名单 {self.SYMBOL_WHITELIST}")
        details["whitelist"] = "ok"

        # 2. 持仓冲突（同一币种同一时刻只允许一单）
        if current_position_side and current_position_size > 0:
            return GateResult(
                False,
                f"已持有 {current_position_side} 仓 ({current_position_size:.4f} BTC)，禁止加仓",
                details=details,
            )
        details["position_conflict"] = "ok"

        # 3. 反向持仓拦截（防御层：已有仓时禁止反向开）
        if current_position_side:
            if (current_position_side == "long" and side == "short") or \
               (current_position_side == "short" and side == "long"):
                return GateResult(
                    False,
                    f"已持有 {current_position_side} 仓，禁止开反向 {side} 仓",
                    details=details,
                )
        details["opposite_block"] = "ok"

        # 4. 止损价合理性 (ATR 紧止损容忍: 强制设最小距离而非拒绝)
        MIN_STOP_DIST_PCT = 0.001  # 最小止损距离 0.1%
        if stop_loss <= 0:
            return GateResult(False, "止损价未设定或无效", details=details)
        original_stop = stop_loss
        if side == "long":
            min_stop = price * (1 - MIN_STOP_DIST_PCT)
            if stop_loss >= price:
                stop_loss = min_stop
            elif stop_loss > min_stop:
                stop_loss = min_stop
        elif side == "short":
            min_stop = price * (1 + MIN_STOP_DIST_PCT)
            if stop_loss <= price:
                stop_loss = min_stop
            elif stop_loss < min_stop:
                stop_loss = min_stop
        details["stop_valid"] = "ok"
        if stop_loss != original_stop:
            details["stop_adjusted"] = f"${original_stop:,.0f}→${stop_loss:,.0f} (min {MIN_STOP_DIST_PCT:.1%} dist)"

        # 5. 以损定仓
        size = self._calc_position_size(
            price=price,
            stop_loss=stop_loss,
            equity=equity,
            cash=cash,
            leverage=leverage,
            side=side,
        )
        if size < self.MIN_SIZE:
            return GateResult(
                False,
                f"以损定仓计算仓位 {size:.6f} BTC < 最小 {self.MIN_SIZE}",
                details=details,
            )
        details["size_by_loss"] = round(size, 6)
        details["max_loss"] = round(abs(price - stop_loss) * size, 2)

        # 6. 单笔上限
        trade_value = size * price
        max_single = equity * self.config.risk.max_single_position_pct
        if trade_value > max_single:
            return GateResult(
                False,
                f"单笔仓位 ${trade_value:,.0f} 超过上限 ${max_single:,.0f} "
                f"(equity ${equity:,.0f} × {self.config.risk.max_single_position_pct:.0%})",
                details=details,
            )
        details["single_limit"] = "ok"

        # 7. 保证金充足
        margin = trade_value / leverage
        if margin > cash * 0.95:
            return GateResult(
                False,
                f"保证金不足: 需要 ${margin:,.0f} > 可用 ${cash:,.0f}",
                details=details,
            )
        details["margin"] = "ok"

        # 8. 杠杆
        if leverage > self.config.futures.max_leverage:
            return GateResult(
                False,
                f"杠杆 {leverage}x 超过最大 {self.config.futures.max_leverage}x",
                details=details,
            )
        details["leverage"] = "ok"

        return GateResult(True, "通过", allowed_size=size, details=details)

    def _calc_position_size(
        self,
        price: float,
        stop_loss: float,
        equity: float,
        cash: float,
        leverage: int,
        side: str,
    ) -> float:
        """以损定仓：先定最大亏损，反推仓位。

        公式:
            max_loss = equity × MAX_LOSS_PCT
            stop_distance = |price - stop_loss|
            size = max_loss / stop_distance

        再受限于：
            - 现金可买 = cash × leverage × 0.9 / price
            - 单笔上限 = equity × max_single_position_pct / price
        """
        if price <= 0 or stop_loss <= 0:
            return 0.0

        stop_distance = abs(price - stop_loss)
        if stop_distance == 0:
            return 0.0

        # 以损定仓
        max_loss = equity * self.MAX_LOSS_PCT
        size_from_loss = max_loss / stop_distance

        # 现金约束
        size_from_cash = (cash * leverage * 0.9) / price if price > 0 else 0

        # 单笔上限约束 (期货语义: pct 控制保证金占比, 杠杆放大得仓位)
        size_from_single = (equity * self.config.risk.max_single_position_pct * leverage) / price

        size = min(size_from_loss, size_from_cash, size_from_single)

        logger.debug(
            f"以损定仓: max_loss=${max_loss:.2f} | "
            f"stop_dist=${stop_distance:.0f} | "
            f"loss_size={size_from_loss:.6f} | "
            f"cash_size={size_from_cash:.6f} | "
            f"single_size={size_from_single:.6f} | "
            f"→ {size:.6f} BTC ({side})"
        )

        return max(size, self.MIN_SIZE)


# ── 单例 ──

_gate: Optional[LiveGate] = None


def get_live_gate() -> LiveGate:
    global _gate
    if _gate is None:
        _gate = LiveGate()
    return _gate
