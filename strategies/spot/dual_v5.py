"""V5 Dual — 多空双杀策略

做空 (核心, 权重2x):
  MA7 < MA25 + RSI > 40 → SHORT
  平空: RSI < 30 或 MA金叉
  止损: ATR × 2.5

做多 (辅助, 权重1x):
  只在非熊市环境下做多 (close > MA99)
  MA7 > MA25 + RSI < 40 → BUY
  平多: RSI > 65 或 MA死叉
  止损: ATR × 2.0

市场判断 (MA99):
  close > MA99 × 1.02 → 牛市 → 做多
  close < MA99 × 0.98 → 熊市 → 只做空
  否则 → 震荡 → 多空都做
"""

import logging
from typing import Optional, Dict

import numpy as np

from strategies.base import Strategy, Signal

logger = logging.getLogger(__name__)


class DualTrendStrategy(Strategy):
    """多空双杀策略 V5 Dual"""

    def __init__(self,
                 long_position_mult: float = 1.0,
                 short_position_mult: float = 2.0,
                 **kwargs):

        super().__init__(name="DualV5")
        self.long_position_mult = long_position_mult
        self.short_position_mult = short_position_mult

        self.entry_price: float = 0
        self.bars_held: int = 0
        self.position_side: Optional[str] = None

    def on_bar(self, bar_data: Dict, engine) -> Optional[str]:
        close = bar_data["close"]
        history = bar_data.get("history")
        position = bar_data.get("position")

        if history is None or len(history) < 100:
            return Signal.HOLD

        closes = history["close"].values
        highs = history["high"].values
        lows = history["low"].values

        # MA
        ma7 = np.mean(closes[-7:])
        ma25 = np.mean(closes[-25:])
        ma99 = np.mean(closes[-99:])

        # RSI 14
        rsi = self._rsi(closes, 14)

        # ATR 14
        tr = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
              for i in range(1, len(closes))]
        atr = np.mean(tr[-14:]) if len(tr) >= 14 else None

        if rsi is None or atr is None:
            return Signal.HOLD

        # ============================================
        # 市场状态 (MA99)
        # ============================================
        if ma99 > 0:
            deviation = close / ma99 - 1
            if deviation > 0.02:
                regime = "bull"
            elif deviation < -0.02:
                regime = "bear"
            else:
                regime = "neutral"
        else:
            regime = "neutral"

        in_position = (position is not None and position.size > 0) or self.position_side is not None
        if in_position:
            self.bars_held += 1

        # ============================================
        # ATR 移动止损
        # ============================================
        if in_position and self.entry_price > 0:
            if self.position_side == "long":
                stop = self.entry_price - atr * 2.0
                if close <= stop:
                    logger.info(f"🛑 多头ATR止损: ${close:.2f}")
                    self._reset()
                    return Signal.SELL
            elif self.position_side == "short":
                stop = self.entry_price + atr * 2.5
                if close >= stop:
                    logger.info(f"🛑 空头ATR止损: ${close:.2f}")
                    self._reset()
                    return Signal.COVER

        # ============================================
        # 做空信号 (core)
        # ============================================
        downtrend = ma7 < ma25
        short_entry = (
            not in_position and
            downtrend and
            rsi > 40
        )

        short_exit = (
            in_position and
            self.position_side == "short" and
            self.bars_held >= 2 and (
                rsi < 30 or    # 超卖
                ma7 > ma25     # MA金叉
            )
        )

        # ============================================
        # 做多信号 (辅助)
        # ============================================
        uptrend = ma7 > ma25
        long_entry = (
            not in_position and
            uptrend and
            rsi < 40 and
            regime in ("bull", "neutral")  # 熊市不做多
        )

        long_exit = (
            in_position and
            self.position_side == "long" and
            self.bars_held >= 2 and (
                rsi > 65 or     # 超买
                ma7 < ma25      # MA死叉
            )
        )

        # ============================================
        # 执行
        # ============================================
        if short_entry:
            self.entry_price = close
            self.bars_held = 0
            self.position_side = "short"
            bar_data["_position_mult"] = self.short_position_mult
            logger.info(f"🔻 SHORT: ${close:.2f} | RSI={rsi:.1f} | regime={regime} | mult={self.short_position_mult}x")
            return Signal.SHORT

        elif short_exit:
            pnl = (self.entry_price - close) / self.entry_price * 100
            logger.info(f"📈 COVER: ${close:.2f} | PnL={pnl:+.2f}% | bars={self.bars_held}")
            self._reset()
            return Signal.COVER

        elif long_entry:
            self.entry_price = close
            self.bars_held = 0
            self.position_side = "long"
            bar_data["_position_mult"] = self.long_position_mult
            logger.info(f"📈 LONG: ${close:.2f} | RSI={rsi:.1f} | regime={regime} | mult={self.long_position_mult}x")
            return Signal.BUY

        elif long_exit:
            pnl = (close - self.entry_price) / self.entry_price * 100
            logger.info(f"📉 SELL: ${close:.2f} | PnL={pnl:+.2f}% | bars={self.bars_held}")
            self._reset()
            return Signal.SELL

        return Signal.HOLD

    def _reset(self):
        self.entry_price = 0
        self.bars_held = 0
        self.position_side = None

    @staticmethod
    def _rsi(prices, period):
        if len(prices) < period + 1:
            return None
        d = np.diff(prices)
        gain = np.mean(d[d > 0]) if np.any(d > 0) else 0
        loss = -np.mean(d[d < 0]) if np.any(d < 0) else 0
        if loss == 0:
            return 100.0
        return 100 - 100 / (1 + gain / loss)