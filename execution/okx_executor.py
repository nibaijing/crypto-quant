#!/usr/bin/env python3
"""CryptoQuant 量化交易系统 — OKX实盘执行器

核心设计:
1. 基于OKX API的真实交易
2. 统一资金管理 — 实时同步账户余额和持仓
3. 完整持久化 — state文件记录交易历史
4. 支持做多/做空/平仓
5. 实时风控检查 — 保证金、强平距离、仓位限制

资金模型:
  总权益 = 可用余额 + 保证金占用 + 未实现盈亏
  开仓: 调用OKX API下单
  平仓: 调用OKX API平仓
"""

import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

from core.config import get_config
from core.exchange_adapter import OKXAdapter, OrderSide, OrderType, PositionSide, AccountInfo, Position, Order

logger = logging.getLogger(__name__)


@dataclass
class OKXOrder:
    """OKX订单记录"""
    order_id: str
    symbol: str
    side: str
    type: str
    size: float
    price: float
    filled: float
    status: str
    timestamp: int
    fee: float = 0
    pnl: float = 0


@dataclass
class OKXPosition:
    """OKX持仓快照"""
    symbol: str
    side: str
    size: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    leverage: int
    margin: float
    liq_price: float
    timestamp: int
    bars_held: int = 0


@dataclass
class OKXAccount:
    """账户快照"""
    total_equity: float
    available_balance: float
    margin_balance: float
    unrealized_pnl: float
    total_trades: int
    winning_trades: int
    win_rate: float
    positions: List[OKXPosition] = field(default_factory=list)
    recent_orders: List[OKXOrder] = field(default_factory=list)
    api_connected: bool = True
    risk_level: str = "SAFE"  # SAFE, WARNING, DANGER


