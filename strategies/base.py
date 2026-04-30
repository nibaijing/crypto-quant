"""策略基类 - 所有策略的抽象接口

回测和实盘共享同一套策略代码，只通过不同的执行层运行。
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class Strategy(ABC):
    """策略基类
    
    子类只需实现 on_bar() 方法，返回信号即可。
    
    支持的信号:
        - Signal.BUY  : 做多 / 买入
        - Signal.SELL : 平多 / 卖出
        - Signal.SHORT: 做空 (合约)
        - Signal.COVER: 平空 (合约)
        - None        : 无操作
    """
    
    def __init__(self, name: str = "BaseStrategy"):
        self.name = name
        self._bar_count: int = 0
    
    @abstractmethod
    def on_bar(self, bar_data: Dict, engine) -> Optional[str]:
        """处理每一根K线
        
        Args:
            bar_data: {
                "timestamp": int (ms),
                "open": float,
                "high": float,
                "low": float,
                "close": float,
                "volume": float,
                "history": pd.DataFrame (过去500根K线),
                "position": SimPosition (当前持仓),
                "ma_7": float,  # 预计算的指标 (如果可用)
                "ma_25": float,
                "rsi": float,
                ... 
            }
            engine: BacktestEngine 或实盘 Executor
        
        Returns:
            信号字符串 或 None
        """
        pass
    
    def init(self, bar_data: Dict):
        """策略初始化 (在第一个 bar 时调用)"""
        pass
    
    @property
    def signal(self):
        """延迟导入 Signal 避免循环引用"""
        from execution.signals import Signal
        return Signal


# 信号常量
class Signal:
    BUY = "BUY"
    SELL = "SELL"
    SHORT = "SHORT"
    COVER = "COVER"
    HOLD = None