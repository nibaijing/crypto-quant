"""V5 — 纯趋势做空策略

核心理念:
- 不用等反弹，趋势向下就直接做空
- 趋势过滤: MA7 < MA25 (短期趋势向下) → 做空
- RSI过滤: RSI > 40 (不是超卖区，避免追空)
- 平空: RSI < 25 (超卖) 或 MA7上穿MA25

和V4区别:
- V4: 等反弹到MA25以上 → 做空 (机会太少)
- V5: 趋势向下就做空 (不需要反弹确认)
"""

import logging
from typing import Optional, Dict

import numpy as np

from strategies.base import Strategy, Signal

logger = logging.getLogger(__name__)


class TrendShortStrategy(Strategy):
    """纯趋势做空策略 V5"""

    def __init__(self,
                 ma_fast: int = 7,
                 ma_slow: int = 25,
                 rsi_period: int = 14,
                 rsi_entry: float = 45,
                 rsi_exit: float = 25,
                 atr_period: int = 14,
                 atr_stop_mult: float = 1.5,
                 position_mult: float = 1.0,
                 **kwargs):

        super().__init__(name=f"TrendShortV5")
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.rsi_period = rsi_period
        self.rsi_entry = rsi_entry
        self.rsi_exit = rsi_exit
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.position_mult = position_mult

        self.entry_price: float = 0
        self.entry_atr: float = 0
        self.bars_held: int = 0
        self.position_side: Optional[str] = None

    def on_bar(self, bar_data: Dict, engine) -> Optional[str]:
        close = bar_data["close"]
        history = bar_data.get("history")
        position = bar_data.get("position")

        if history is None or len(history) < self.ma_slow:
            return Signal.HOLD

        closes = history["close"].values
        highs = history["high"].values
        lows = history["low"].values

        ma_fast_val = np.mean(closes[-self.ma_fast:])
        ma_slow_val = np.mean(closes[-self.ma_slow:])
        rsi_val = self._rsi(closes, self.rsi_period)
        atr = self._atr(highs, lows, closes, self.atr_period)

        if any(v is None for v in [ma_fast_val, ma_slow_val, rsi_val, atr]):
            return Signal.HOLD

        in_position = (position is not None and position.size > 0) or self.position_side is not None
        if in_position:
            self.bars_held += 1

        # === ATR止损 ===
        if in_position and self.entry_price > 0 and self.entry_atr > 0:
            if self.position_side == "short":
                stop_price = self.entry_price + self.entry_atr * self.atr_stop_mult
                if close >= stop_price:
                    logger.info(f"🛑 空头ATR止损: {close:.2f} >= {stop_price:.2f}")
                    self._reset()
                    return Signal.COVER

        # === 趋势做空 ===
        downtrend = ma_fast_val < ma_slow_val  # MA7 < MA25
        not_oversold = rsi_val > self.rsi_entry  # RSI > 阈值

        short_entry = (
            not in_position and
            downtrend and
            not_oversold
        )

        short_exit = (
            in_position and
            self.position_side == "short" and
            self.bars_held >= 2 and (
                rsi_val < self.rsi_exit or  # RSI超卖
                ma_fast_val > ma_slow_val   # MA金叉
            )
        )

        if short_entry:
            self.entry_price = close
            self.entry_atr = atr
            self.bars_held = 0
            self.position_side = "short"
            bar_data["_position_mult"] = self.position_mult
            logger.info(f"🔻 SHORT: ${close:.2f} | MA7<MA25 | RSI={rsi_val:.1f} | ATR={atr:.2f}")
            return Signal.SHORT

        elif short_exit:
            pnl = (self.entry_price - close) / self.entry_price * 100
            logger.info(f"📈 COVER: ${close:.2f} | PnL={pnl:.2f}% | bars={self.bars_held}")
            self._reset()
            return Signal.COVER

        return Signal.HOLD

    def _reset(self):
        self.entry_price = 0
        self.entry_atr = 0
        self.bars_held = 0
        self.position_side = None

    @staticmethod
    def _rsi(prices, period):
        if len(prices) < period + 1: return None
        d = np.diff(prices)
        g = np.where(d > 0, d, 0)
        l = np.where(d < 0, -d, 0)
        avg_gain = np.mean(g[-period:])
        avg_loss = np.mean(l[-period:])
        if avg_loss == 0: return 100.0
        return 100 - 100 / (1 + avg_gain / avg_loss)

    @staticmethod
    def _atr(h, l, c, period):
        if len(c) < period + 1: return None
        tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
        return np.mean(tr[-period:]) if len(tr) >= period else None