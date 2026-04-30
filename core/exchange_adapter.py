"""OKX 交易所适配层 - REST API + WebSocket

统一接口，支持现货和合约（永续/交割）。基于 python-okx SDK 封装。
"""

import asyncio
import json
import logging
from typing import Optional, Dict, List, Any, Callable, Union
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

import okx.MarketData as MarketData
import okx.Trade as Trade
import okx.Account as Account
import okx.PublicData as PublicData
from okx.websocket.WsPublicAsync import WsPublicAsync
from okx.websocket.WsPrivateAsync import WsPrivateAsync

from core.config import get_config, AppConfig

logger = logging.getLogger(__name__)


# ===== 数据类型 =====

class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    NET = "net"  # 现货单向持仓


class InstrumentType(str, Enum):
    SPOT = "SPOT"
    SWAP = "SWAP"       # 永续合约
    FUTURES = "FUTURES"  # 交割合约
    MARGIN = "MARGIN"


@dataclass
class Kline:
    """K线数据结构"""
    timestamp: int  # ms
    open: float
    high: float
    low: float
    close: float
    volume: float
    vol_ccy: float = 0  # 计价货币成交量
    
    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass
class Ticker:
    """实时行情"""
    symbol: str
    last: float
    bid: float
    ask: float
    high_24h: float
    low_24h: float
    vol_24h: float
    timestamp: int


@dataclass
class Order:
    """订单"""
    order_id: str
    symbol: str
    side: OrderSide
    type: OrderType
    price: float
    size: float
    filled: float = 0
    status: str = ""
    timestamp: int = 0


@dataclass
class Position:
    """持仓"""
    symbol: str
    side: PositionSide
    size: float          # 合约张数
    avg_price: float     # 开仓均价
    mark_price: float    # 标记价格
    liq_price: float     # 强平价格
    margin: float        # 保证金
    leverage: int
    unrealized_pnl: float = 0
    realized_pnl: float = 0


@dataclass
class AccountInfo:
    """账户信息"""
    total_equity: float      # 总权益
    available_balance: float # 可用余额
    margin_balance: float    # 保证金余额
    unrealized_pnl: float    # 未实现盈亏
    positions: List[Position] = field(default_factory=list)


# ===== OKX 适配器 =====

