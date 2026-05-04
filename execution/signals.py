"""信号处理器和执行器"""

import logging
from typing import Optional, Dict
from dataclasses import dataclass, field, asdict

logger = logging.getLogger(__name__)


@dataclass
class SignalReport:
    """策略层输出的结构化信号报告 — 供决策层消费"""

    # === Identity ===
    timestamp: int = 0
    price: float = 0.0

    # === Strategy Output ===
    raw_signal: str = "HOLD"       # LONG | SHORT | HOLD (策略原始判断)
    exit_signal: Optional[str] = None  # SELL | COVER | None (风控平仓, 绕行AI)

    # === Condition Scores (0.0~1.0) ===
    long_score: float = 0.0
    short_score: float = 0.0

    # === Condition Details ===
    conditions_long: Dict[str, bool] = field(default_factory=dict)
    conditions_short: Dict[str, bool] = field(default_factory=dict)

    # === Key Indicators ===
    rsi: float = 50.0
    adx: float = 20.0
    macd_hist: float = 0.0
    ma7: float = 0.0
    ma25: float = 0.0
    ma99: float = 0.0
    volatility: float = 0.0
    volume_surge: bool = False

    # === Market Regime ===
    regime: str = "neutral"         # bull | bear | neutral
    price_trend_5bars: str = "sideways"  # up | down | sideways
    rsi_trend: str = "flat"         # rising | falling | flat
    adx_trend: str = "flat"

    # === Execution Context ===
    bars_since_last_trade: int = 0
    is_cooldown: bool = False
    lgb_opinion: str = "no_opinion"  # agree | disagree | no_opinion

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        """单行摘要, 用于日志"""
        long_parts = []
        for k, v in self.conditions_long.items():
            long_parts.append(f"{k}={'✓' if v else '✗'}")
        short_parts = []
        for k, v in self.conditions_short.items():
            short_parts.append(f"{k}={'✓' if v else '✗'}")
        return (
            f"SignalReport(price=${self.price:,.0f} | "
            f"raw={self.raw_signal} | exit={self.exit_signal} | "
            f"LONG={self.long_score:.0%}[{' '.join(long_parts)}] | "
            f"SHORT={self.short_score:.0%}[{' '.join(short_parts)}] | "
            f"RSI={self.rsi:.0f} ADX={self.adx:.0f} MACDh={self.macd_hist:.0f} "
            f"regime={self.regime} cooldown={self.is_cooldown})"
        )

    def to_ai_context(self) -> str:
        """生成供 AI 决策使用的上下文文本"""
        # 价格趋势描述
        trend_emoji = {"up": "↗", "down": "↘", "sideways": "→"}
        te = trend_emoji.get(self.price_trend_5bars, "→")

        # RSI 区间描述
        if self.rsi > 70:
            rsi_zone = "overbought"
        elif self.rsi < 30:
            rsi_zone = "oversold"
        else:
            rsi_zone = "neutral"

        # MA 排列
        if self.ma7 > self.ma25 > self.ma99:
            ma_desc = "BULLISH (MA7>MA25>MA99)"
        elif self.ma7 < self.ma25 < self.ma99:
            ma_desc = "BEARISH (MA7<MA25<MA99)"
        else:
            ma_desc = "NEUTRAL (no clear alignment)"

        lines = [
            "## Current Market Snapshot",
            f"Price: ${self.price:,.0f} | 5-bar trend: {te}",
            f"RSI: {self.rsi:.0f} ({rsi_zone}) | RSI trend: {self.rsi_trend}",
            f"ADX: {self.adx:.0f} | ADX trend: {self.adx_trend}",
            f"MACD Histogram: {self.macd_hist:+.0f}",
            f"MA Alignment: {ma_desc}",
            f"MA7=${self.ma7:,.0f} MA25=${self.ma25:,.0f} MA99=${self.ma99:,.0f}",
            f"Volatility: {self.volatility*100:.1f}% | Volume Surge: {'Yes' if self.volume_surge else 'No'}",
            "",
            "## Strategy Layer Assessment",
        ]

        # 计算通过/未通过描述
        long_passed = sum(1 for v in self.conditions_long.values() if v)
        short_passed = sum(1 for v in self.conditions_short.values() if v)
        total = len(self.conditions_long)

        def fmt_conditions(conds: dict) -> str:
            parts = []
            for k, v in conds.items():
                parts.append(f"{k}={v}")
            return ", ".join(parts)

        lines.append(f"LONG: {long_passed}/{total} passed | Raw: {self.raw_signal}")
        lines.append(f"  [{fmt_conditions(self.conditions_long)}]")
        lines.append(f"SHORT: {short_passed}/{total} passed")
        lines.append(f"  [{fmt_conditions(self.conditions_short)}]")

        if self.lgb_opinion != "no_opinion":
            lines.append(f"LightGBM opinion: {self.lgb_opinion}")

        lines.append("")
        lines.append("## Context")
        lines.append(f"Bars since last trade: {self.bars_since_last_trade}")
        if self.is_cooldown:
            lines.append("⚠️ In cooldown period (recently exited a position)")

        if self.exit_signal:
            lines.append(f"⚠️ Exit signal triggered: {self.exit_signal} (risk management)")

        lines.append("")
        lines.append("## Decision")
        lines.append("Reply ONE word: LONG, SHORT, or HOLD.")
        lines.append("Optional: POSITION_SIZE=X.XX (fraction, default 0.15)")

        return "\n".join(lines)


@dataclass
class FinalDecision:
    """决策层输出的最终交易决策"""
    action: str                  # LONG | SHORT | SELL | COVER | HOLD
    source: str                  # "strategy_direct" | "ai_decision" | "auto_clear"
    reasoning: str = ""
    confidence: float = 0.0

    @property
    def is_actionable(self) -> bool:
        return self.action != "HOLD"


def default_signal_handler(signal: str, bar_data: dict, engine):
    """默认信号处理器 - 将策略信号转为买卖操作"""
    close = bar_data["close"]
    
    if signal == "BUY" or signal == "LONG":
        engine.buy()
    elif signal == "SELL":
        engine.sell()
    elif signal == "SHORT":
        engine.short_sell()
    elif signal == "COVER":
        engine.short_cover()
    else:
        logger.debug(f"未知信号: {signal}")
