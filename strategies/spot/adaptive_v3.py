"""V3 — 自适应趋势策略 (多空双向)

核心理念: 顺大势做趋势，牛市做多、熊市做空、震荡不做。

信号逻辑:
- 牛市 (price > MA99): 做多 (超卖买入，超买卖出)
- 熊市 (price < MA99): 做空 (超买做空，超卖平空)
- 震荡 (price 在 MA99 附近): 双向信号，但放宽条件

风控:
- ATR 动态止损 (做多止损 = entry - ATR*mult，做空止损 = entry + ATR*mult)
- 最小持仓周期 (避免噪音交易)
- 成交量确认 (放量突破才入场)
"""

import logging
from typing import Optional, Dict

import numpy as np
import pandas as pd

from strategies.base import Strategy, Signal

logger = logging.getLogger(__name__)


class AdaptiveTrendStrategy(Strategy):
    """自适应趋势策略 V3 — 多空双向
    
    参数:
        allow_short (True): 是否允许做空
        short_rsi_threshold (60): RSI>此阈值且MA死叉时考虑做空
        short_ma_confirm (True): 做空是否需要MA死叉确认
    """
    
    def __init__(self,
                 ma_fast: int = 7,
                 ma_slow: int = 25,
                 ma_trend: int = 99,
                 rsi_period: int = 14,
                 rsi_oversold: float = 40,
                 rsi_overbought: float = 60,
                 atr_period: int = 14,
                 atr_stop_mult: float = 2.0,
                 min_bars_hold: int = 4,
                 vol_ratio_threshold: float = 1.2,
                 allow_short: bool = True,
                 short_rsi_threshold: float = 60,
                 short_ma_confirm: bool = True,
                 **kwargs):
        
        super().__init__(name=f"Adaptive(MA{ma_trend})")
        
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.ma_trend = ma_trend
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.min_bars_hold = min_bars_hold
        self.vol_ratio_threshold = vol_ratio_threshold
        self.allow_short = allow_short
        self.short_rsi_threshold = short_rsi_threshold
        self.short_ma_confirm = short_ma_confirm
        
        # 状态
        self.entry_price: float = 0
        self.entry_atr: float = 0
        self.bars_held: int = 0
        self.position_side: Optional[str] = None  # 'long' / 'short' / None
        self.market_regime: str = "neutral"
    
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
        rsi_val = self._rsi(closes, self.rsi_period)
        atr = self._atr(highs, lows, closes, self.atr_period)
        macd_hist = self._macd_histogram(closes)
        
        vol_ma = np.mean(volumes[-20:]) if len(volumes) >= 20 else volumes[-1]
        vol_ratio = volume / vol_ma if vol_ma > 0 else 1
        
        if any(v is None for v in [ma_fast_val, ma_slow_val, rsi_val, atr, macd_hist]):
            return Signal.HOLD
        
        # === 市场状态判断 ===
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
        # 做多信号 (牛市/震荡)
        # =========================================
        long_ok = self.market_regime in ("bull", "neutral")
        
        long_entry = (
            long_ok and
            not in_position and
            ma_fast_val > ma_slow_val and
            rsi_val < self.rsi_oversold and
            macd_hist > 0 and
            vol_ratio > self.vol_ratio_threshold
        )
        
        long_exit = (
            in_position and
            self.position_side == "long" and
            self.bars_held >= self.min_bars_hold and (
                ma_fast_val < ma_slow_val or
                rsi_val > self.rsi_overbought
            )
        )
        
        # =========================================
        # 做空信号 (熊市/震荡)
        # =========================================
        if self.allow_short:
            short_ok = self.market_regime in ("bear", "neutral")
            
            # MA确认
            ma_confirm_ok = not self.short_ma_confirm or (ma_fast_val < ma_slow_val)
            
            short_entry = (
                short_ok and
                not in_position and
                rsi_val > self.short_rsi_threshold and
                macd_hist < 0 and
                vol_ratio > self.vol_ratio_threshold and
                ma_confirm_ok
            )
            
            short_exit = (
                in_position and
                self.position_side == "short" and
                self.bars_held >= self.min_bars_hold and (
                    rsi_val < self.rsi_oversold or
                    ma_fast_val > ma_slow_val  # MA金叉平空
                )
            )
        else:
            short_entry = False
            short_exit = False
        
        # =========================================
        # 执行信号
        # =========================================
        
        if long_entry:
            self.entry_price = close
            self.entry_atr = atr
            self.bars_held = 0
            self.position_side = "long"
            logger.info(
                f"📈 LONG: ${close:.2f} | regime={self.market_regime} | "
                f"RSI={rsi_val:.1f} | vol={vol_ratio:.1f}x | ATR={atr:.2f}"
            )
            return Signal.BUY
        
        elif long_exit:
            pnl = (close - self.entry_price) / self.entry_price * 100
            logger.info(f"📉 CLOSE LONG: ${close:.2f} | PnL={pnl:.2f}% | bars={self.bars_held}")
            self._reset()
            return Signal.SELL
        
        elif short_entry:
            self.entry_price = close
            self.entry_atr = atr
            self.bars_held = 0
            self.position_side = "short"
            logger.info(
                f"🔻 SHORT: ${close:.2f} | regime={self.market_regime} | "
                f"RSI={rsi_val:.1f} | vol={vol_ratio:.1f}x | ATR={atr:.2f}"
            )
            return Signal.SHORT
        
        elif short_exit:
            pnl = (self.entry_price - close) / self.entry_price * 100
            logger.info(f"📈 COVER SHORT: ${close:.2f} | PnL={pnl:.2f}% | bars={self.bars_held}")
            self._reset()
            return Signal.COVER
        
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
    
    @staticmethod
    def _macd_histogram(p):
        if len(p) < 26: return None
        ef = AdaptiveTrendStrategy._ema(p, 12)
        es = AdaptiveTrendStrategy._ema(p, 26)
        if ef is None or es is None: return None
        macd = ef - es
        return macd - macd * 0.9