"""V4 — 熊市优先策略 (做空为主，做多为辅)

核心理念:
- 熊市里做空应该是常态，不需要等"完美信号"
- 做空入场: MA99下行 + 价格反弹到短期均线附近 = 做空
- 做多入场: 只在极度超卖 + 明确底背离时才做反弹
- 用ATR移动止损锁利润，不用固定止盈

信号逻辑:
做空 (核心):
  1. 价格 < MA99 (确认熊市)
  2. 价格反弹到 MA25 以上 (短期超买)
  3. RSI > 50 (不在超卖区)
  4. → 做空
  平空: RSI < 30 (超卖) 或 MA7上穿MA25

做多 (辅助):
  1. RSI < 25 (极度超卖)
  2. 成交量放大 (恐慌盘)
  3. → 做反弹
  平多: RSI > 50 或 价格回到MA25

风控:
- ATR移动止损 (做空止损 = entry + ATR*mult，做多止损 = entry - ATR*mult)
- 做空仓位 1.5x (熊市里做空应该加仓)
- 做多仓位 0.5x (反弹轻仓)
"""

import logging
from typing import Optional, Dict

import numpy as np
import pandas as pd

from strategies.base import Strategy, Signal

logger = logging.getLogger(__name__)


class BearMarketStrategy(Strategy):
    """熊市优先策略 V4
    
    参数:
        short_rsi_entry (50): 做空RSI阈值，价格>此值才做空
        short_rsi_exit (30): 平空RSI阈值，价格<此值平空
        long_rsi_entry (25): 做多RSI阈值，极度超卖才做反弹
        long_rsi_exit (50): 平多RSI阈值
        atr_stop_mult (1.5): ATR止损倍数
        short_position_mult (1.5): 做空仓位倍数
        long_position_mult (0.5): 做多仓位倍数
    """
    
    def __init__(self,
                 ma_fast: int = 7,
                 ma_slow: int = 25,
                 ma_trend: int = 99,
                 rsi_period: int = 14,
                 short_rsi_entry: float = 50,
                 short_rsi_exit: float = 30,
                 long_rsi_entry: float = 25,
                 long_rsi_exit: float = 50,
                 atr_period: int = 14,
                 atr_stop_mult: float = 1.5,
                 vol_ratio_threshold: float = 1.0,
                 short_position_mult: float = 1.5,
                 long_position_mult: float = 0.5,
                 **kwargs):
        
        super().__init__(name=f"BearV4")
        
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.ma_trend = ma_trend
        self.rsi_period = rsi_period
        self.short_rsi_entry = short_rsi_entry
        self.short_rsi_exit = short_rsi_exit
        self.long_rsi_entry = long_rsi_entry
        self.long_rsi_exit = long_rsi_exit
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.vol_ratio_threshold = vol_ratio_threshold
        self.short_position_mult = short_position_mult
        self.long_position_mult = long_position_mult
        
        # 状态
        self.entry_price: float = 0
        self.entry_atr: float = 0
        self.bars_held: int = 0
        self.position_side: Optional[str] = None
        self.market_regime: str = "neutral"
        self.ma_trend_slope: float = 0  # MA99斜率，判断趋势方向
    
    def on_bar(self, bar_data: Dict, engine) -> Optional[str]:
        close = bar_data["close"]
        history = bar_data.get("history")
        position = bar_data.get("position")
        volume = bar_data.get("volume", 0)
        
        if history is None or len(history) < self.ma_trend:
            return Signal.HOLD
        
        closes = history["close"].values
        highs = history["high"].values
        lows = history["low"].values
        volumes = history["volume"].values
        
        # === 计算指标 ===
        ma_trend_val = np.mean(closes[-self.ma_trend:])
        ma_fast_val = np.mean(closes[-self.ma_fast:])
        ma_slow_val = np.mean(closes[-self.ma_slow:])
        
        # MA99 前值，计算斜率
        if len(closes) > self.ma_trend + 1:
            ma_trend_prev = np.mean(closes[-(self.ma_trend+1):-1])
        else:
            ma_trend_prev = ma_trend_val
        
        rsi_val = self._rsi(closes, self.rsi_period)
        atr = self._atr(highs, lows, closes, self.atr_period)
        
        vol_ma = np.mean(volumes[-20:]) if len(volumes) >= 20 else volumes[-1]
        vol_ratio = volume / vol_ma if vol_ma > 0 else 1
        
        if any(v is None for v in [ma_fast_val, ma_slow_val, rsi_val, atr]):
            return Signal.HOLD
        
        # === 市场状态 ===
        if close > ma_trend_val * 1.02:
            self.market_regime = "bull"
        elif close < ma_trend_val * 0.98:
            self.market_regime = "bear"
        else:
            self.market_regime = "neutral"
        
        # === 持仓管理 ===
        in_position = (position is not None and position.size > 0) or self.position_side is not None
        if in_position:
            self.bars_held += 1
        
        # === ATR 动态止损 ===
        if in_position and self.entry_price > 0 and self.entry_atr > 0:
            if self.position_side == "long":
                stop_price = self.entry_price - self.entry_atr * self.atr_stop_mult
                if close <= stop_price:
                    logger.info(f"🛑 多头ATR止损: {close:.2f} ≤ {stop_price:.2f}")
                    self._reset()
                    return Signal.SELL
            
            elif self.position_side == "short":
                stop_price = self.entry_price + self.entry_atr * self.atr_stop_mult
                if close >= stop_price:
                    logger.info(f"🛑 空头ATR止损: {close:.2f} ≥ {stop_price:.2f}")
                    self._reset()
                    return Signal.COVER
        
        # =========================================
        # 做空信号 (熊市核心)
        # =========================================
        # 价格反弹到MA25附近 (短期超买) + RSI>50 → 做空
        short_entry = (
            not in_position and
            self.market_regime in ("bear",) and
            close > ma_slow_val and       # 反弹到MA25以上 = 短期高位
            rsi_val > self.short_rsi_entry and  # RSI > 50, 不是超卖
            vol_ratio > self.vol_ratio_threshold
        )
        
        short_exit = (
            in_position and
            self.position_side == "short" and
            self.bars_held >= 2 and (
                rsi_val < self.short_rsi_exit or   # RSI超卖 → 平空
                (close < ma_fast_val and ma_fast_val < ma_slow_val)  # 加速下跌 → 也可以继续持有但不平
            )
        )
        # 修正: 用更可靠的退出条件
        short_exit = (
            in_position and
            self.position_side == "short" and
            self.bars_held >= 2 and (
                rsi_val < self.short_rsi_exit or    # RSI < 30 超卖
                ma_fast_val > ma_slow_val           # MA金叉
            )
        )
        
        # =========================================
        # 做多信号 (极度超卖反弹)
        # =========================================
        long_entry = (
            not in_position and
            self.market_regime in ("bear",) and  # 熊市里做反弹
            rsi_val < self.long_rsi_entry and     # RSI < 25 极度超卖
            vol_ratio > 1.3                       # 放量恐慌盘
        )
        
        long_exit = (
            in_position and
            self.position_side == "long" and
            self.bars_held >= 2 and (
                rsi_val > self.long_rsi_exit or   # RSI > 50 反弹到位
                close > ma_slow_val               # 回到MA25
            )
        )
        
        # =========================================
        # 执行信号
        # =========================================
        
        if short_entry:
            self.entry_price = close
            self.entry_atr = atr
            self.bars_held = 0
            self.position_side = "short"
            # 设置仓位倍数
            bar_data["_position_mult"] = self.short_position_mult
            logger.info(
                f"🔻 SHORT: ${close:.2f} | regime={self.market_regime} | "
                f"RSI={rsi_val:.1f} | MA99={ma_trend_val:.0f} | ATR={atr:.2f} | mult={self.short_position_mult}x"
            )
            return Signal.SHORT
        
        elif short_exit:
            pnl = (self.entry_price - close) / self.entry_price * 100
            logger.info(f"📈 COVER: ${close:.2f} | PnL={pnl:.2f}% | bars={self.bars_held} | from={self.entry_price:.0f}")
            self._reset()
            return Signal.COVER
        
        elif long_entry:
            self.entry_price = close
            self.entry_atr = atr
            self.bars_held = 0
            self.position_side = "long"
            bar_data["_position_mult"] = self.long_position_mult
            logger.info(
                f"📈 LONG (bounce): ${close:.2f} | regime={self.market_regime} | "
                f"RSI={rsi_val:.1f} | vol={vol_ratio:.1f}x | ATR={atr:.2f} | mult={self.long_position_mult}x"
            )
            return Signal.BUY
        
        elif long_exit:
            pnl = (close - self.entry_price) / self.entry_price * 100
            logger.info(f"📉 CLOSE LONG: ${close:.2f} | PnL={pnl:.2f}% | bars={self.bars_held}")
            self._reset()
            return Signal.SELL
        
        return Signal.HOLD
    
    def _reset(self):
        self.entry_price = 0
        self.entry_atr = 0
        self.bars_held = 0
        self.position_side = None
    
    # ===== 技术指标 =====
    
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
    
    @staticmethod
    def _ema(p, period):
        if len(p) < period: return None
        k = 2 / (period + 1)
        e = p[0]
        for x in p[1:]: e = x * k + e * (1 - k)
        return e