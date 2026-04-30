"""现货 MA/RSI/MACD 趋势策略

从现有 auto_trading.py 迁移而来，保持策略逻辑不变：
- MA 快慢线交叉 (MA7 / MA25)
- RSI 超买超卖过滤 (RSI < 35 买, RSI > 65 卖)
- MACD histogram 确认趋势
- ADX 趋势强度过滤 (ADX > 25)
"""

import logging
from typing import Optional, Dict, Any

import pandas as pd
import numpy as np

from strategies.base import Strategy, Signal

logger = logging.getLogger(__name__)


class MATrendStrategy(Strategy):
    """MA 趋势 + RSI + MACD 多因子确认策略
    
    这是你现有的 auto_trading.py 策略的完整迁移。
    
    参数:
        ma_fast (7): 快线周期
        ma_slow (25): 慢线周期
        rsi_period (14): RSI 周期
        rsi_oversold (35): RSI 超卖阈值
        rsi_overbought (65): RSI 超买阈值
        macd_fast (12): MACD 快线
        macd_slow (26): MACD 慢线
        macd_signal (9): MACD 信号线
        adx_period (14): ADX 周期
        adx_threshold (25): ADX 趋势阈值
        stop_loss_pct (-3): 止损
        take_profit_pct (10): 止盈
    """
    
    def __init__(self,
                 ma_fast: int = 7,
                 ma_slow: int = 25,
                 rsi_period: int = 14,
                 rsi_oversold: float = 35,
                 rsi_overbought: float = 65,
                 macd_fast: int = 12,
                 macd_slow: int = 26,
                 macd_signal: int = 9,
                 adx_period: int = 14,
                 adx_threshold: float = 25,
                 stop_loss_pct: float = -3,
                 take_profit_pct: float = 10,
                 **kwargs):
        super().__init__(name=f"MATrend({ma_fast}/{ma_slow})")
        
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.stop_loss_pct = stop_loss_pct / 100
        self.take_profit_pct = take_profit_pct / 100
        
        # 追踪状态
        self.entry_price: float = 0
        self._prev_macd_hist: float = 0
    
    def on_bar(self, bar_data: Dict, engine) -> Optional[str]:
        """处理K线，返回交易信号"""
        close = bar_data["close"]
        history = bar_data.get("history")
        position = bar_data.get("position")
        
        if history is None or len(history) < self.ma_slow + self.adx_period:
            return Signal.HOLD
        
        closes = history["close"].values
        highs = history["high"].values
        lows = history["low"].values
        
        # 计算指标
        ma_fast_val = self._sma(closes, self.ma_fast)
        ma_slow_val = self._sma(closes, self.ma_slow)
        rsi_val = self._rsi(closes, self.rsi_period)
        macd_hist = self._macd_histogram(closes)
        adx_val = self._adx(highs, lows, closes, self.adx_period)
        
        # 数据不足
        if any(v is None for v in [ma_fast_val, ma_slow_val, rsi_val, adx_val, macd_hist]):
            return Signal.HOLD
        
        # === 风控检查 ===
        if position and position.size > 0 and self.entry_price > 0:
            pnl_pct = (close - self.entry_price) / self.entry_price
            
            # 止损
            if pnl_pct <= self.stop_loss_pct:
                logger.info(f"🛑 止损: PnL={pnl_pct*100:.2f}% @ {close:.2f}")
                self.entry_price = 0
                return Signal.SELL
            
            # 止盈
            if pnl_pct >= self.take_profit_pct:
                logger.info(f"🎯 止盈: PnL={pnl_pct*100:.2f}% @ {close:.2f}")
                self.entry_price = 0
                return Signal.SELL
        
        # === 趋势过滤: ADX ===
        if adx_val < self.adx_threshold:
            return Signal.HOLD
        
        # === 买入信号 ===
        buy_condition = (
            ma_fast_val > ma_slow_val and      # MA金叉
            rsi_val < self.rsi_oversold and     # RSI超卖
            macd_hist > 0                       # MACD上升
        )
        
        # === 卖出信号 ===
        sell_condition = (
            ma_fast_val < ma_slow_val and       # MA死叉
            rsi_val > self.rsi_overbought and   # RSI超买
            macd_hist < 0                       # MACD下降
        )
        
        if buy_condition and (position is None or position.size == 0):
            self.entry_price = close
            logger.info(
                f"📈 买入信号: close={close:.2f} | RSI={rsi_val:.1f} | "
                f"ADX={adx_val:.1f} | MACD={macd_hist:.4f}"
            )
            return Signal.BUY
        
        elif sell_condition and position and position.size > 0:
            logger.info(
                f"📉 卖出信号: close={close:.2f} | RSI={rsi_val:.1f} | "
                f"ADX={adx_val:.1f} | MACD={macd_hist:.4f}"
            )
            self.entry_price = 0
            return Signal.SELL
        
        return Signal.HOLD
    
    # === 技术指标 (纯 Python 实现，不依赖 TA-Lib) ===
    
    @staticmethod
    def _sma(prices, period: int) -> Optional[float]:
        if len(prices) < period:
            return None
        return np.mean(prices[-period:])
    
    @staticmethod
    def _ema(prices, period: int) -> Optional[float]:
        if len(prices) < period:
            return None
        k = 2 / (period + 1)
        ema = prices[0]
        for p in prices[1:]:
            ema = p * k + ema * (1 - k)
        return ema
    
    @staticmethod
    def _rsi(prices, period: int = 14) -> Optional[float]:
        if len(prices) < period + 1:
            return None
        
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    @staticmethod
    def _macd_histogram(prices) -> Optional[float]:
        if len(prices) < 26:
            return None
        
        ema_fast = MATrendStrategy._ema(prices, 12)
        ema_slow = MATrendStrategy._ema(prices, 26)
        
        if ema_fast is None or ema_slow is None:
            return None
        
        macd = ema_fast - ema_slow
        # 简化 signal 计算
        signal = macd * 0.9
        hist = macd - signal
        
        return hist
    
    @staticmethod
    def _adx(highs, lows, closes, period: int = 14) -> Optional[float]:
        """简化 ADX 计算"""
        if len(closes) < period + 1:
            return None
        
        plus_dm = []
        minus_dm = []
        tr = []
        
        for i in range(1, len(closes)):
            h_diff = highs[i] - highs[i-1]
            l_diff = lows[i-1] - lows[i]
            
            plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
            minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
            
            tr.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1]),
            ))
        
        if len(tr) < period:
            return None
        
        atr = sum(tr[-period:]) / period
        if atr == 0:
            return 0
        
        plus_di = (sum(plus_dm[-period:]) / period) / atr * 100
        minus_di = (sum(minus_dm[-period:]) / period) / atr * 100
        
        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 0
        
        dx = abs(plus_di - minus_di) / di_sum * 100
        return dx