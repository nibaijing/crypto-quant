"""实盘执行器 - 模拟盘模式

核心设计:
1. 模拟盘 = 使用 Binance Testnet API (现货) + 本地虚拟账户 (合约)
2. 策略信号通过 on_bar() 返回，执行器负责转化为实际下单
3. 所有成交、余额变动记录到本地数据库
4. 支持 Telegram 实时推送交易通知

为什么 Binance Testnet:
- OKX API 在国内被墙
- Binance Testnet 有完整的现货模拟环境
- 合约部分本地虚拟 (Binance Testnet 期货不太稳定)
"""

import os
import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum

import requests
import pandas as pd

from core.config import get_config

logger = logging.getLogger(__name__)


# ===== Binance Testnet 配置 =====
BINANCE_TESTNET_REST = "https://testnet.binance.vision/api"
BINANCE_TESTNET_WS = "wss://testnet.binance.vision/ws"

# Testnet 默认 API Key (公开的，可以注册自己的: https://testnet.binance.vision/)
BINANCE_TESTNET_API_KEY = os.getenv("BINANCE_TESTNET_API_KEY", "")
BINANCE_TESTNET_SECRET = os.getenv("BINANCE_TESTNET_SECRET", "")


@dataclass
class LivePosition:
    """实盘持仓快照"""
    symbol: str
    side: str           # long/short/net
    size: float         # 持仓量
    entry_price: float  # 开仓均价
    current_price: float # 当前价格
    unrealized_pnl: float
    unrealized_pnl_pct: float
    leverage: int = 1
    margin: float = 0
    timestamp: int = 0


@dataclass  
class LiveOrder:
    """下单记录"""
    order_id: str
    symbol: str
    side: str
    type: str  # market/limit
    size: float
    price: float
    status: str
    timestamp: int


@dataclass
class LiveAccount:
    """账户快照"""
    total_equity: float
    available_balance: float
    unrealized_pnl: float
    total_trades: int
    winning_trades: int
    win_rate: float
    positions: List[LivePosition] = field(default_factory=list)
    recent_trades: List[LiveOrder] = field(default_factory=list)


