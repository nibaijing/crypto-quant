"""优化版 MA 趋势策略 — V2

相比 V1 改动:
1. K线周期: 15m (从1H缩短，频率提升4倍)
2. RSI阈值: 放宽 35→40 (超卖) / 65→60 (超买)
3. 去掉 ADX 过滤 (减少一个限制条件)
4. 新增: 动态 ATR 止损 (基于波动率自适应，固定-3%太死板)
5. 新增: 均线斜率确认 (MA7方向必须向上才做多)
6. 新增: 最小持仓周期 (至少持有4根K线=1小时，避免噪音交易)
"""

import logging
from typing import Optional, Dict, Any

import numpy as np
import pandas as pd

from strategies.base import Strategy, Signal

logger = logging.getLogger(__name__)


class MATrendStrategyV2(Strategy):
    """MA 趋势策略 V2 — 优化版 (15m K线)
    
    参数:
        ma_fast (7):   快线周期
        ma_slow (25):  慢线周期  
        rsi_period (14): RSI 周期
        rsi_oversold (40):  放宽超卖阈值 (V1=35)
        rsi_overbought (60): 放宽超买阈值 (V1=65)
        macd_fast/slow/signal: MACD 参数
        atr_period (14): ATR 周期 (动态止损用)
        atr_stop_mult (2.0): ATR 止损倍数
        min_bars_hold (4): 最小持仓K线数 (4*15m=1小时)
        ma_slope_period (3): MA斜率确认周期
        use_adx (False): 是否启用ADX过滤 (V2默认关闭)
    """
    
    def __init__(self,
                 ma_fast: int = 7,
                 ma_slow: int = 25,
                 rsi_period: int = 14,
                 rsi_oversold: float = 40,
                 rsi_overbought: float = 60,
                 macd_fast: int = 12,
                 macd_slow: int = 26,
                 macd_signal: int = 9,
                 atr_period: int = 14,
                 atr_stop_mult: float = 2.0,
                 min_bars_hold: int = 4,
                 ma_slope_period: int = 3,
                 use_adx: bool = False,
                 adx_threshold: float = 25,
                 **kwargs):
        
        super().__init__(name=f"MATrendV2({ma_fast}/{ma_slow})")
        
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.min_bars_hold = min_bars_hold
        self.ma_slope_period = ma_slope_period
        self.use_adx = use_adx
        self.adx_threshold = adx_threshold
        
        # 追踪状态
        self.entry_price: float = 0
        self.entry_atr: float = 0       # 开仓时的ATR (动态止损基准)
        self.bars_held: int = 0         # 当前持仓已持K线数
        self._prev_close: float = 0
    
    def on_bar(self, bar_data: Dict, engine) -> Optional[str]:
        close = bar_data["close"]
        history = bar_data.get("history")
        position = bar_data.get("position")
        
        if history is None or len(history) < max(self.ma_slow, self.atr_period + 1):
            return Signal.HOLD
        
        closes = history["close"].values
        highs = history["high"].values
        lows = history["low"].values
        
        # === 计算指标 ===
        ma_fast_val = np.mean(closes[-self.ma_fast:])
        ma_slow_val = np.mean(closes[-self.ma_slow:])
        rsi_val = self._rsi(closes, self.rsi_period)
        macd_hist = self._macd_histogram(closes)
        atr_val = self._atr(highs, lows, closes, self.atr_period)
        
        # MA7 斜率 (上升=正, 下降=负)
        ma_slope = 0
        if len(closes) >= self.ma_fast + self.ma_slope_period:
            ma_fast_prev = np.mean(closes[-(self.ma_fast + self.ma_slope_period):-self.ma_slope_period])
            ma_slope = (ma_fast_val - ma_fast_prev) / ma_fast_prev * 100
        
        # === 数据有效性检查 ===
        if any(v is None for v in [ma_fast_val, ma_slow_val, rsi_val, macd_hist, atr_val]):
            return Signal.HOLD
        
        # === 持仓状态管理 ===
        in_position = position is not None and position.size > 0
        
        if in_position:
            self.bars_held += 1
        
        # === 风控: 动态 ATR 止损 ===
        if in_position and self.entry_price > 0 and self.entry_atr > 0:
            stop_price = self.entry_price - (self.entry_atr * self.atr_stop_mult)
            if close <= stop_price:
                logger.info(
                    f"🛑 ATR止损: close={close:.2f} ≤ stop={stop_price:.2f} "
                    f"(entry={self.entry_price:.2f}, ATR={self.entry_atr:.2f})"
                )
                self._reset_state()
                return Signal.SELL
        
        # === 可选: ADX 趋势过滤 ===
        if self.use_adx:
            adx_val = self._adx(highs, lows, closes)
            if adx_val and adx_val < self.adx_threshold:
                return Signal.HOLD
        
        # === 买入信号 ===
        buy_condition = (
            ma_fast_val > ma_slow_val and                    # MA金叉
            rsi_val < self.rsi_oversold and                  # RSI超卖 (放宽到40)
            macd_hist > 0 and                                 # MACD上升
            ma_slope > 0                                      # MA7方向向上 (新增)
        )
        
        # === 卖出信号 ===
        sell_condition = (
            ma_fast_val < ma_slow_val and                    # MA死叉
            rsi_val > self.rsi_overbought and                # RSI超买 (放宽到60)
            macd_hist < 0                                     # MACD下降
        )
        
        # === 执行 ===
        if buy_condition and not in_position:
            self.entry_price = close
            self.entry_atr = atr_val
            self.bars_held = 0
            
            logger.info(
                f"📈 买入: ${close:.2f} | RSI={rsi_val:.1f} | "
                f"MA斜率={ma_slope:.2f}% | ATR={atr_val:.2f} | MACD={macd_hist:.4f}"
            )
            return Signal.BUY
        
        elif sell_condition and in_position:
            # 最小持仓周期检查 (防止噪音交易)
            if self.bars_held < self.min_bars_hold:
                logger.debug(f"未达最小持仓周期: {self.bars_held}/{self.min_bars_hold}")
                return Signal.HOLD
            
            pnl_pct = (close - self.entry_price) / self.entry_price * 100
            logger.info(
                f"📉 卖出: ${close:.2f} | PnL={pnl_pct:.2f}% | "
                f"持仓: {self.bars_held}根K线 | RSI={rsi_val:.1f}"
            )
            self._reset_state()
            return Signal.SELL
        
        return Signal.HOLD
    
    def _reset_state(self):
        self.entry_price = 0
        self.entry_atr = 0
        self.bars_held = 0
    
    # === 技术指标 ===
    
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
        return 100 - (100 / (1 + avg_gain / avg_loss))
    
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
    def _macd_histogram(prices) -> Optional[float]:
        if len(prices) < 26:
            return None
        ema_fast = MATrendStrategyV2._ema(prices, 12)
        ema_slow = MATrendStrategyV2._ema(prices, 26)
        if ema_fast is None or ema_slow is None:
            return None
        macd = ema_fast - ema_slow
        signal = macd * 0.9
        return macd - signal
    
    @staticmethod
    def _atr(highs, lows, closes, period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1]),
            )
            trs.append(tr)
        if len(trs) < period:
            return None
        return np.mean(trs[-period:])
    
    @staticmethod
    def _adx(highs, lows, closes, period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        plus_dm, minus_dm, tr = [], [], []
        for i in range(1, len(closes)):
            h_diff = highs[i] - highs[i-1]
            l_diff = lows[i-1] - lows[i]
            plus_dm.append(h_diff if h_diff > l_diff and h_diff > 0 else 0)
            minus_dm.append(l_diff if l_diff > h_diff and l_diff > 0 else 0)
            tr.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
        if len(tr) < period:
            return None
        atr = sum(tr[-period:]) / period
        if atr == 0:
            return 0
        plus_di = (sum(plus_dm[-period:]) / period) / atr * 100
        minus_di = (sum(minus_dm[-period:]) / period) / atr * 100
        di_sum = plus_di + minus_di
        return abs(plus_di - minus_di) / di_sum * 100 if di_sum > 0 else 0