class OKXAdapter:
    """OKX 交易所适配器 - 统一现货/合约入口
    
    支持:
    - REST: 行情、交易、账户查询
    - WebSocket: 实时行情、订单簿、持仓推送
    """
    
    def __init__(self, config: AppConfig = None):
        self.config = config or get_config()
        self._testnet = self.config.exchange.testnet
        self._flag = "1" if self._testnet else "0"  # OKX flag: 0=实盘, 1=模拟盘
        
        # REST API clients
        self._market = MarketData.MarketAPI(flag=self._flag, debug=False)
        self._trade = Trade.TradeAPI(
            api_key=self.config.exchange.api_key or "",
            api_secret_key=self.config.exchange.api_secret or "",
            passphrase=self.config.exchange.passphrase or "",
            flag=self._flag,
            debug=False,
        )
        self._account = Account.AccountAPI(
            api_key=self.config.exchange.api_key or "",
            api_secret_key=self.config.exchange.api_secret or "",
            passphrase=self.config.exchange.passphrase or "",
            flag=self._flag,
            debug=False,
        )
        
        # WebSocket (延迟初始化，避免不必要的连接)
        self._ws_public: Optional[WsPublicAsync] = None
        self._ws_private: Optional[WsPrivateAsync] = None
        self._ws_callbacks: Dict[str, List[Callable]] = {}
        
        logger.info(f"OKX适配器初始化 | testnet={self._testnet}")
    
    # ===== REST API: 行情 =====
    
    def get_klines(self, symbol: str, bar: str = "1H", limit: int = 100,
                   before: str = None, after: str = None) -> List[Kline]:
        """获取历史K线
        
        Args:
            symbol: 交易对，如 BTC-USDT-SWAP (合约) 或 BTC-USDT (现货)
            bar: K线周期 1m/5m/15m/30m/1H/4H/1D
            limit: 数量，最大300
            before: 分页，在此之前的时间戳
            after: 分页，在此之后的时间戳
        """
        try:
            result = self._market.get_candlesticks(
                instId=symbol,
                bar=bar,
                limit=str(limit),
                before=before,
                after=after,
            )
            
            if result.get("code") != "0":
                logger.error(f"获取K线失败: {result.get('msg')}")
                return []
            
            klines = []
            for item in result["data"]:
                klines.append(Kline(
                    timestamp=int(item[0]),
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5]),
                    vol_ccy=float(item[6]),
                ))
            
            # 按时间升序排列
            klines.sort(key=lambda x: x.timestamp)
            return klines
            
        except Exception as e:
            logger.error(f"获取K线异常: {e}")
            return []
    
    def get_all_klines(self, symbol: str, bar: str = "1H",
                       start: datetime = None, end: datetime = None) -> List[Kline]:
        """获取大量历史K线（自动分页）
        
        一次请求最多返回300条，此方法自动翻页获取完整数据。
        """
        all_klines = []
        after = str(int(start.timestamp() * 1000)) if start else None
        before = str(int(end.timestamp() * 1000)) if end else None
        
        while True:
            batch = self.get_klines(
                symbol=symbol, bar=bar, limit=300,
                before=before, after=after,
            )
            
            if not batch:
                break
            
            all_klines.extend(batch)
            
            # 用最早的一条时间戳作为下一次的 before
            after = str(batch[0].timestamp)
            
            if len(batch) < 300:
                break
            
            # 避免速率限制
            import time
            time.sleep(0.2)
        
        # 去重 + 排序
        seen = set()
        unique = []
        for k in sorted(all_klines, key=lambda x: x.timestamp):
            if k.timestamp not in seen:
                seen.add(k.timestamp)
                unique.append(k)
        
        # 按时间范围过滤
        if start:
            start_ts = int(start.timestamp() * 1000)
            unique = [k for k in unique if k.timestamp >= start_ts]
        if end:
            end_ts = int(end.timestamp() * 1000)
            unique = [k for k in unique if k.timestamp <= end_ts]
        
        logger.info(f"获取K线完成: {symbol} {bar} | {len(unique)} 条")
        return unique
    
    def get_ticker(self, symbol: str) -> Optional[Ticker]:
        """获取实时行情"""
        try:
            result = self._market.get_ticker(instId=symbol)
            if result.get("code") != "0":
                return None
            
            item = result["data"][0]
            return Ticker(
                symbol=symbol,
                last=float(item["last"]),
                bid=float(item["bidPx"]),
                ask=float(item["askPx"]),
                high_24h=float(item["high24h"]),
                low_24h=float(item["low24h"]),
                vol_24h=float(item["vol24h"]),
                timestamp=int(item["ts"]),
            )
        except Exception as e:
            logger.error(f"获取行情失败: {e}")
            return None
    
    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """获取当前资金费率 (仅合约)"""
        try:
            result = self._market.get_funding_rate(instId=symbol)
            if result.get("code") != "0" or not result["data"]:
                return None
            return float(result["data"][0]["fundingRate"])
        except Exception as e:
            logger.error(f"获取资金费率失败: {e}")
            return None
    
    # ===== REST API: 账户 =====
    
    def get_account_balance(self) -> Optional[AccountInfo]:
        """获取账户余额和持仓"""
        try:
            balance = self._account.get_account_balance()
            if balance.get("code") != "0":
                return None
            
            data = balance["data"][0]
            total_eq = float(data["totalEq"])
            
            # 获取合约持仓
            positions = self.get_positions()
            
            return AccountInfo(
                total_equity=total_eq,
                available_balance=float(data["availEq"]),
                margin_balance=float(data.get("marginBal", 0)),
                unrealized_pnl=float(data.get("upl", 0)),
                positions=positions,
            )
        except Exception as e:
            logger.error(f"获取账户余额失败: {e}")
            return None
    
    def get_positions(self, inst_type: str = "SWAP") -> List[Position]:
        """获取当前持仓"""
        try:
            result = self._account.get_positions(instType=inst_type)
            if result.get("code") != "0":
                return []
            
            positions = []
            for item in result["data"]:
                pos_size = float(item.get("pos", 0))
                if pos_size == 0:
                    continue
                
                side = PositionSide.LONG if pos_size > 0 else PositionSide.SHORT
                positions.append(Position(
                    symbol=item["instId"],
                    side=side,
                    size=abs(pos_size),
                    avg_price=float(item["avgPx"]),
                    mark_price=float(item.get("markPx", 0)),
                    liq_price=float(item.get("liqPx", 0)),
                    margin=float(item.get("margin", 0)),
                    leverage=int(float(item.get("lever", 1))),
                    unrealized_pnl=float(item.get("upl", 0)),
                    realized_pnl=float(item.get("realizedPnl", 0)),
                ))
            
            return positions
        except Exception as e:
            logger.error(f"获取持仓失败: {e}")
            return []
    
    # ===== REST API: 交易 =====
    
    def place_order(self, symbol: str, side: Union[OrderSide, str],
                    order_type: Union[OrderType, str] = OrderType.MARKET,
                    size: float = 0.01, price: float = None,
                    pos_side: str = None) -> Optional[str]:
        """下单
        
        Args:
            symbol: 交易对
            side: buy/sell
            order_type: market/limit
            size: 数量 (合约: 张数; 现货: 币数量)
            price: 限价（市价可为None）
            pos_side: 持仓方向 (long/short, 合约用)
        
        Returns:
            订单ID，失败返回None
        """
        try:
            side_str = side.value if isinstance(side, OrderSide) else side
            type_str = order_type.value if isinstance(order_type, OrderType) else order_type
            
            logger.info(f"下单: {symbol} {side_str} {type_str} size={size} price={price}")
            
            # 合约下单：指定 tdMode 和 posSide
            is_contract = "SWAP" in symbol or "FUTURES" in symbol
            
            params = {
                "instId": symbol,
                "tdMode": "isolated" if is_contract else "cash",
                "side": side_str,
                "ordType": type_str,
                "sz": str(size),
            }
            
            if is_contract and pos_side:
                params["posSide"] = pos_side
            
            if price and type_str == "limit":
                params["px"] = str(price)
            
            result = self._trade.place_order(**params)
            
            if result.get("code") == "0":
                order_id = result["data"][0]["ordId"]
                logger.info(f"下单成功: {order_id}")
                return order_id
            else:
                logger.error(f"下单失败: {result.get('msg')}")
                return None
                
        except Exception as e:
            logger.error(f"下单异常: {e}")
            return None
    
    def set_leverage(self, symbol: str, leverage: int, 
                     pos_side: str = None, mgn_mode: str = "isolated") -> bool:
        """设置杠杆倍数 (仅合约)
        
        Args:
            symbol: 合约交易对
            leverage: 杠杆倍数
            pos_side: long/short 双向持仓时指定方向
            mgn_mode: isolated/cross
        """
        try:
            # 先设置逐仓/全仓
            self._account.set_position_mode(
                instId=symbol if not pos_side else None,
                posMode="long_short_mode",
            )
            
            result = self._account.set_leverage(
                instId=symbol,
                lever=str(leverage),
                mgnMode=mgn_mode,
                posSide=pos_side,
            )
            
            if result.get("code") == "0":
                logger.info(f"设置杠杆: {symbol} {leverage}x {mgn_mode}")
                return True
            else:
                logger.error(f"设置杠杆失败: {result.get('msg')}")
                return False
        except Exception as e:
            logger.error(f"设置杠杆异常: {e}")
            return False
    
    # ===== WebSocket (异步) =====
    
    async def ws_connect_public(self) -> bool:
        """连接公开 WebSocket (行情)"""
        if self._ws_public:
            return True
        
        try:
            url = self.config.exchange.ws.testnet_public_url if self._testnet else self.config.exchange.ws.public_url
            self._ws_public = WsPublicAsync(url=url)
            await self._ws_public.start()
            logger.info("WebSocket 公开频道已连接")
            return True
        except Exception as e:
            logger.error(f"WebSocket 公开连接失败: {e}")
            return False
    
    async def ws_subscribe_ticker(self, symbol: str, callback: Callable = None):
        """订阅实时行情"""
        await self.ws_connect_public()
        
        channel = {
            "channel": "tickers",
            "instId": symbol,
        }
        
        def _handler(message):
            logger.debug(f"Ticker: {message}")
            if callback:
                callback(message)
        
        await self._ws_public.subscribe([channel], _handler)
        logger.info(f"已订阅行情: {symbol}")
    
    async def ws_subscribe_candles(self, symbol: str, bar: str = "1H",
                                    callback: Callable = None):
        """订阅K线"""
        await self.ws_connect_public()
        
        channel = {
            "channel": f"candle{bar}",
            "instId": symbol,
        }
        
        def _handler(message):
            if callback:
                callback(message)
        
        await self._ws_public.subscribe([channel], _handler)
        logger.info(f"已订阅K线: {symbol} {bar}")
    
    async def ws_close(self):
        """关闭所有 WebSocket 连接"""
        if self._ws_public:
            await self._ws_public.close()
            self._ws_public = None
        if self._ws_private:
            await self._ws_private.close()
            self._ws_private = None
        logger.info("WebSocket 连接已关闭")