class SpotExecutor:
    """现货执行器 - Binance Testnet
    
    支持:
    - 余额查询
    - 市价/限价下单
    - 订单状态查询
    - 模拟盘模式下订单即时成交
    """
    
    def __init__(self, api_key: str = None, secret: str = None, sandbox: bool = True):
        self.config = get_config()
        self.sandbox = sandbox
        self.base_url = BINANCE_TESTNET_REST if sandbox else "https://api.binance.com/api"
        
        self.api_key = api_key or BINANCE_TESTNET_API_KEY
        self.api_secret = secret or BINANCE_TESTNET_SECRET
        
        # 模拟盘本地状态 (无需真实API Key)
        self._sim_cash = float(self.config.backtest.initial_capital)
        self._sim_position: Optional[LivePosition] = None
        self._sim_trades: List[LiveOrder] = []
        self._sim_total_trades = 0
        self._sim_winning = 0
        
        # 状态文件
        self.state_file = self.config.project_root / "data" / "live_state.json"
        self._load_state()
        
        if self.api_key:
            logger.info(f"现货执行器初始化 | {'模拟盘' if sandbox else '实盘'} | API Key已配置")
        else:
            logger.info(f"现货执行器初始化 | 本地模拟模式 (无API Key)")
    
    @property
    def position(self) -> Optional[LivePosition]:
        return self._sim_position
    
    @property
    def cash(self) -> float:
        return self._sim_cash
    
    @property
    def equity(self) -> float:
        if self._sim_position and self._sim_position.size > 0:
            return self._sim_cash + self._sim_position.size * self._sim_position.current_price
        return self._sim_cash
    
    @property
    def total_trades(self) -> int:
        return self._sim_total_trades
    
    @property
    def winning_trades(self) -> int:
        return self._sim_winning
    
    def _load_state(self):
        """加载持久化状态"""
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    state = json.load(f)
                self._sim_cash = state.get("cash", self._sim_cash)
                self._sim_total_trades = state.get("total_trades", 0)
                self._sim_winning = state.get("winning_trades", 0)
                logger.info(f"状态已加载: cash={self._sim_cash:.2f}, trades={self._sim_total_trades}")
            except Exception as e:
                logger.warning(f"状态加载失败: {e}")
    
    def _save_state(self):
        """保存状态到文件"""
        state = {
            "cash": self._sim_cash,
            "total_trades": self._sim_total_trades,
            "winning_trades": self._sim_winning,
            "updated_at": datetime.now().isoformat(),
        }
        if self._sim_position and self._sim_position.size > 0:
            state["position"] = {
                "symbol": self._sim_position.symbol,
                "side": self._sim_position.side,
                "size": self._sim_position.size,
                "entry_price": self._sim_position.entry_price,
            }
        
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)
    
    def get_account(self) -> LiveAccount:
        """获取账户快照"""
        positions = []
        if self._sim_position and self._sim_position.size > 0:
            positions.append(self._sim_position)
        
        win_rate = (self._sim_winning / max(self._sim_total_trades, 1)) * 100
        
        return LiveAccount(
            total_equity=self.equity,
            available_balance=self._sim_cash,
            unrealized_pnl=self._sim_position.unrealized_pnl if self._sim_position else 0,
            total_trades=self._sim_total_trades,
            winning_trades=self._sim_winning,
            win_rate=win_rate,
            positions=positions,
            recent_trades=self._sim_trades[-10:],
        )
    
    def update_price(self, symbol: str, price: float):
        """更新当前价格 (每次行情推送时调用)"""
        if self._sim_position and self._sim_position.size > 0:
            self._sim_position.current_price = price
            if self._sim_position.side == "long":
                self._sim_position.unrealized_pnl = (price - self._sim_position.entry_price) * self._sim_position.size
            else:
                self._sim_position.unrealized_pnl = (self._sim_position.entry_price - price) * self._sim_position.size
            
            if self._sim_position.entry_price > 0:
                self._sim_position.unrealized_pnl_pct = self._sim_position.unrealized_pnl / (self._sim_position.size * self._sim_position.entry_price) * 100
    
    def buy(self, symbol: str, size: float = None, price: float = None,
            stop_loss_pct: float = -3, take_profit_pct: float = 10) -> Optional[str]:
        """买入 (模拟)"""
        
        # 计算买入金额
        if size is None:
            max_size = (self._sim_cash * 0.95) / price
            size = max_size * self.config.risk.max_total_position_pct
        
        cost = size * price
        commission = cost * 0.0005  # 模拟 0.05% 手续费
        
        if cost > self._sim_cash:
            logger.warning(f"资金不足: 需要 {cost:.2f}, 可用 {self._sim_cash:.2f}")
            return None
        
        # 扣款
        self._sim_cash -= (cost + commission)
        
        # 更新持仓
        self._sim_position = LivePosition(
            symbol=symbol,
            side="long",
            size=size,
            entry_price=price,
            current_price=price,
            unrealized_pnl=0,
            unrealized_pnl_pct=0,
            leverage=1,
            margin=cost,
            timestamp=int(time.time() * 1000),
        )
        
        # 记录交易
        order_id = f"sim_{int(time.time() * 1000)}"
        order = LiveOrder(
            order_id=order_id,
            symbol=symbol,
            side="BUY",
            type="MARKET",
            size=size,
            price=price,
            status="FILLED",
            timestamp=int(time.time() * 1000),
        )
        self._sim_trades.append(order)
        self._sim_total_trades += 1
        
        self._save_state()
        logger.info(f"✅ 买入: {size:.4f} {symbol} @ {price:.2f} | 手续费: {commission:.2f}")
        
        return order_id
    
    def sell(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """卖出 / 平仓 (模拟)"""
        
        if not self._sim_position or self._sim_position.size <= 0:
            logger.warning("无持仓可卖")
            return None
        
        sell_size = size or self._sim_position.size
        if sell_size > self._sim_position.size:
            sell_size = self._sim_position.size
        
        revenue = sell_size * price
        commission = revenue * 0.0005
        
        # 计算盈亏
        pnl = (price - self._sim_position.entry_price) * sell_size
        
        # 回笼资金
        self._sim_cash += (revenue - commission)
        
        if pnl > 0:
            self._sim_winning += 1
        
        # 记录交易
        order_id = f"sim_{int(time.time() * 1000)}"
        order = LiveOrder(
            order_id=order_id,
            symbol=symbol,
            side="SELL",
            type="MARKET",
            size=sell_size,
            price=price,
            status="FILLED",
            timestamp=int(time.time() * 1000),
        )
        self._sim_trades.append(order)
        
        logger.info(f"✅ 卖出: {sell_size:.4f} {symbol} @ {price:.2f} | "
                     f"盈亏: {pnl:.2f} ({pnl/self._sim_position.entry_price/sell_size*100:.2f}%) "
                     f"| 手续费: {commission:.2f}")
        
        # 清仓
        self._sim_position = None
        self._save_state()
        
        return order_id
    
    def short_sell(self, symbol: str, size: float = None, price: float = None) -> Optional[str]:
        """做空 (模拟盘 - 记录空头仓位)
        
        模拟盘做空逻辑: 记录一笔"借币卖出"的持仓，价格下跌赚差价。
        """
        if self._sim_position and self._sim_position.size > 0:
            logger.warning("已有仓位，无法开空")
            return None
        
        if size is None:
            max_size = (self._sim_cash * 0.5) / price
            size = max_size
        
        cost = size * price
        commission = cost * 0.0005
        
        self._sim_position = LivePosition(
            symbol=symbol,
            side="short",
            size=size,
            entry_price=price,
            current_price=price,
            unrealized_pnl=0,
            unrealized_pnl_pct=0,
            leverage=1,
            margin=cost,
            timestamp=int(time.time() * 1000),
        )
        
        order_id = f"sim_short_{int(time.time() * 1000)}"
        order = LiveOrder(
            order_id=order_id, symbol=symbol, side="SHORT",
            type="MARKET", size=size, price=price,
            status="FILLED", timestamp=int(time.time() * 1000),
        )
        self._sim_trades.append(order)
        self._sim_total_trades += 1
        
        self._save_state()
        logger.info(f"🔻 做空: {size:.4f} {symbol} @ {price:.2f}")
        return order_id
    
    def short_cover(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """平空 (模拟盘)"""
        if not self._sim_position or self._sim_position.size <= 0:
            return None
        if self._sim_position.side != "short":
            return None
        
        cover_size = size or self._sim_position.size
        pnl = (self._sim_position.entry_price - price) * cover_size
        commission = cover_size * price * 0.0005
        
        if pnl > 0:
            self._sim_winning += 1
        
        # 实现盈亏到现金
        self._sim_cash += pnl - commission
        
        order_id = f"sim_cover_{int(time.time() * 1000)}"
        order = LiveOrder(
            order_id=order_id, symbol=symbol, side="COVER",
            type="MARKET", size=cover_size, price=price,
            status="FILLED", timestamp=int(time.time() * 1000),
        )
        self._sim_trades.append(order)
        
        logger.info(f"✅ 平空: {cover_size:.4f} @ {price:.2f} | 盈亏: {pnl:.2f}")
        self._sim_position = None
        self._save_state()
        return order_id
    
    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> List[Dict]:
        """拉取K线 (真实API - Binance公开接口)"""
        try:
            # 使用真实 Binance API (不需要认证)
            binance_symbol = symbol.replace("-", "")
            url = f"https://api.binance.com/api/v3/klines"
            params = {
                "symbol": binance_symbol,
                "interval": interval,
                "limit": min(limit, 1000),
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            
            data = resp.json()
            klines = []
            for item in data:
                klines.append({
                    "timestamp": item[0],
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                })
            return klines
        except Exception as e:
            logger.error(f"K线获取失败: {e}")
            return []
    
    def get_ticker(self, symbol: str) -> Optional[Dict]:
        """获取实时行情"""
        try:
            binance_symbol = symbol.replace("-", "")
            url = f"https://api.binance.com/api/v3/ticker/price"
            params = {"symbol": binance_symbol}
            resp = requests.get(url, params=params, timeout=5)
            data = resp.json()
            return {
                "symbol": symbol,
                "price": float(data["price"]),
                "timestamp": int(time.time() * 1000),
            }
        except Exception as e:
            logger.error(f"行情获取失败: {e}")
            return None


class FuturesExecutor:
    """合约执行器 - 本地虚拟 (后续接 OKX 实盘)
    
    当前: 本地模拟 (逻辑与 SpotExecutor 一致, 额外支持做空和杠杆)
    后续: 替换为 OKX API 调用
    """
    
    def __init__(self):
        self.config = get_config()
        self._sim_cash = float(self.config.backtest.initial_capital)
        self._sim_position: Optional[LivePosition] = None
        self._sim_leverage = self.config.futures.default_leverage
        self._sim_total_trades = 0
        self._sim_winning = 0
        self._sim_trades: List[LiveOrder] = []
        self.state_file = self.config.project_root / "data" / "live_futures_state.json"
        self._load_state()
        logger.info(f"合约执行器初始化 (本地模拟) | 杠杆: {self._sim_leverage}x")
    
    def _load_state(self):
        if self.state_file.exists():
            try:
                import json
                with open(self.state_file) as f:
                    state = json.load(f)
                # 仅当 cash 正常时才加载 (避免脏数据污染)
                loaded_cash = state.get("cash", 0)
                if loaded_cash >= 10:  # sanity check (min $10)
                    self._sim_cash = loaded_cash
                    self._sim_total_trades = state.get("total_trades", 0)
                    self._sim_winning = state.get("winning_trades", 0)
                else:
                    logger.warning(f"状态文件 cash={loaded_cash} 异常，使用默认值")
            except Exception as e:
                logger.warning(f"状态加载失败: {e}")

    def _save_state(self):
        import json
        from datetime import datetime
        state = {
            "cash": self._sim_cash,
            "total_trades": self._sim_total_trades,
            "winning_trades": self._sim_winning,
            "updated_at": datetime.now().isoformat(),
        }
        if self._sim_position and self._sim_position.size > 0:
            state["position"] = {
                "symbol": self._sim_position.symbol,
                "side": self._sim_position.side,
                "size": self._sim_position.size,
                "entry_price": self._sim_position.entry_price,
                "leverage": self._sim_position.leverage,
            }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)
    
    @property
    def position(self) -> Optional[LivePosition]:
        return self._sim_position
    
    @property
    def cash(self) -> float:
        return self._sim_cash

    @property
    def total_trades(self) -> int:
        return self._sim_total_trades

    @property
    def winning_trades(self) -> int:
        return self._sim_winning
    
    @property
    def equity(self) -> float:
        if self._sim_position and self._sim_position.size > 0:
            return self._sim_cash + self._sim_position.margin + self._sim_position.unrealized_pnl
        return self._sim_cash
    
    def set_leverage(self, lev: int):
        self._sim_leverage = min(lev, self.config.futures.max_leverage)
    
    def update_price(self, symbol: str, price: float):
        if self._sim_position and self._sim_position.size > 0:
            self._sim_position.current_price = price
            if self._sim_position.side == "long":
                self._sim_position.unrealized_pnl = (price - self._sim_position.entry_price) * self._sim_position.size
            else:
                self._sim_position.unrealized_pnl = (self._sim_position.entry_price - price) * self._sim_position.size
            
            if self._sim_position.entry_price > 0:
                self._sim_position.unrealized_pnl_pct = self._sim_position.unrealized_pnl / (self._sim_position.size * self._sim_position.entry_price) * self._sim_leverage * 100
    
    def get_ticker(self, symbol: str) -> Optional[Dict]:
        """获取实时行情"""
        try:
            import requests, time as _time
            binance_symbol = symbol.replace("-", "")
            url = f"https://api.binance.com/api/v3/ticker/price"
            params = {"symbol": binance_symbol}
            resp = requests.get(url, params=params, timeout=5)
            data = resp.json()
            return {
                "symbol": symbol,
                "price": float(data["price"]),
                "timestamp": int(_time.time() * 1000),
            }
        except Exception as e:
            logger.error(f"行情获取失败: {e}")
            return None

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> List[Dict]:
        """拉取K线"""
        try:
            import requests
            binance_symbol = symbol.replace("-", "")
            url = f"https://api.binance.com/api/v3/klines"
            params = {
                "symbol": binance_symbol,
                "interval": interval,
                "limit": min(limit, 1000),
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            klines = []
            for item in data:
                klines.append({
                    "timestamp": item[0],
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                })
            return klines
        except Exception as e:
            logger.error(f"K线获取失败: {e}")
            return []
    
    def get_account(self) -> LiveAccount:
        """获取账户快照"""
        positions = []
        if self._sim_position and self._sim_position.size > 0:
            positions.append(self._sim_position)
        win_rate = (self._sim_winning / max(self._sim_total_trades, 1)) * 100
        return LiveAccount(
            total_equity=self.equity,
            available_balance=self._sim_cash,
            unrealized_pnl=self._sim_position.unrealized_pnl if self._sim_position else 0,
            total_trades=self._sim_total_trades,
            winning_trades=self._sim_winning,
            win_rate=win_rate,
            positions=positions,
            recent_trades=self._sim_trades[-10:],
        )
    
    def buy(self, symbol: str, size: float = None, price: float = None) -> Optional[str]:
        """做多"""
        if self._sim_position and self._sim_position.side == "short":
            self.short_cover(symbol, price)
        
        if size is None:
            max_pos = (self._sim_cash * self._sim_leverage * 0.9) / price
            size = max_pos
        
        cost = size * price
        margin = cost / self._sim_leverage
        
        if margin > self._sim_cash:
            return None
        
        self._sim_cash -= margin
        self._sim_total_trades += 1
        
        commission = cost * 0.0004
        self._sim_cash -= commission
        
        self._sim_position = LivePosition(
            symbol=symbol, side="long", size=size,
            entry_price=price, current_price=price,
            unrealized_pnl=0, unrealized_pnl_pct=0,
            leverage=self._sim_leverage, margin=margin,
            timestamp=int(time.time() * 1000),
        )
        self._save_state()
        logger.info(f"✅ 合约做多: {size:.4f} {symbol} @ {price:.2f} | {self._sim_leverage}x | 保证金=${margin:.2f} | 手续费=${commission:.2f}")
        return f"sim_futures_{int(time.time() * 1000)}"
    
    def short_sell(self, symbol: str, size: float = None, price: float = None) -> Optional[str]:
        """做空"""
        if self._sim_position and self._sim_position.side == "long":
            self.sell(symbol, price)
        
        if size is None:
            max_pos = (self._sim_cash * self._sim_leverage * 0.9) / price
            size = max_pos
        
        cost = size * price
        margin = cost / self._sim_leverage
        
        if margin > self._sim_cash:
            return None
        
        self._sim_cash -= margin
        self._sim_total_trades += 1
        
        commission = cost * 0.0004  # 0.04% taker fee
        self._sim_cash -= commission
        
        self._sim_position = LivePosition(
            symbol=symbol, side="short", size=size,
            entry_price=price, current_price=price,
            unrealized_pnl=0, unrealized_pnl_pct=0,
            leverage=self._sim_leverage, margin=margin,
            timestamp=int(time.time() * 1000),
        )
        self._save_state()
        logger.info(f"🔻 合约做空: {size:.4f} {symbol} @ {price:.2f} | {self._sim_leverage}x | 保证金=${margin:.2f} | 手续费=${commission:.2f}")
        return f"sim_futures_{int(time.time() * 1000)}"
    
    def short_cover(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """平空"""
        if not self._sim_position or self._sim_position.size <= 0:
            return None
        if self._sim_position.side != "short":
            return None
        
        cover_size = size or self._sim_position.size
        if cover_size > self._sim_position.size:
            cover_size = self._sim_position.size
        
        pnl = (self._sim_position.entry_price - price) * cover_size
        commission = cover_size * price * 0.0004  # 0.04% taker fee
        net_pnl = pnl - commission
        
        self._sim_cash += self._sim_position.margin + net_pnl
        if pnl > 0:
            self._sim_winning += 1
        
        logger.info(f"✅ 合约平空: {cover_size:.4f} @ {price:.2f} | PnL=${pnl:+.2f} | 手续费=${commission:.2f} | 净利=${net_pnl:+.2f} | 余额=${self._sim_cash:.2f}")
        self._sim_position = None
        self._save_state()
        return f"sim_futures_cover_{int(time.time() * 1000)}"
    
    def sell(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """平仓"""
        if not self._sim_position or self._sim_position.size <= 0:
            return None
        
        sell_size = size or self._sim_position.size
        if sell_size > self._sim_position.size:
            sell_size = self._sim_position.size
        
        if self._sim_position.side == "long":
            pnl = (price - self._sim_position.entry_price) * sell_size
        else:
            pnl = (self._sim_position.entry_price - price) * sell_size
        
        commission = sell_size * price * 0.0004
        net_pnl = pnl - commission
        
        self._sim_cash += self._sim_position.margin + net_pnl
        if pnl > 0:
            self._sim_winning += 1
        
        logger.info(f"✅ 合约平仓: {sell_size:.4f} @ {price:.2f} | PnL=${pnl:+.2f} | 手续费=${commission:.2f} | 净利=${net_pnl:+.2f} | 余额=${self._sim_cash:.2f}")
        self._sim_position = None
        self._save_state()
        return f"sim_futures_{int(time.time() * 1000)}"