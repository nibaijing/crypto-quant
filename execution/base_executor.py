"""执行器统一抽象基类 — 定义所有执行器必须实现的标准接口"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, List, Any


class BaseExecutor(ABC):
    """执行器抽象基类

    统一接口保障:
      - 模拟盘 (FuturesExecutor) 和 实盘 (OKXExecutor) 可互换
      - run_live_ws.py / run_live_okx.py 共享同一套调用约定
      - 所有私有属性访问必须通过公开属性/方法暴露
    """

    # --- 持仓 & 账户 ---

    @property
    @abstractmethod
    def position(self) -> Optional[Any]:
        """当前持仓对象, None=空仓. 至少包含 .side/.size/.entry_price/.leverage/.unrealized_pnl_pct"""

    @property
    @abstractmethod
    def cash(self) -> float:
        """可用余额 (不含保证金和浮动盈亏)"""

    @property
    @abstractmethod
    def equity(self) -> float:
        """总权益 = 可用余额 + 保证金 + 浮动盈亏"""

    @property
    @abstractmethod
    def leverage(self) -> int:
        """当前杠杆倍数"""

    @property
    @abstractmethod
    def total_trades(self) -> int:
        """累计交易次数"""

    @property
    @abstractmethod
    def winning_trades(self) -> int:
        """盈利交易次数"""

    # --- 设置 ---

    @abstractmethod
    def set_leverage(self, lev: int):
        """设置杠杆倍数"""

    # --- 价格更新 ---

    @abstractmethod
    def update_price(self, symbol: str, price: float):
        """更新持仓价格, 重新计算浮动盈亏"""

    @abstractmethod
    def update_bars_held(self):
        """持仓 K 线计数 +1"""

    # --- 交易操作 ---

    @abstractmethod
    def buy(self, symbol: str, size: float = None, price: float = None) -> Optional[str]:
        """开多仓 / 加多仓, 返回订单ID或None"""

    @abstractmethod
    def sell(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """平多仓, 返回订单ID或None"""

    @abstractmethod
    def short_sell(self, symbol: str, size: float = None, price: float = None) -> Optional[str]:
        """开空仓, 返回订单ID或None"""

    @abstractmethod
    def short_cover(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """平空仓, 返回订单ID或None"""

    # --- 状态持久化 ---

    @abstractmethod
    def save_state(self):
        """持久化当前状态到文件"""



    # --- 行情 & 账户 ---

    @abstractmethod
    def get_account(self) -> Any:
        """获取账户快照"""

    @abstractmethod
    def get_ticker(self, symbol: str) -> Optional[Dict]:
        """获取当前行情 {symbol, price, timestamp}"""

    @abstractmethod
    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> List[Dict]:
        """获取K线 [{timestamp, open, high, low, close, volume}]"""
