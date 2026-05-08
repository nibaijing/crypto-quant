#!/usr/bin/env python3
"""CryptoQuant 量化交易系统 — 模拟盘执行器

核心设计:
1. 合约本地模拟 — 完全隔离无需 API Key
2. 统一资金管理 — cash + margin + pnl 模型
3. 完整持久化 — state 文件在每次交易后自动保存
4. 支持做多/做空/平仓

资金模型:
  总权益 = 可用现金 + 已占用保证金 + 未实现盈亏
  开仓: 扣减保证金, 记录持仓
  平仓: 释放保证金, 实现盈亏到现金
"""

import json
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

import requests

from core.config import get_config

logger = logging.getLogger(__name__)


@dataclass
class LivePosition:
    """实盘持仓快照"""
    symbol: str
    side: str           # long/short
    size: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    leverage: int = 1
    margin: float = 0
    timestamp: int = 0
    bars_held: int = 0  # 持仓 K 线数
    addition_count: int = 0  # 加仓次数


@dataclass
class LiveOrder:
    """下单记录"""
    order_id: str
    symbol: str
    side: str
    type: str
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


class FuturesExecutor:
    """合约执行器 — 本地模拟
    
    核心属性:
        cash: 可用现金 (不含保证金)
        equity: 总权益 = cash + margin + unrealized_pnl
        position: 当前持仓 (None = 空仓)
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
        self._initial_capital = self._sim_cash
        self._load_state()
        logger.info(f"合约执行器初始化 | 杠杆: {self._sim_leverage}x | 资金: ${self._sim_cash:.0f}")

    # ==================== 属性 ====================

    @property
    def position(self) -> Optional[LivePosition]:
        return self._sim_position

    @property
    def cash(self) -> float:
        """可用现金 (未占用)"""
        return self._sim_cash

    @property
    def total_trades(self) -> int:
        return self._sim_total_trades

    @property
    def winning_trades(self) -> int:
        return self._sim_winning

    @property
    def equity(self) -> float:
        """总权益 = 可用 + 保证金 + 浮动盈亏"""
        if self._sim_position and self._sim_position.size > 0:
            return self._sim_cash + self._sim_position.margin + self._sim_position.unrealized_pnl
        return self._sim_cash

    @property
    def used_margin(self) -> float:
        if self._sim_position:
            return self._sim_position.margin
        return 0

    # ==================== 状态持久化 ====================

    def _load_state(self):
        """加载持久化状态 (含持仓恢复)"""
        if not self.state_file.exists():
            self._save_state()
            return

        try:
            with open(self.state_file) as f:
                state = json.load(f)

            loaded_cash = state.get("cash", self._sim_cash)
            if loaded_cash >= 1:  # sanity: 至少 $1
                self._sim_cash = loaded_cash
            else:
                logger.warning(f"状态 cash=${loaded_cash} 异常, 使用默认 ${self._sim_cash}")

            self._sim_total_trades = state.get("total_trades", 0)
            self._sim_winning = state.get("winning_trades", 0)

            # 恢复持仓
            pos_data = state.get("position")
            if pos_data and pos_data.get("size", 0) > 0:
                self._sim_position = LivePosition(
                    symbol=pos_data["symbol"],
                    side=pos_data["side"],
                    size=pos_data["size"],
                    entry_price=pos_data["entry_price"],
                    current_price=pos_data.get("entry_price", 0),
                    unrealized_pnl=0,
                    unrealized_pnl_pct=0,
                    leverage=pos_data.get("leverage", self._sim_leverage),
                    margin=pos_data.get("margin", 0),
                    timestamp=int(time.time() * 1000),
                )
                logger.info(
                    f"持仓恢复: {self._sim_position.side} "
                    f"{self._sim_position.size:.4f} @ ${self._sim_position.entry_price:.0f}"
                )
        except Exception as e:
            logger.warning(f"状态加载失败: {e}")

    def _save_state(self):
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
                "margin": self._sim_position.margin,
            }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)

    # ==================== 杠杆 ====================

    def set_leverage(self, lev: int):
        self._sim_leverage = min(lev, self.config.futures.max_leverage)

    # ==================== 价格更新 ====================

    def update_price(self, symbol: str, price: float):
        if self._sim_position and self._sim_position.size > 0:
            self._sim_position.current_price = price
            ep = self._sim_position.entry_price
            sz = self._sim_position.size
            lev = self._sim_position.leverage

            if self._sim_position.side == "long":
                self._sim_position.unrealized_pnl = (price - ep) * sz
            else:
                self._sim_position.unrealized_pnl = (ep - price) * sz

            if ep > 0:
                self._sim_position.unrealized_pnl_pct = (
                    self._sim_position.unrealized_pnl
                    / (sz * ep)
                    * lev
                    * 100
                )

            # 限频保存状态 (每5秒最多一次，供 Dashboard 读取)
            now = time.time()
            if not hasattr(self, '_last_state_save'):
                self._last_state_save = 0
            if now - self._last_state_save > 5:
                self._save_state()
                self._last_state_save = now

    def update_bars_held(self):
        if self._sim_position:
            self._sim_position.bars_held += 1

    # ==================== 交易操作 ====================

    def _check_risk(self, symbol: str, side: str, size: float, price: float) -> tuple[bool, str]:
        """风控检查：单笔上限 + 总仓位上限 + 保证金 + 反向持仓拦截。

        期货语义: max_single/max_total_position_pct 控制的是保证金占权益比例。
        例如 pct=0.2 在 10x 杠杆下 → trade_value ≤ equity × 200%。

        Returns (pass, reason)。
        """
        equity = self.equity
        trade_value = size * price
        margin = trade_value / self._sim_leverage

        # 1. 反向持仓拦截 (防御层)
        if self._sim_position and self._sim_position.size > 0:
            current_side = self._sim_position.side
            if (current_side == "long" and side == "short") or (current_side == "short" and side == "long"):
                return False, f"已持有{current_side}仓, 禁止开反向仓"

        # 2. 单笔上限 — 保证金 ≤ equity × max_single_position_pct
        max_single_margin = equity * self.config.risk.max_single_position_pct
        if margin > max_single_margin:
            max_trade = max_single_margin * self._sim_leverage
            return False, (
                f"保证金 ${margin:,.0f} (仓位 ${trade_value:,.0f}) 超过单笔上限 "
                f"${max_single_margin:,.0f} (equity ${equity:,.0f} × {self.config.risk.max_single_position_pct:.0%}"
                f" → 最大仓位 ${max_trade:,.0f})"
            )

        # 3. 总仓位上限 — 总保证金 ≤ equity × max_total_position_pct
        current_value = 0.0
        if self._sim_position and self._sim_position.size > 0:
            current_value = self._sim_position.size * price
        current_margin = current_value / self._sim_leverage if self._sim_leverage > 0 else 0
        total_margin = current_margin + margin
        max_total_margin = equity * self.config.risk.max_total_position_pct
        if total_margin > max_total_margin:
            max_total_trade = max_total_margin * self._sim_leverage
            return False, (
                f"总保证金 ${total_margin:,.0f} 超过上限 "
                f"${max_total_margin:,.0f} (equity ${equity:,.0f} × {self.config.risk.max_total_position_pct:.0%}"
                f" → 最大总仓位 ${max_total_trade:,.0f})"
            )

        # 4. 保证金充足
        available = self._sim_cash * 0.95  # 留5%缓冲
        if margin > available:
            return False, f"保证金不足: 需要 ${margin:,.0f} > 可用 ${available:,.0f}"

        return True, "通过"

    def _get_max_allowed_size(self, price: float) -> float:
        """基于风控上限计算单次开仓最大数量（期货：保证金约束）。

        取 min(现金可买, 单笔保证金上限, 总仓位剩余保证金空间)。
        max_single/max_total_position_pct = 保证金占权益比例。
        """
        equity = self.equity

        # 现金可买 (杠杆放大购买力)
        max_from_cash = (self._sim_cash * self._sim_leverage * 0.9) / price if price > 0 else 0

        # 单笔保证金上限 → 最大仓位
        max_single_margin = equity * self.config.risk.max_single_position_pct
        max_from_single = (max_single_margin * self._sim_leverage) / price if price > 0 else 0

        # 总仓位剩余保证金 → 最大仓位
        current_value = 0.0
        if self._sim_position and self._sim_position.size > 0:
            current_value = self._sim_position.size * price
        current_margin = current_value / self._sim_leverage if self._sim_leverage > 0 else 0
        max_total_margin = equity * self.config.risk.max_total_position_pct
        remaining_margin = max(max_total_margin - current_margin, 0)
        max_from_total = (remaining_margin * self._sim_leverage) / price if price > 0 else 0

        return max(0.0001, min(max_from_cash, max_from_single, max_from_total))

    def _close_opposite(self, symbol: str, price: float) -> bool:
        """平掉对向持仓 (如果存在), 返回是否平了"""
        if not self._sim_position or self._sim_position.size <= 0:
            return False
        return False  # 不再自动平仓 —— 由信号层管理

    def buy(self, symbol: str, size: float = None, price: float = None) -> Optional[str]:
        """开多 / 加多 — 受风控上限约束"""
        if price is None:
            logger.error("buy() 需要 price")
            return None

        if size is None:
            size = self._get_max_allowed_size(price)

        # 风控检查
        ok, reason = self._check_risk(symbol, "long", size, price)
        if not ok:
            logger.warning(f"❌ LONG 风控拒绝: {reason}")
            return None

        cost = size * price
        margin = cost / self._sim_leverage

        commission = cost * self.config.backtest.commission
        self._sim_cash -= (margin + commission)
        self._sim_total_trades += 1

        self._sim_position = LivePosition(
            symbol=symbol, side="long", size=size,
            entry_price=price, current_price=price,
            unrealized_pnl=0, unrealized_pnl_pct=0,
            leverage=self._sim_leverage, margin=margin,
            timestamp=int(time.time() * 1000), bars_held=0,
        )
        self._save_state()
        logger.info(f"✅ 合约做多: {size:.4f} {symbol} @ {price:.2f} | {self._sim_leverage}x | 保证金=${margin:.0f}")
        return f"sim_buy_{int(time.time() * 1000)}"

    def sell(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """平多 / 减多"""
        if not self._sim_position or self._sim_position.side != "long":
            return None

        if price is None:
            logger.error("sell() 需要 price")
            return None

        close_size = size or self._sim_position.size
        close_size = min(close_size, self._sim_position.size)

        pnl = (price - self._sim_position.entry_price) * close_size
        released_margin = self._sim_position.margin * (close_size / self._sim_position.size)
        commission = close_size * price * self.config.backtest.commission

        self._sim_cash += released_margin + pnl - commission

        net_pnl = pnl - commission
        if pnl > 0:
            self._sim_winning += 1

        logger.info(
            f"✅ 合约平多: {close_size:.4f} @ {price:.2f} | "
            f"PnL=${pnl:+.2f} | 手续费=${commission:.2f} | 净利=${net_pnl:+.2f} | 余额=${self._sim_cash:.2f}"
        )

        # 部分平仓 or 全部
        remaining = self._sim_position.size - close_size
        if remaining > 0.0001:
            self._sim_position.size = remaining
            self._sim_position.margin -= released_margin
            self._sim_position.unrealized_pnl = 0
            self._sim_position.unrealized_pnl_pct = 0
        else:
            self._sim_position = None

        self._save_state()
        return f"sim_sell_{int(time.time() * 1000)}"

    def short_sell(self, symbol: str, size: float = None, price: float = None) -> Optional[str]:
        """开空 — 受风控上限约束"""
        if price is None:
            logger.error("short_sell() 需要 price")
            return None

        if size is None:
            size = self._get_max_allowed_size(price)

        # 风控检查
        ok, reason = self._check_risk(symbol, "short", size, price)
        if not ok:
            logger.warning(f"❌ SHORT 风控拒绝: {reason}")
            return None

        cost = size * price
        margin = cost / self._sim_leverage

        commission = cost * self.config.backtest.commission
        self._sim_cash -= (margin + commission)
        self._sim_total_trades += 1

        self._sim_position = LivePosition(
            symbol=symbol, side="short", size=size,
            entry_price=price, current_price=price,
            unrealized_pnl=0, unrealized_pnl_pct=0,
            leverage=self._sim_leverage, margin=margin,
            timestamp=int(time.time() * 1000), bars_held=0,
        )
        self._save_state()
        logger.info(f"🔻 合约做空: {size:.4f} {symbol} @ {price:.2f} | {self._sim_leverage}x | 保证金=${margin:.0f}")
        return f"sim_short_{int(time.time() * 1000)}"

    def short_cover(self, symbol: str, price: float, size: float = None) -> Optional[str]:
        """平空 / 减空"""
        if not self._sim_position or self._sim_position.side != "short":
            return None

        if price is None:
            logger.error("short_cover() 需要 price")
            return None

        close_size = size or self._sim_position.size
        close_size = min(close_size, self._sim_position.size)

        pnl = (self._sim_position.entry_price - price) * close_size
        released_margin = self._sim_position.margin * (close_size / self._sim_position.size)
        commission = close_size * price * self.config.backtest.commission

        self._sim_cash += released_margin + pnl - commission

        net_pnl = pnl - commission
        if pnl > 0:
            self._sim_winning += 1

        logger.info(
            f"✅ 合约平空: {close_size:.4f} @ {price:.2f} | "
            f"PnL=${pnl:+.2f} | 手续费=${commission:.2f} | 净利=${net_pnl:+.2f} | 余额=${self._sim_cash:.2f}"
        )

        remaining = self._sim_position.size - close_size
        if remaining > 0.0001:
            self._sim_position.size = remaining
            self._sim_position.margin -= released_margin
            self._sim_position.unrealized_pnl = 0
            self._sim_position.unrealized_pnl_pct = 0
        else:
            self._sim_position = None

        self._save_state()
        return f"sim_cover_{int(time.time() * 1000)}"

    # ==================== 仓位缩放 — 加仓 / 减仓 ====================

    MAX_ADDITIONS = 2  # 单边最多加仓次数，防无底洞补仓

    def add_to_long(self, symbol: str, add_size: float, price: float) -> Optional[str]:
        """加多：增加现有做多仓位，计算加权均价"""
        if not self._sim_position or self._sim_position.side != "long":
            logger.warning("无做多持仓, 无法加仓")
            return None
        if self._sim_position.addition_count >= self.MAX_ADDITIONS:
            logger.warning(f"加仓次数已达上限 ({self.MAX_ADDITIONS}), 拒绝加仓")
            return None

        ok, reason = self._check_risk(symbol, "long", add_size, price)
        if not ok:
            logger.warning(f"❌ 加仓风控拒绝: {reason}")
            return None

        cost = add_size * price
        margin = cost / self._sim_leverage
        commission = cost * self.config.backtest.commission

        # 加权均价
        old_size = self._sim_position.size
        old_total = old_size * self._sim_position.entry_price
        new_size = old_size + add_size
        new_entry = (old_total + cost) / new_size

        self._sim_cash -= (margin + commission)
        self._sim_position.size = new_size
        self._sim_position.entry_price = new_entry
        self._sim_position.margin += margin
        self._sim_position.addition_count += 1
        self._sim_total_trades += 1
        self._save_state()

        logger.info(
            f"📈 加多: +{add_size:.4f} → 总{new_size:.4f} | "
            f"均价${new_entry:.0f} | 加{self._sim_position.addition_count}/{self.MAX_ADDITIONS}次 | "
            f"{self._sim_leverage}x"
        )
        return f"sim_add_long_{int(time.time() * 1000)}"

    def add_to_short(self, symbol: str, add_size: float, price: float) -> Optional[str]:
        """加空：增加现有做空仓位，计算加权均价"""
        if not self._sim_position or self._sim_position.side != "short":
            logger.warning("无做空持仓, 无法加仓")
            return None
        if self._sim_position.addition_count >= self.MAX_ADDITIONS:
            logger.warning(f"加仓次数已达上限 ({self.MAX_ADDITIONS}), 拒绝加仓")
            return None

        ok, reason = self._check_risk(symbol, "short", add_size, price)
        if not ok:
            logger.warning(f"❌ 加仓风控拒绝: {reason}")
            return None

        cost = add_size * price
        margin = cost / self._sim_leverage
        commission = cost * self.config.backtest.commission

        old_size = self._sim_position.size
        old_total = old_size * self._sim_position.entry_price
        new_size = old_size + add_size
        new_entry = (old_total + cost) / new_size

        self._sim_cash -= (margin + commission)
        self._sim_position.size = new_size
        self._sim_position.entry_price = new_entry
        self._sim_position.margin += margin
        self._sim_position.addition_count += 1
        self._sim_total_trades += 1
        self._save_state()

        logger.info(
            f"📉 加空: +{add_size:.4f} → 总{new_size:.4f} | "
            f"均价${new_entry:.0f} | 加{self._sim_position.addition_count}/{self.MAX_ADDITIONS}次 | "
            f"{self._sim_leverage}x"
        )
        return f"sim_add_short_{int(time.time() * 1000)}"

    # ==================== 行情 & 账户 ====================

    def get_account(self) -> LiveAccount:
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

    def get_ticker(self, symbol: str) -> Optional[Dict]:
        try:
            binance_symbol = symbol.replace("-", "")
            url = "https://api.binance.com/api/v3/ticker/price"
            resp = requests.get(url, params={"symbol": binance_symbol}, timeout=5)
            data = resp.json()
            return {
                "symbol": symbol,
                "price": float(data["price"]),
                "timestamp": int(time.time() * 1000),
            }
        except Exception as e:
            logger.error(f"行情获取失败: {e}")
            return None

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> List[Dict]:
        try:
            binance_symbol = symbol.replace("-", "")
            url = "https://api.binance.com/api/v3/klines"
            resp = requests.get(
                url,
                params={"symbol": binance_symbol, "interval": interval, "limit": min(limit, 1000)},
                timeout=10,
            )
            data = resp.json()
            return [
                {
                    "timestamp": item[0],
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[5]),
                }
                for item in data
            ]
        except Exception as e:
            logger.error(f"K线获取失败: {e}")
            return []