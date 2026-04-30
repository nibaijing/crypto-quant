"""信号处理器和执行器"""

import logging
from typing import Optional

from strategies.base import Signal

logger = logging.getLogger(__name__)


def default_signal_handler(signal: str, bar_data: dict, engine):
    """默认信号处理器 - 将策略信号转为买卖操作
    
    在回测和实盘中复用，确保行为一致。
    
    现货信号:
        BUY  → engine.buy()
        SELL → engine.sell()
    
    合约信号 (额外支持):
        SHORT → engine.short_sell()
        COVER → engine.short_cover()
    """
    close = bar_data["close"]
    
    # 现货买卖
    if signal == Signal.BUY:
        engine.buy()
    
    elif signal == Signal.SELL:
        engine.sell()
    
    # 合约做空
    elif signal == Signal.SHORT:
        engine.short_sell()
    
    elif signal == Signal.COVER:
        engine.short_cover()
    
    else:
        logger.debug(f"未知信号: {signal}")


class SpotExecutor:
    """现货执行器 (实盘用)"""
    pass


class FuturesExecutor:
    """合约执行器 (实盘用)"""
    pass