class OKXExecutor:
    """OKX实盘执行器
    
    核心属性:
        adapter: OKX API适配器
        leverage: 当前杠杆倍数
        position: 当前持仓 (None = 空仓)
        orders: 订单历史
    """

    def __init__(self, config=None):
        self.config = config or get_config()
        
        # 初始化OKX适配器
        self.adapter = OKXAdapter(self.config)
        
        # 运行时状态
        self._leverage = self.config.futures.default_leverage
        self._position: Optional[OKXPosition] = None
        self._orders: List[OKXOrder] = []
        self._total_trades = 0
        self._winning_trades = 0
        self._consecutive_losses = 0
        self._api_connected = True
        self._last_sync_time = 0
        
        # 持久化文件
        self.state_file = self.config.project_root / "data" / "okx_live_state.json"
        self.orders_file = self.config.project_root / "data" / "okx_orders.json"
        
        # 加载历史状态
        self._load_state()
        
        logger.info(f"OKX实盘执行器初始化 | 杠杆: {self._leverage}x | 模式: {'实盘' if not self.config.exchange.testnet else '模拟盘'}")

    # ==================== 属性 ====================

    @property
    def position(self) -> Optional[OKXPosition]:
        return self._position

    @property
    def cash(self) -> float:
        """可用余额"""
        account = self._get_account_info()
        return account.available_balance if account else 0

    @property
    def equity(self) -> float:
        """总权益"""
        account = self._get_account_info()
        return account.total_equity if account else 0

    @property
    def total_trades(self) -> int:
        return self._total_trades

    @property
    def winning_trades(self) -> int:
        return self._winning_trades

    @property
    def api_connected(self) -> bool:
        return self._api_connected

    # ==================== OKX API 调用 ====================

    def _get_account_info(self) -> Optional[AccountInfo]:
        """获取账户信息"""
        try:
            account = self.adapter.get_account_balance()
            if account:
                self._api_connected = True
            else:
                self._api_connected = False
            return account
        except Exception as e:
            logger.error(f"获取账户信息失败: {e}")
            self._api_connected = False
            return None

    def _sync_position(self) -> Optional[OKXPosition]:
        """同步持仓信息"""
        try:
            positions = self.adapter.get_positions(inst_type="SWAP")
            
            # 过滤BTC-USDT-SWAP持仓
            btc_positions = [p for p in positions if "BTC-USDT-SWAP" in p.symbol]
            
            if not btc_positions:
                return None
            
            pos = btc_positions[0]
            return OKXPosition(
                symbol=pos.symbol,
                side=pos.side.value,
                size=pos.size,
                entry_price=pos.avg_price,
                mark_price=pos.mark_price,
                unrealized_pnl=pos.unrealized_pnl,
                unrealized_pnl_pct=(pos.unrealized_pnl / (pos.size * pos.avg_price) * 100 * pos.leverage) if pos.avg_price > 0 else 0,
                leverage=pos.leverage,
                margin=pos.margin,
                liq_price=pos.liq_price,
                timestamp=int(time.time() * 1000),
                bars_held=0,
            )
        except Exception as e:
            logger.error(f"同步持仓失败: {e}")
            return None

    def _check_risk(self, symbol: str, side: str, size: float) -> tuple[bool, str]:
        """风控检查
        
        Returns:
            (是否通过, 原因)
        """
        account = self._get_account_info()
        if not account:
            return False, "无法获取账户信息"
        
        # 检查连续亏损
        if self._consecutive_losses >= self.config.risk.max_consecutive_losses:
            return False, f"连续亏损{self._consecutive_losses}次，暂停交易"
        
        # 检查单笔交易限制
        trade_value = size * self._get_current_price(symbol)
        max_trade = account.total_equity * self.config.risk.max_single_position_pct
        if trade_value > max_trade:
            return False, f"单笔交易${trade_value:.0f}超过限制${max_trade:.0f}"
        
        # 检查总仓位
        current_positions = account.positions
        total_position = sum(p.size * p.mark_price for p in current_positions)
        max_total = account.total_equity * self.config.risk.max_total_position_pct
        if total_position + trade_value > max_total:
            return False, f"总仓位${total_position + trade_value:.0f}超过限制${max_total:.0f}"
        
        # 检查保证金充足性
        required_margin = trade_value / self._leverage
        if required_margin > account.available_balance * 0.9:
            return False, f"保证金不足: 需要${required_margin:.0f}, 可用${account.available_balance:.0f}"
        
        return True, "通过"

    def _get_current_price(self, symbol: str) -> float:
        """获取当前价格"""
        ticker = self.adapter.get_ticker(symbol)
        return ticker.last if ticker else 0

    # ==================== 状态持久化 ====================

    def _load_state(self):
        """加载持久化状态"""
        if not self.state_file.exists():
            self._save_state()
            return

        try:
            with open(self.state_file) as f:
                state = json.load(f)

            self._total_trades = state.get("total_trades", 0)
            self._winning_trades = state.get("winning_trades", 0)
            self._consecutive_losses = state.get("consecutive_losses", 0)
            self._leverage = state.get("leverage", self.config.futures.default_leverage)

            # 加载订单历史
            if self.orders_file.exists():
                with open(self.orders_file) as f:
                    orders_data = json.load(f)
                    self._orders = [OKXOrder(**o) for o in orders_data]

            logger.info(f"状态加载: 交易{self._total_trades}次, 胜{self._winning_trades}次")
        except Exception as e:
            logger.warning(f"状态加载失败: {e}")

    def _save_state(self):
        """保存状态"""
        state = {
            "total_trades": self._total_trades,
            "winning_trades": self._winning_trades,
            "consecutive_losses": self._consecutive_losses,
            "leverage": self._leverage,
            "updated_at": datetime.now().isoformat(),
        }
        
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

    def _save_order(self, order: OKXOrder):
        """保存订单记录"""
        self._orders.append(order)
        # 只保留最近100条
        if len(self._orders) > 100:
            self._orders = self._orders[-100:]
        
        orders_data = [
            {
                "order_id": o.order_id,
                "symbol": o.symbol,
                "side": o.side,
                "type": o.type,
                "size": o.size,
                "price": o.price,
                "filled": o.filled,
                "status": o.status,
                "timestamp": o.timestamp,
                "fee": o.fee,
                "pnl": o.pnl,
            }
            for o in self._orders
        ]
        
        with open(self.orders_file, "w") as f:
            json.dump(orders_data, f, indent=2)

    # ==================== 杠杆设置 ====================

    def set_leverage(self, lev: int):
        """设置杠杆"""
        target_lev = min(lev, self.config.futures.max_leverage)
        
        try:
            # 调用OKX API设置杠杆
            success = self.adapter.set_leverage(
                symbol="BTC-USDT-SWAP",
                leverage=target_lev,
                pos_side="long",
                mgn_mode=self.config.futures.margin_mode,
            )
            
            if success:
                self._leverage = target_lev
                logger.info(f"杠杆设置成功: {target_lev}x")
            else:
                logger.warning(f"杠杆设置失败: {target_lev}x")
        except Exception as e:
            logger.error(f"设置杠杆异常: {e}")

    # ==================== 价格更新 ====================

    def update_price(self, symbol: str, price: float):
        """更新持仓价格（用于计算浮动盈亏）"""
        if self._position and self._position.size > 0:
            self._position.mark_price = price
            
            # 重新计算浮动盈亏
            if self._position.side == "long":
                self._position.unrealized_pnl = (price - self._position.entry_price) * self._position.size
            else:
                self._position.unrealized_pnl = (self._position.entry_price - price) * self._position.size
            
            if self._position.entry_price > 0:
                self._position.unrealized_pnl_pct = (
                    self._position.unrealized_pnl
                    / (self._position.size * self._position.entry_price)
                    * self._position.leverage
                    * 100
                )

    def update_bars_held(self):
        """更新持仓K线数"""
        if self._position:
            self._position.bars_held += 1

    # ==================== 交易操作 ====================

    def buy(self, symbol: str, size: float = None, price: float = None) -> Optional[str]:
        """开多仓"""
        if price is None:
            logger.error("buy() 需要 price")
            return None

        # 获取当前价格
        current_price = self._get_current_price(symbol)
        if current_price == 0:
            logger.error("无法获取当前价格")
            return None

        # 计算仓位大小
        if size is None:
            account = self._get_account_info()
            if not account:
                return None
            max_pos = (account.available_balance * self._leverage * 0.9) / current_price
            size = max_pos

        # 风控检查
        passed, reason = self._check_risk(symbol, "buy", size)
        if not passed:
            logger.warning(f"风控拒绝: {reason}")
            return None

        try:
            # 调用OKX API下单
            order_id = self.adapter.place_order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                size=size,
                pos_side="long",
            )

            if order_id:
                self._total_trades += 1
                
                # 记录订单
                order = OKXOrder(
                    order_id=order_id,
                    symbol=symbol,
                    side="BUY",
                    type="MARKET",
                    size=size,
                    price=current_price,
                    filled=size,  # 市价单假设全部成交
                    status="FILLED",
                    timestamp=int(time.time() * 1000),
                )
                self._save_order(order)
                
                # 同步持仓
                self._position = self._sync_position()
                
                self._save_state()
                
                logger.info(f"✅ OKX开多: {size:.4f} {symbol} @ {current_price:.2f} | {self._leverage}x | 订单: {order_id}")
                return order_id
            else:
                logger.error("OKX下单失败")
                return None

        except Exception as e:
            logger.error(f"开多仓异常: {e}")
            return None

    def sell(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """平多仓"""
        if not self._position or self._position.side != "long":
            logger.warning("无多仓可平")
            return None

        try:
            # 调用OKX API平仓
            close_size = size or self._position.size
            order_id = self.adapter.place_order(
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                size=close_size,
                pos_side="long",
            )

            if order_id:
                # 计算盈亏
                current_price = self._get_current_price(symbol)
                pnl = (current_price - self._position.entry_price) * close_size
                
                if pnl > 0:
                    self._winning_trades += 1
                    self._consecutive_losses = 0
                else:
                    self._consecutive_losses += 1
                
                # 记录订单
                order = OKXOrder(
                    order_id=order_id,
                    symbol=symbol,
                    side="SELL",
                    type="MARKET",
                    size=close_size,
                    price=current_price,
                    filled=close_size,
                    status="FILLED",
                    timestamp=int(time.time() * 1000),
                    pnl=pnl,
                )
                self._save_order(order)
                
                # 同步持仓
                self._position = self._sync_position()
                
                self._save_state()
                
                logger.info(f"✅ OKX平多: {close_size:.4f} @ {current_price:.2f} | PnL=${pnl:+.2f} | 订单: {order_id}")
                return order_id
            else:
                logger.error("OKX平仓失败")
                return None

        except Exception as e:
            logger.error(f"平多仓异常: {e}")
            return None

    def short_sell(self, symbol: str, size: float = None, price: float = None) -> Optional[str]:
        """开空仓"""
        if price is None:
            logger.error("short_sell() 需要 price")
            return None

        # 获取当前价格
        current_price = self._get_current_price(symbol)
        if current_price == 0:
            logger.error("无法获取当前价格")
            return None

        # 计算仓位大小
        if size is None:
            account = self._get_account_info()
            if not account:
                return None
            max_pos = (account.available_balance * self._leverage * 0.9) / current_price
            size = max_pos

        # 风控检查
        passed, reason = self._check_risk(symbol, "sell", size)
        if not passed:
            logger.warning(f"风控拒绝: {reason}")
            return None

        try:
            # 调用OKX API下单
            order_id = self.adapter.place_order(
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=OrderType.MARKET,
                size=size,
                pos_side="short",
            )

            if order_id:
                self._total_trades += 1
                
                # 记录订单
                order = OKXOrder(
                    order_id=order_id,
                    symbol=symbol,
                    side="SELL",
                    type="MARKET",
                    size=size,
                    price=current_price,
                    filled=size,
                    status="FILLED",
                    timestamp=int(time.time() * 1000),
                )
                self._save_order(order)
                
                # 同步持仓
                self._position = self._sync_position()
                
                self._save_state()
                
                logger.info(f"🔻 OKX开空: {size:.4f} {symbol} @ {current_price:.2f} | {self._leverage}x | 订单: {order_id}")
                return order_id
            else:
                logger.error("OKX下单失败")
                return None

        except Exception as e:
            logger.error(f"开空仓异常: {e}")
            return None

    def short_cover(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """平空仓"""
        if not self._position or self._position.side != "short":
            logger.warning("无空仓可平")
            return None

        try:
            # 调用OKX API平仓
            close_size = size or self._position.size
            order_id = self.adapter.place_order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                size=close_size,
                pos_side="short",
            )

            if order_id:
                # 计算盈亏
                current_price = self._get_current_price(symbol)
                pnl = (self._position.entry_price - current_price) * close_size
                
                if pnl > 0:
                    self._winning_trades += 1
                    self._consecutive_losses = 0
                else:
                    self._consecutive_losses += 1
                
                # 记录订单
                order = OKXOrder(
                    order_id=order_id,
                    symbol=symbol,
                    side="BUY",
                    type="MARKET",
                    size=close_size,
                    price=current_price,
                    filled=close_size,
                    status="FILLED",
                    timestamp=int(time.time() * 1000),
                    pnl=pnl,
                )
                self._save_order(order)
                
                # 同步持仓
                self._position = self._sync_position()
                
                self._save_state()
                
                logger.info(f"✅ OKX平空: {close_size:.4f} @ {current_price:.2f} | PnL=${pnl:+.2f} | 订单: {order_id}")
                return order_id
            else:
                logger.error("OKX平仓失败")
                return None

        except Exception as e:
            logger.error(f"平空仓异常: {e}")
            return None

    # ==================== 账户信息 ====================

    def get_account(self) -> OKXAccount:
        """获取账户快照"""
        # 同步持仓
        self._position = self._sync_position()
        
        # 获取账户信息
        account_info = self._get_account_info()
        
        if not account_info:
            # 返回默认值
            return OKXAccount(
                total_equity=0,
                available_balance=0,
                margin_balance=0,
                unrealized_pnl=0,
                total_trades=self._total_trades,
                winning_trades=self._winning_trades,
                win_rate=(self._winning_trades / max(self._total_trades, 1)) * 100,
                positions=[],
                recent_orders=self._orders[-10:],
                api_connected=False,
                risk_level="UNKNOWN",
            )
        
        # 构建持仓列表
        positions = []
        if self._position and self._position.size > 0:
            positions.append(self._position)
        
        # 计算风险等级
        risk_level = self._calculate_risk_level(account_info, positions)
        
        win_rate = (self._winning_trades / max(self._total_trades, 1)) * 100
        
        return OKXAccount(
            total_equity=account_info.total_equity,
            available_balance=account_info.available_balance,
            margin_balance=account_info.margin_balance,
            unrealized_pnl=account_info.unrealized_pnl,
            total_trades=self._total_trades,
            winning_trades=self._winning_trades,
            win_rate=win_rate,
            positions=positions,
            recent_orders=self._orders[-10:],
            api_connected=self._api_connected,
            risk_level=risk_level,
        )

    def _calculate_risk_level(self, account: AccountInfo, positions: List[OKXPosition]) -> str:
        """计算风险等级"""
        if not self._api_connected:
            return "UNKNOWN"
        
        # 检查保证金使用率
        margin_ratio = account.margin_balance / account.total_equity if account.total_equity > 0 else 0
        if margin_ratio > 0.8:
            return "DANGER"
        elif margin_ratio > 0.6:
            return "WARNING"
        
        # 检查强平距离
        for pos in positions:
            if pos.liq_price > 0:
                if pos.side == "long":
                    distance = (pos.mark_price - pos.liq_price) / pos.mark_price
                else:
                    distance = (pos.liq_price - pos.mark_price) / pos.mark_price
                
                if distance < 0.1:
                    return "DANGER"
                elif distance < 0.2:
                    return "WARNING"
        
        # 检查连续亏损
        if self._consecutive_losses >= 3:
            return "WARNING"
        
        return "SAFE"

    def get_ticker(self, symbol: str) -> Optional[Dict]:
        """获取行情"""
        try:
            ticker = self.adapter.get_ticker(symbol)
            if ticker:
                return {
                    "symbol": symbol,
                    "price": ticker.last,
                    "timestamp": ticker.timestamp,
                }
        except Exception as e:
            logger.error(f"获取行情失败: {e}")
        return None

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> List[Dict]:
        """获取K线"""
        try:
            klines = self.adapter.get_klines(symbol, interval, limit)
            return [
                {
                    "timestamp": k.timestamp,
                    "open": k.open,
                    "high": k.high,
                    "low": k.low,
                    "close": k.close,
                    "volume": k.volume,
                }
                for k in klines
            ]
        except Exception as e:
            logger.error(f"获取K线失败: {e}")
            return []
