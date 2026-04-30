"""合约趋势策略框架 - 支持多空双向 + 杠杆

合约与现货的核心差异:
1. 可以做空 (short)
2. 杠杆放大收益和风险
3. 资金费率影响持仓成本
4. 强平风险

此策略作为框架模板，后续可在此基础上迭代具体策略逻辑。

当前实现:
- 简单的双均线趋势跟踪 (做多/做空)
- 动态杠杆调整 (基于波动率)
- 资金费率回避 (费率过高时减仓或不开仓)
"""

import logging
from typing import Optional, Dict

import numpy as np
import pandas as pd

from strategies.base import Strategy, Signal

logger = logging.getLogger(__name__)


class FuturesTrendStrategy(Strategy):
    """合约趋势跟踪策略 (多空双向)
    
    信号逻辑:
    - MA7 > MA25 且价格 > MA99: 做多
    - MA7 < MA25 且价格 < MA99: 做空
    - 否则: 平仓
    
    风控:
    - 波动率越高, 杠杆越低
    - 资金费率 > 0.1% 时不开新仓
    - 固定止损 3% (杠杆后)
    """
    
    def __init__(self,
                 ma_fast: int = 7,
                 ma_slow: int = 25,
                 ma_trend: int = 99,
                 volatility_window: int = 20,
                 max_leverage: int = 10,
                 base_leverage: int = 3,
                 funding_threshold: float = 0.001,
                 stop_loss_ratio: float = 0.03,
                 **kwargs):
        super().__init__(name=f"FuturesTrend({ma_fast}/{ma_slow}/{ma_trend})")
        
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.ma_trend = ma_trend
        self.volatility_window = volatility_window
        self.max_leverage = max_leverage
        self.base_leverage = base_leverage
        self.funding_threshold = funding_threshold
        self.stop_loss_ratio = stop_loss_ratio
        
        self.entry_price: float = 0
        self.current_side: Optional[str] = None  # long/short/None
    
    def on_bar(self, bar_data: Dict, engine) -> Optional[str]:
        close = bar_data["close"]
        history = bar_data.get("history")
        position = bar_data.get("position")
        
        if history is None or len(history) < self.ma_trend:
            return Signal.HOLD
        
        closes = history["close"].values
        
        # 计算均线
        ma_fast_val = np.mean(closes[-self.ma_fast:])
        ma_slow_val = np.mean(closes[-self.ma_slow:])
        ma_trend_val = np.mean(closes[-self.ma_trend:])
        
        if any(pd.isna(v) for v in [ma_fast_val, ma_slow_val, ma_trend_val]):
            return Signal.HOLD
        
        # 计算波动率 (决定杠杆)
        returns = np.diff(closes[-self.volatility_window:]) / closes[-self.volatility_window:-1]
        volatility = np.std(returns) if len(returns) > 1 else 0
        
        # 动态杠杆: 波动率越高, 杠杆越低
        if volatility > 0:
            suggested_leverage = min(
                int(self.base_leverage / (volatility * 100)),
                self.max_leverage,
            )
        else:
            suggested_leverage = self.base_leverage
        
        # 检查资金费率 (从 bar_data 获取, 回测中可能为模拟值)
        funding_rate = bar_data.get("funding_rate", 0)
        
        # === 风控: 止损检查 ===
        if position and position.size > 0 and self.entry_price > 0:
            if self.current_side == "long":
                pnl_ratio = (close - self.entry_price) / self.entry_price
            elif self.current_side == "short":
                pnl_ratio = (self.entry_price - close) / self.entry_price
            else:
                pnl_ratio = 0
            
            # 杠杆放大后的实际亏损
            effective_pnl = pnl_ratio * position.leverage
            
            if effective_pnl <= -self.stop_loss_ratio:
                logger.info(f"🛑 合约止损: PnL={pnl_ratio*100:.2f}% | lev={position.leverage}x")
                self.entry_price = 0
                self.current_side = None
                
                if self.current_side == "long":
                    return Signal.SELL
                else:
                    return Signal.COVER
        
        # === 信号生成 ===
        
        # 做多条件: 短期趋势向上, 价格在长期均线上方
        long_condition = (
            ma_fast_val > ma_slow_val and
            close > ma_trend_val
        )
        
        # 做空条件: 短期趋势向下, 价格在长期均线下方
        short_condition = (
            ma_fast_val < ma_slow_val and
            close < ma_trend_val
        )
        
        # 资金费率过高时不交易
        if abs(funding_rate) > self.funding_threshold:
            logger.debug(f"资金费率过高 ({funding_rate:.4%}), 跳过交易")
            return Signal.HOLD
        
        # 多空信号
        if long_condition:
            if position and position.size > 0 and self.current_side == "short":
                # 空转多: 先平空
                self.entry_price = close
                self.current_side = "long"
                return Signal.COVER  # 触发平空+做多
            
            if position is None or position.size == 0:
                self.entry_price = close
                self.current_side = "long"
                # 设置杠杆 (通过 engine.set_leverage())
                engine.set_leverage(suggested_leverage)
                return Signal.BUY
        
        elif short_condition:
            if position and position.size > 0 and self.current_side == "long":
                self.entry_price = close
                self.current_side = "short"
                return Signal.SELL  # 触发平多+做空
            
            if position is None or position.size == 0:
                self.entry_price = close
                self.current_side = "short"
                engine.set_leverage(suggested_leverage)
                return Signal.SHORT
        
        else:
            # 趋势不明: 平仓观望
            if position and position.size > 0:
                self.entry_price = 0
                self.current_side = None
                if self.current_side == "long":
                    return Signal.SELL
                else:
                    return Signal.COVER
        
        return Signal.HOLD


